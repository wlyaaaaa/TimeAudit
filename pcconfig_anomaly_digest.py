"""Bounded, read-only TimeAudit anomaly digest for PCConfig consumers.

This provider queries only aggregate facts from fact_system_hardware through the
owner's existing PostgreSQL container. It never returns raw samples, process
activity, window titles, database credentials, or machine identifiers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


UTC = dt.timezone.utc
SCHEMA = "timeaudit.pcconfig-anomaly-digest.v1"
OWNER_REF = "timeaudit:hardware-telemetry"
PROFILE = "timeaudit:pcconfig-hardware-anomaly.v1"
DEFAULT_LOOKBACK_HOURS = 24
MAX_WINDOW_HOURS = 168
MAX_CLOCK_SKEW_SECONDS = 300
FRESHNESS_SECONDS = 60


@dataclass(frozen=True)
class Rule:
    anomaly_id: str
    severity: str
    condition: str
    minimum_samples: int
    projection_recheck_recommended: bool
    threshold_ref: str


RULES = (
    Rule(
        "cpu_thermal_pressure",
        "critical",
        "cpu_package_temp >= 95 AND cpu_package_temp <= 120",
        10,
        True,
        "cpu_package_temp_celsius_gte_95_for_10_samples",
    ),
    Rule(
        "gpu_thermal_pressure",
        "critical",
        "((gpu_core_temp >= 90 AND gpu_core_temp <= 120) "
        "OR (gpu_hotspot_temp >= 105 AND gpu_hotspot_temp <= 130))",
        10,
        True,
        "gpu_core_celsius_gte_90_or_hotspot_gte_105_for_10_samples",
    ),
    Rule(
        "memory_pressure",
        "warning",
        "system_ram_usage_pct >= 95 AND system_ram_usage_pct <= 100",
        20,
        True,
        "system_ram_usage_pct_gte_95_for_20_samples",
    ),
    Rule(
        "storage_latency_pressure",
        "warning",
        "disk_max_latency_ms >= 1000 AND disk_max_latency_ms <= 600000",
        5,
        True,
        "disk_max_latency_ms_gte_1000_for_5_samples",
    ),
    Rule(
        "scheduler_jitter_saturation",
        "warning",
        "system_dpc_latency >= 100000 AND system_dpc_latency <= 100000",
        20,
        False,
        "bounded_user_space_scheduler_jitter_us_eq_100000_for_20_samples",
    ),
    Rule(
        "telemetry_out_of_bounds",
        "warning",
        "(cpu_total_usage < 0 OR cpu_total_usage > 100 "
        "OR gpu_usage < 0 OR gpu_usage > 100 "
        "OR system_ram_usage_pct < 0 OR system_ram_usage_pct > 100 "
        "OR cpu_package_temp < 0 OR cpu_package_temp > 120 "
        "OR gpu_core_temp < 0 OR gpu_core_temp > 120 "
        "OR cpu_package_power < 0 OR gpu_board_power < 0 "
        "OR disk_max_latency_ms < 0 OR system_dpc_latency < 0)",
        1,
        False,
        "timeaudit_hardware_physical_bounds_v1",
    ),
)


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
    columns = [
        "COUNT(*) AS sample_count",
        'MIN("timestamp") AS first_sample_utc',
        'MAX("timestamp") AS last_sample_utc',
    ]
    for rule in RULES:
        columns.extend(
            (
                f"COUNT(*) FILTER (WHERE {rule.condition}) "
                f"AS {rule.anomaly_id}_count",
                f'MIN("timestamp") FILTER (WHERE {rule.condition}) '
                f"AS {rule.anomaly_id}_first",
                f'MAX("timestamp") FILTER (WHERE {rule.condition}) '
                f"AS {rule.anomaly_id}_last",
            )
        )
    return (
        "SELECT row_to_json(aggregate_row) FROM (SELECT\n  "
        + ",\n  ".join(columns)
        + '\nFROM public.fact_system_hardware\nWHERE "timestamp" > '
        + ":'after_utc'::timestamptz\n"
        + '  AND "timestamp" <= :\'until_utc\'::timestamptz'
        + "\n) AS aggregate_row;"
    )


def query_aggregate(
    after_utc: dt.datetime,
    until_utc: dt.datetime,
    *,
    docker_executable: str | None = None,
    container_name: str = "audit-postgres",
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    docker = docker_executable or shutil.which("docker.exe") or shutil.which(
        "docker"
    )
    if not docker:
        raise RuntimeError("docker_unavailable")
    command = [
        docker,
        "exec",
        "-i",
        container_name,
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = (
            "query_timeout"
            if isinstance(exc, subprocess.TimeoutExpired)
            else "query_unavailable"
        )
        raise RuntimeError(reason) from None
    if completed.returncode != 0:
        raise RuntimeError("query_failed")
    stdout = completed.stdout.strip()
    if not stdout or len(stdout.encode("utf-8")) > 1_048_576:
        raise RuntimeError("query_output_invalid")
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("query_output_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("query_output_invalid")
    return value


def _optional_utc(value: Any) -> str | None:
    if value is None:
        return None
    return format_utc(parse_utc(str(value)))


def build_digest(
    aggregate: dict[str, Any],
    *,
    after_utc: dt.datetime,
    until_utc: dt.datetime,
    generated_at_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at_utc or dt.datetime.now(UTC)
    sample_count = int(aggregate.get("sample_count") or 0)
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

    anomalies: list[dict[str, Any]] = []
    for rule in RULES:
        count = int(aggregate.get(f"{rule.anomaly_id}_count") or 0)
        if count < rule.minimum_samples:
            continue
        anomalies.append(
            {
                "anomaly_id": rule.anomaly_id,
                "severity": rule.severity,
                "sample_count": count,
                "first_seen_utc": _optional_utc(
                    aggregate.get(f"{rule.anomaly_id}_first")
                ),
                "last_seen_utc": _optional_utc(
                    aggregate.get(f"{rule.anomaly_id}_last")
                ),
                "threshold_ref": rule.threshold_ref,
                "projection_recheck_recommended": (
                    rule.projection_recheck_recommended
                ),
            }
        )
    if coverage_status in {"empty", "stale"}:
        anomalies.append(
            {
                "anomaly_id": "telemetry_gap",
                "severity": "warning",
                "sample_count": 0,
                "first_seen_utc": None,
                "last_seen_utc": last_sample,
                "threshold_ref": "latest_sample_age_seconds_lte_60",
                "projection_recheck_recommended": False,
            }
        )
    anomalies.sort(key=lambda item: item["anomaly_id"])
    critical_count = sum(
        1 for item in anomalies if item["severity"] == "critical"
    )
    warning_count = sum(
        1 for item in anomalies if item["severity"] == "warning"
    )
    recheck = any(
        bool(item["projection_recheck_recommended"]) for item in anomalies
    )
    return {
        "schema": SCHEMA,
        "status": "ok",
        "owner_ref": OWNER_REF,
        "threshold_profile": PROFILE,
        "generated_at_utc": format_utc(generated),
        "window": {
            "after_exclusive_utc": format_utc(after_utc),
            "until_inclusive_utc": format_utc(until_utc),
            "maximum_window_hours": MAX_WINDOW_HOURS,
            "sample_count": sample_count,
            "first_sample_utc": first_sample,
            "last_sample_utc": last_sample,
            "latest_sample_age_seconds": latest_age_seconds,
            "coverage_status": coverage_status,
        },
        "anomalies": anomalies,
        "summary": {
            "anomaly_count": len(anomalies),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "projection_recheck_recommended": recheck,
        },
        "cursor": {
            "kind": "timestamp_exclusive",
            "next_after_utc": format_utc(until_utc),
        },
        "privacy": {
            "raw_samples_included": False,
            "process_activity_included": False,
            "window_titles_included": False,
            "database_credentials_included": False,
            "machine_identifiers_included": False,
        },
    }


def unavailable_digest(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "owner_ref": OWNER_REF,
        "reason": reason,
        "privacy": {
            "raw_samples_included": False,
            "process_activity_included": False,
            "window_titles_included": False,
            "database_credentials_included": False,
            "machine_identifiers_included": False,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-utc")
    parser.add_argument("--until-utc")
    parser.add_argument("--container-name", default="audit-postgres")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        now = dt.datetime.now(UTC)
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
        aggregate = query_aggregate(
            after_utc,
            until_utc,
            container_name=args.container_name,
        )
        result = build_digest(
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
            )
        )
        return 0
    except (RuntimeError, ValueError) as exc:
        reason = str(exc)
        allowed = {
            "docker_unavailable",
            "query_failed",
            "query_output_invalid",
            "query_timeout",
            "query_unavailable",
            "timestamp_timezone_required",
            "window_future_invalid",
            "window_order_invalid",
            "window_too_large",
        }
        if reason not in allowed:
            reason = "internal_error"
        print(
            json.dumps(
                unavailable_digest(reason),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
