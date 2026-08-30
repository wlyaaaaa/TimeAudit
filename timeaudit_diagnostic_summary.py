"""Fast, bounded and payload-free historical evidence for PC diagnosis.

This provider is deliberately independent from Grafana and database passwords.
It performs one aggregate-only query through the existing PostgreSQL container
and returns a small versioned JSON document.  It never starts or repairs any
TimeAudit component.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import subprocess
import sys
from typing import Any


UTC = dt.timezone.utc
SCHEMA = "timeaudit.diagnostic-summary.v1"
OWNER_REF = "timeaudit:diagnostic-history"
DEFAULT_LOOKBACK_HOURS = 24
MAX_WINDOW_HOURS = 168
MAX_CLOCK_SKEW_SECONDS = 300
FRESHNESS_SECONDS = 60
QUERY_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 1_048_576
CONTAINER_NAME = "audit-postgres"


def parse_utc(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(UTC)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def build_aggregate_sql() -> str:
    """Return the single aggregate-only SQL statement used by the provider."""
    return r"""
WITH bounds AS (
  SELECT :'after_utc'::timestamptz AS t_from,
         :'until_utc'::timestamptz AS t_to
),
hardware AS MATERIALIZED (
  SELECT
    h."timestamp", h.current_fps, h.average_fps,
    h.one_percent_low_fps, h.frametime_ms,
    h.cpu_total_usage, h.cpu_package_temp, h.cpu_package_power,
    h.system_dpc_latency, h.gpu_usage, h.gpu_core_temp,
    h.gpu_hotspot_temp, h.gpu_board_power, h.system_ram_usage_pct,
    h.disk_max_latency_ms, h.network_ping_ms, h.is_packet_loss
  FROM public.fact_system_hardware h, bounds b
  WHERE h."timestamp" > b.t_from AND h."timestamp" <= b.t_to
),
valid_frames AS MATERIALIZED (
  SELECT h.*
  FROM hardware h
  WHERE h.current_fps BETWEEN 0.5 AND 1000
    AND h.average_fps BETWEEN 0.5 AND 1000
    AND h.frametime_ms BETWEEN 0.5 AND 2000
    AND ABS(h.frametime_ms - 1000.0 / h.current_fps)
        <= GREATEST(3.0, (1000.0 / h.current_fps) * 0.35)
),
hardware_gaps AS (
  SELECT h.*,
         EXTRACT(EPOCH FROM (
           h."timestamp" - LAG(h."timestamp") OVER (ORDER BY h."timestamp")
         )) AS gap_seconds
  FROM hardware h
),
hardware_summary AS (
  SELECT
    COUNT(*)::bigint AS hardware_sample_count,
    MIN("timestamp")::text AS first_sample_utc,
    MAX("timestamp")::text AS last_sample_utc,
    ROUND(COALESCE(MAX(gap_seconds), 0)::numeric, 3)
      AS max_internal_gap_seconds,

    ROUND(AVG(cpu_total_usage)::numeric, 3) AS cpu_usage_avg_pct,
    ROUND(MAX(cpu_total_usage)::numeric, 3) AS cpu_usage_max_pct,
    ROUND(AVG(cpu_package_temp)::numeric, 3) AS cpu_temp_avg_c,
    ROUND(MAX(cpu_package_temp)::numeric, 3) AS cpu_temp_max_c,
    ROUND(AVG(cpu_package_power)::numeric, 3) AS cpu_power_avg_w,
    ROUND(MAX(cpu_package_power)::numeric, 3) AS cpu_power_max_w,

    ROUND(AVG(gpu_usage)::numeric, 3) AS gpu_usage_avg_pct,
    ROUND(MAX(gpu_usage)::numeric, 3) AS gpu_usage_max_pct,
    ROUND(AVG(gpu_core_temp)::numeric, 3) AS gpu_temp_avg_c,
    ROUND(MAX(gpu_core_temp)::numeric, 3) AS gpu_temp_max_c,
    ROUND(MAX(gpu_hotspot_temp)::numeric, 3) AS gpu_hotspot_max_c,
    ROUND(AVG(gpu_board_power)::numeric, 3) AS gpu_power_avg_w,
    ROUND(MAX(gpu_board_power)::numeric, 3) AS gpu_power_max_w,

    ROUND(AVG(system_ram_usage_pct)::numeric, 3) AS ram_usage_avg_pct,
    ROUND(MAX(system_ram_usage_pct)::numeric, 3) AS ram_usage_max_pct,
    ROUND(AVG(disk_max_latency_ms)::numeric, 3) AS disk_latency_avg_ms,
    ROUND((percentile_cont(0.95) WITHIN GROUP (
      ORDER BY disk_max_latency_ms
    ))::numeric, 3) AS disk_latency_p95_ms,
    ROUND(MAX(disk_max_latency_ms)::numeric, 3) AS disk_latency_max_ms,
    ROUND(AVG(network_ping_ms)::numeric, 3) AS network_ping_avg_ms,
    MAX(network_ping_ms)::bigint AS network_ping_max_ms,
    COUNT(*) FILTER (WHERE is_packet_loss = 1)::bigint AS packet_loss_samples,

    (SELECT COUNT(*) FROM hardware WHERE current_fps > 0)::bigint
      AS fps_positive_sample_count,
    (SELECT COUNT(*) FROM valid_frames)::bigint AS fps_sample_count,
    (SELECT ROUND(AVG(current_fps)::numeric, 3) FROM valid_frames) AS fps_avg,
    (SELECT ROUND(MIN(current_fps)::numeric, 3) FROM valid_frames) AS fps_min,
    (SELECT ROUND(AVG(one_percent_low_fps)::numeric, 3)
       FROM valid_frames
      WHERE one_percent_low_fps BETWEEN 0.1 AND 1000)
      AS fps_one_percent_low_avg,
    (SELECT ROUND((percentile_cont(0.95) WITHIN GROUP (
       ORDER BY frametime_ms))::numeric, 3) FROM valid_frames)
      AS frametime_p95_ms,
    (SELECT ROUND(MAX(frametime_ms)::numeric, 3) FROM valid_frames)
      AS frametime_max_ms,
    (SELECT COUNT(*) FROM valid_frames WHERE frametime_ms >= 50)::bigint
      AS frametime_spike_samples,

    COUNT(*) FILTER (
      WHERE cpu_package_temp >= 95 AND cpu_package_temp <= 120
    )::bigint AS cpu_thermal_samples,
    COUNT(*) FILTER (
      WHERE (gpu_core_temp >= 90 AND gpu_core_temp <= 120)
         OR (gpu_hotspot_temp >= 105 AND gpu_hotspot_temp <= 130)
    )::bigint AS gpu_thermal_samples,
    COUNT(*) FILTER (
      WHERE system_ram_usage_pct >= 95 AND system_ram_usage_pct <= 100
    )::bigint AS memory_pressure_samples,
    COUNT(*) FILTER (
      WHERE disk_max_latency_ms >= 1000 AND disk_max_latency_ms <= 600000
    )::bigint AS storage_latency_samples,
    COUNT(*) FILTER (
      WHERE cpu_total_usage < 0 OR cpu_total_usage > 100
         OR gpu_usage < 0 OR gpu_usage > 100
         OR system_ram_usage_pct < 0 OR system_ram_usage_pct > 100
         OR cpu_package_temp < 0 OR cpu_package_temp > 120
         OR gpu_core_temp < 0 OR gpu_core_temp > 120
         OR cpu_package_power < 0 OR gpu_board_power < 0
         OR disk_max_latency_ms < 0 OR system_dpc_latency < 0
    )::bigint AS telemetry_out_of_bounds_samples
  FROM hardware_gaps
),
state_clip AS MATERIALIZED (
  SELECT
    CASE
      WHEN a.process_name = 'System_Sleep' THEN 'sleep'
      WHEN a.process_name = 'System_DisplayOff' THEN 'display_off'
      WHEN a.process_name IN ('System_LockScreen', 'LockApp.exe', 'LogonUI.exe')
        THEN 'lock'
      WHEN a.process_name IN ('System_Idle', 'Idle') THEN 'idle'
      ELSE 'active'
    END AS state,
    GREATEST(a.start_time, b.t_from) AS s,
    LEAST(
      a.start_time + (a.duration_seconds || ' seconds')::interval,
      b.t_to
    ) AS e
  FROM public.app_usage_logs a, bounds b
  WHERE a.duration_seconds > 0
    AND a.start_time < b.t_to
    AND a.start_time + (a.duration_seconds || ' seconds')::interval > b.t_from
),
state_with_previous AS (
  SELECT state, s, e,
         MAX(e) OVER (
           PARTITION BY state ORDER BY s, e
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ) AS previous_max_e
  FROM state_clip
),
state_marked AS (
  SELECT state, s, e,
         CASE WHEN previous_max_e IS NULL OR s > previous_max_e
              THEN 1 ELSE 0 END AS new_island
  FROM state_with_previous
),
state_grouped AS (
  SELECT state, s, e,
         SUM(new_island) OVER (
           PARTITION BY state ORDER BY s, e ROWS UNBOUNDED PRECEDING
         ) AS grp
  FROM state_marked
),
state_islands AS (
  SELECT state, grp, MIN(s) AS s, MAX(e) AS e
  FROM state_grouped
  GROUP BY state, grp
),
state_durations AS (
  SELECT state, SUM(EXTRACT(EPOCH FROM (e - s))) AS seconds
  FROM state_islands
  GROUP BY state
),
all_with_previous AS (
  SELECT s, e,
         MAX(e) OVER (
           ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
         ) AS previous_max_e
  FROM state_clip
),
all_marked AS (
  SELECT s, e,
         CASE WHEN previous_max_e IS NULL OR s > previous_max_e
              THEN 1 ELSE 0 END AS new_island
  FROM all_with_previous
),
all_grouped AS (
  SELECT s, e,
         SUM(new_island) OVER (ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS grp
  FROM all_marked
),
all_islands AS (
  SELECT grp, MIN(s) AS s, MAX(e) AS e
  FROM all_grouped
  GROUP BY grp
),
state_summary AS (
  SELECT
    (SELECT COUNT(*) FROM state_clip)::bigint AS state_event_count,
    COALESCE(ROUND(MAX(seconds) FILTER (WHERE state = 'active')), 0)::bigint
      AS active_seconds,
    COALESCE(ROUND(MAX(seconds) FILTER (WHERE state = 'idle')), 0)::bigint
      AS idle_seconds,
    COALESCE(ROUND(MAX(seconds) FILTER (WHERE state = 'display_off')), 0)::bigint
      AS display_off_seconds,
    COALESCE(ROUND(MAX(seconds) FILTER (WHERE state = 'lock')), 0)::bigint
      AS lock_seconds,
    COALESCE(ROUND(MAX(seconds) FILTER (WHERE state = 'sleep')), 0)::bigint
      AS sleep_seconds,
    COALESCE(ROUND(SUM(seconds)), 0)::bigint AS summed_state_seconds
  FROM state_durations
),
coverage_summary AS (
  SELECT COALESCE(ROUND(SUM(EXTRACT(EPOCH FROM (e - s)))), 0)::bigint
           AS recorded_coverage_seconds
  FROM all_islands
)
SELECT row_to_json(summary_row)
FROM (
  SELECT h.*, s.*,
         c.recorded_coverage_seconds,
         ROUND(EXTRACT(EPOCH FROM (b.t_to - b.t_from)))::bigint
           AS requested_window_seconds,
         GREATEST(
           ROUND(EXTRACT(EPOCH FROM (b.t_to - b.t_from)))::bigint
             - c.recorded_coverage_seconds,
           0
         )::bigint AS uncovered_seconds,
         GREATEST(
           s.summed_state_seconds - c.recorded_coverage_seconds,
           0
         )::bigint AS cross_state_overlap_seconds
  FROM hardware_summary h
  CROSS JOIN state_summary s
  CROSS JOIN coverage_summary c
  CROSS JOIN bounds b
) summary_row;
""".strip()


def query_aggregate(
    after_utc: dt.datetime,
    until_utc: dt.datetime,
    *,
    docker_executable: str | None = None,
    timeout_seconds: int = QUERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    docker = docker_executable or shutil.which("docker.exe") or shutil.which("docker")
    if not docker:
        raise RuntimeError("docker_unavailable")
    command = [
        docker,
        "exec",
        "-i",
        CONTAINER_NAME,
        "psql",
        "-U",
        "leyang",
        "-d",
        "time_audit",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"after_utc={format_utc(after_utc)}",
        "-v",
        f"until_utc={format_utc(until_utc)}",
    ]
    try:
        completed = subprocess.run(
            command,
            input=build_aggregate_sql(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout_seconds,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("query_timeout") from None
    except OSError:
        raise RuntimeError("query_unavailable") from None
    if completed.returncode != 0:
        raise RuntimeError("query_failed")
    stdout = completed.stdout.strip()
    if not stdout or len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise RuntimeError("query_output_invalid")
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("query_output_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("query_output_invalid")
    return value


def _int(value: Any) -> int:
    return int(value or 0)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return round(parsed, 3) if math.isfinite(parsed) else None


def _optional_utc(value: Any) -> str | None:
    return None if value is None else format_utc(parse_utc(str(value)))


def _range(average: Any, maximum: Any) -> dict[str, float | None]:
    return {"average": _float(average), "maximum": _float(maximum)}


def build_summary(
    aggregate: dict[str, Any],
    *,
    after_utc: dt.datetime,
    until_utc: dt.datetime,
    generated_at_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at_utc or dt.datetime.now(UTC)
    sample_count = _int(aggregate.get("hardware_sample_count"))
    first_sample = _optional_utc(aggregate.get("first_sample_utc"))
    last_sample = _optional_utc(aggregate.get("last_sample_utc"))
    latest_age_seconds: float | None = None
    if last_sample is not None:
        latest_age_seconds = round(
            max(0.0, (until_utc - parse_utc(last_sample)).total_seconds()), 3
        )
    if sample_count == 0:
        coverage_status = "empty"
    elif latest_age_seconds is not None and latest_age_seconds <= FRESHNESS_SECONDS:
        coverage_status = "fresh"
    else:
        coverage_status = "stale"
    start_gap_seconds: float | None = None
    end_gap_seconds: float | None = None
    max_gap_seconds: float | None = None
    if sample_count and first_sample is not None and last_sample is not None:
        start_gap_seconds = round(
            max(0.0, (parse_utc(first_sample) - after_utc).total_seconds()), 3
        )
        end_gap_seconds = round(
            max(0.0, (until_utc - parse_utc(last_sample)).total_seconds()), 3
        )
        internal_gap = _float(aggregate.get("max_internal_gap_seconds")) or 0.0
        max_gap_seconds = round(
            max(start_gap_seconds, end_gap_seconds, internal_gap), 3
        )

    signal_specs = (
        ("cpu_thermal_occurrences", "critical", "cpu_thermal_samples", "cpu_temp_c_gte_95"),
        ("gpu_thermal_occurrences", "critical", "gpu_thermal_samples", "gpu_core_c_gte_90_or_hotspot_gte_105"),
        ("memory_pressure_occurrences", "warning", "memory_pressure_samples", "ram_usage_pct_gte_95"),
        ("storage_latency_occurrences", "warning", "storage_latency_samples", "disk_latency_ms_gte_1000"),
        ("telemetry_out_of_bounds", "warning", "telemetry_out_of_bounds_samples", "timeaudit_physical_bounds_v1"),
        ("packet_loss_occurrences", "warning", "packet_loss_samples", "packet_loss_observed"),
        ("frametime_spike_occurrences", "warning", "frametime_spike_samples", "valid_game_frametime_ms_gte_50"),
    )
    signals = []
    for signal_id, severity, source_key, threshold_ref in signal_specs:
        count = _int(aggregate.get(source_key))
        if count:
            signals.append(
                {
                    "signal_id": signal_id,
                    "severity": severity,
                    "sample_count": count,
                    "threshold_ref": threshold_ref,
                    "evidence_kind": "threshold_occurrence",
                }
            )

    fps_positive_samples = _int(aggregate.get("fps_positive_sample_count"))
    fps_samples = _int(aggregate.get("fps_sample_count"))
    fps_rejected_samples = max(0, fps_positive_samples - fps_samples)
    recorded = _int(aggregate.get("recorded_coverage_seconds"))
    overlap = _int(aggregate.get("cross_state_overlap_seconds"))
    return {
        "schema": SCHEMA,
        "status": "ok",
        "owner_ref": OWNER_REF,
        "generated_at_utc": format_utc(generated),
        "window": {
            "after_exclusive_utc": format_utc(after_utc),
            "until_inclusive_utc": format_utc(until_utc),
            "maximum_window_hours": MAX_WINDOW_HOURS,
            "requested_seconds": _int(aggregate.get("requested_window_seconds")),
            "hardware_sample_count": sample_count,
            "first_sample_utc": first_sample,
            "last_sample_utc": last_sample,
            "latest_sample_age_seconds": latest_age_seconds,
            "window_start_gap_seconds": start_gap_seconds,
            "window_end_gap_seconds": end_gap_seconds,
            "max_gap_seconds": max_gap_seconds,
            "coverage_status": coverage_status,
        },
        "hardware": {
            "cpu": {
                "usage_pct": _range(aggregate.get("cpu_usage_avg_pct"), aggregate.get("cpu_usage_max_pct")),
                "package_temp_c": _range(aggregate.get("cpu_temp_avg_c"), aggregate.get("cpu_temp_max_c")),
                "package_power_w": _range(aggregate.get("cpu_power_avg_w"), aggregate.get("cpu_power_max_w")),
            },
            "gpu": {
                "usage_pct": _range(aggregate.get("gpu_usage_avg_pct"), aggregate.get("gpu_usage_max_pct")),
                "core_temp_c": _range(aggregate.get("gpu_temp_avg_c"), aggregate.get("gpu_temp_max_c")),
                "hotspot_max_c": _float(aggregate.get("gpu_hotspot_max_c")),
                "board_power_w": _range(aggregate.get("gpu_power_avg_w"), aggregate.get("gpu_power_max_w")),
            },
            "memory_usage_pct": _range(aggregate.get("ram_usage_avg_pct"), aggregate.get("ram_usage_max_pct")),
            "disk_latency_ms": {
                "average": _float(aggregate.get("disk_latency_avg_ms")),
                "p95": _float(aggregate.get("disk_latency_p95_ms")),
                "maximum": _float(aggregate.get("disk_latency_max_ms")),
            },
            "network": {
                "ping_ms": _range(aggregate.get("network_ping_avg_ms"), aggregate.get("network_ping_max_ms")),
                "packet_loss_samples": _int(aggregate.get("packet_loss_samples")),
            },
        },
        "game_performance": {
            "status": "observed" if fps_samples else "no_game_frames",
            "quality": (
                "mixed_valid_and_rejected"
                if fps_samples and fps_rejected_samples
                else "consistent"
                if fps_samples
                else "rejected_only"
                if fps_positive_samples
                else "no_game_frames"
            ),
            "positive_frame_samples": fps_positive_samples,
            "valid_frame_samples": fps_samples,
            "rejected_positive_samples": fps_rejected_samples,
            "fps_average": _float(aggregate.get("fps_avg")),
            "fps_minimum": _float(aggregate.get("fps_min")),
            "one_percent_low_fps_average": _float(aggregate.get("fps_one_percent_low_avg")),
            "frametime_p95_ms": _float(aggregate.get("frametime_p95_ms")),
            "frametime_max_ms": _float(aggregate.get("frametime_max_ms")),
            "frametime_spike_samples": _int(aggregate.get("frametime_spike_samples")),
        },
        "activity_state": {
            "event_count": _int(aggregate.get("state_event_count")),
            "durations_seconds": {
                "active": _int(aggregate.get("active_seconds")),
                "idle": _int(aggregate.get("idle_seconds")),
                "display_off": _int(aggregate.get("display_off_seconds")),
                "lock": _int(aggregate.get("lock_seconds")),
                "sleep": _int(aggregate.get("sleep_seconds")),
            },
            "recorded_coverage_seconds": recorded,
            "uncovered_seconds": _int(aggregate.get("uncovered_seconds")),
            "cross_state_overlap_seconds": overlap,
            "quality": "overlap_detected" if overlap else "consistent",
        },
        "signals": signals,
        "interpretation": {
            "causality": "correlation_only",
            "data_gap_meaning": "sleep_power_off_or_collection_gap",
            "scheduler_jitter_meaning": "not_kernel_dpc_latency",
            "fps_validity": "positive_plausible_and_fps_frametime_consistent",
        },
        "privacy": {
            "raw_samples_included": False,
            "process_names_included": False,
            "process_paths_included": False,
            "window_titles_included": False,
            "command_lines_included": False,
            "remote_addresses_included": False,
            "database_credentials_included": False,
            "machine_identifiers_included": False,
        },
    }


def unavailable_summary(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "owner_ref": OWNER_REF,
        "reason": reason,
        "privacy": {
            "raw_samples_included": False,
            "process_names_included": False,
            "process_paths_included": False,
            "window_titles_included": False,
            "command_lines_included": False,
            "remote_addresses_included": False,
            "database_credentials_included": False,
            "machine_identifiers_included": False,
        },
    }


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RuntimeError("argument_invalid")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = JsonArgumentParser()
    parser.add_argument("--after-utc")
    parser.add_argument("--until-utc")
    parser.add_argument("--hours", type=float)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        now = dt.datetime.now(UTC)
        if args.hours is not None and (args.after_utc or args.until_utc):
            raise RuntimeError("window_argument_conflict")
        if args.hours is not None:
            if args.hours <= 0 or args.hours > MAX_WINDOW_HOURS:
                raise RuntimeError("window_hours_invalid")
            until_utc = now
            after_utc = until_utc - dt.timedelta(hours=args.hours)
        else:
            until_utc = parse_utc(args.until_utc) if args.until_utc else now
            after_utc = (
                parse_utc(args.after_utc)
                if args.after_utc
                else until_utc - dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS)
            )
        if until_utc <= after_utc:
            raise RuntimeError("window_order_invalid")
        if until_utc > now + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise RuntimeError("window_future_invalid")
        if until_utc - after_utc > dt.timedelta(hours=MAX_WINDOW_HOURS):
            raise RuntimeError("window_too_large")
        aggregate = query_aggregate(after_utc, until_utc)
        result = build_summary(
            aggregate,
            after_utc=after_utc,
            until_utc=until_utc,
            generated_at_utc=now,
        )
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0
    except (RuntimeError, ValueError) as exc:
        reason = str(exc)
        allowed = {
            "docker_unavailable",
            "argument_invalid",
            "query_failed",
            "query_output_invalid",
            "query_timeout",
            "query_unavailable",
            "timestamp_timezone_required",
            "window_future_invalid",
            "window_argument_conflict",
            "window_hours_invalid",
            "window_order_invalid",
            "window_too_large",
        }
        if reason not in allowed:
            reason = "internal_error"
        print(
            json.dumps(
                unavailable_summary(reason),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
