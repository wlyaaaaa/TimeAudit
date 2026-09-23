"""Read existing activity evidence without inferring human presence or productivity.

The public business entry checks the existing shared personal-data access state.
No collector, database, or authorization state is created or changed here.
Raw titles and command lines are never selected.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from timeaudit_diagnostic_summary import parse_utc, format_utc

UTC = dt.timezone.utc
SCHEMA = "timeaudit.personal-activity.v1"
STATE_NAMES = {
    "System_CollectionGap": "collection_gap", "System_Sleep": "sleep",
    "System_DisplayOff": "display_off", "System_LockScreen": "lock",
    "LockApp.exe": "lock", "LogonUI.exe": "lock",
    "System_Idle": "physical_idle", "Idle": "no_foreground_response",
    "System_Hung": "foreground_hung", "Unknown": "capture_unknown",
}

SQL = r"""
WITH b AS (SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e)
SELECT json_build_object('source','foreground','id',
  json_build_object('timestamp',c.timestamp,'process_key',c.process_key,'os_pid',c.os_pid),
  'start',c.timestamp,'end',c.end_timestamp,'duration_ms',c.duration_ms,
  'process_name',r.process_name,'window_mode',c.window_mode)
FROM public.fact_process_context c LEFT JOIN public.dim_process_registry r USING(process_key), b
WHERE c.is_foreground=1 AND c.timestamp < b.e
  AND (c.end_timestamp > b.s OR (c.timestamp >= b.s AND
       (c.end_timestamp IS NULL OR c.end_timestamp <= c.timestamp)))
UNION ALL
SELECT json_build_object('source','ahk','id',json_build_object('id',a.id),
  'start',a.start_time,'end',a.start_time + a.duration_seconds * interval '1 second',
  'process_name',a.process_name)
FROM public.app_usage_logs a, b
WHERE a.start_time < b.e AND
  (a.start_time + a.duration_seconds * interval '1 second' > b.s OR
   (a.start_time >= b.s AND a.duration_seconds <= 0));
"""

RANGE_SQL = r"""
SELECT json_build_object(
 'foreground',json_build_object(
   'first_start',(SELECT min(timestamp) FROM public.fact_process_context WHERE is_foreground=1),
   'last_start',(SELECT max(timestamp) FROM public.fact_process_context WHERE is_foreground=1),
   'unclosed_before_window_count',(SELECT count(*) FROM public.fact_process_context
      WHERE is_foreground=1 AND end_timestamp IS NULL AND timestamp < :'after_utc'::timestamptz)),
 'ahk',json_build_object(
   'first_start',(SELECT min(start_time) FROM public.app_usage_logs),
   'last_start',(SELECT max(start_time) FROM public.app_usage_logs)));
"""


def query(sql, after, until):
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if not docker:
        raise RuntimeError("docker_unavailable")
    command = [docker, "exec", "-i", "-e",
               "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=20000 -c lock_timeout=1000",
               "audit-postgres", "psql", "-X", "-U", "leyang", "-d", "time_audit",
               "-At", "-v", "ON_ERROR_STOP=1", "-v", f"after_utc={format_utc(after)}",
               "-v", f"until_utc={format_utc(until)}"]
    try:
        result = subprocess.run(command, input=sql, capture_output=True, text=True,
                                encoding="utf-8", timeout=25,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("query_timeout") from None
    if result.returncode:
        if "statement timeout" in result.stderr:
            raise RuntimeError("query_timeout")
        raise RuntimeError("query_failed")  # Do not echo database content/errors.
    if len(result.stdout.encode("utf-8")) > 32 * 1024 * 1024:
        raise RuntimeError("query_output_too_large")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def seconds(intervals):
    return round(sum((end - start).total_seconds() for start, end in merge(intervals)), 6)


def intersect(left, right):
    left, right = merge(left), merge(right)
    i = j = 0
    result = []
    while i < len(left) and j < len(right):
        start, end = max(left[i][0], right[j][0]), min(left[i][1], right[j][1])
        if end > start:
            result.append((start, end))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return result


def missing(intervals, start, end):
    result, cursor = [], start
    for s, e in merge(intervals):
        if s > cursor:
            result.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        result.append((cursor, end))
    return result


def summarize(rows, after, until, automation=()):
    groups = {}
    all_intervals = defaultdict(list)
    states = defaultdict(list)
    counts = defaultdict(int)
    anomalies = defaultdict(int)
    for row in rows:
        source = row["source"]
        counts[source] += 1
        start = parse_utc(row["start"])
        process = row.get("process_name") or "[unresolved_process]"
        state = STATE_NAMES.get(process, "app_observed") if source == "ahk" else "foreground"
        key = (source, process, state)
        group = groups.setdefault(key, {"source": source, "process_name": process, "state": state,
                                       "row_count": 0, "open_row_count": 0,
                                       "intervals": [], "window_modes": set(), "refs": []})
        group["row_count"] += 1
        # Only two representative exact native keys; the complete selection is
        # reproducible from source, process and the requested half-open window.
        ref = {"table": "fact_process_context" if source == "foreground" else "app_usage_logs",
               "key": row["id"]}
        group["refs"].append((start, ref))
        if row.get("window_mode") is not None:
            group["window_modes"].add(row["window_mode"])
        if row.get("end") is None:
            group["open_row_count"] += 1
            anomalies["open_foreground_rows_not_extrapolated"] += 1
            continue
        end = parse_utc(row["end"])
        if end <= start:
            anomalies[f"{source}_nonpositive_intervals"] += 1
            continue
        if source == "foreground" and row.get("duration_ms") is not None:
            if abs((end-start).total_seconds()*1000-row["duration_ms"]) > 1000:
                anomalies["foreground_duration_mismatch_rows"] += 1
        s, e = max(start, after), min(end, until)
        if e <= s:
            continue
        group["intervals"].append((s, e))
        all_intervals[source].append((s, e))
        if source == "ahk":
            states[state].append((s, e))

    automation_intervals = [(max(after, parse_utc(a["after_utc"])),
                             min(until, parse_utc(a["until_utc"]))) for a in automation]
    output = []
    for group in groups.values():
        intervals = group.pop("intervals")
        refs = sorted(group.pop("refs"), key=lambda item: item[0])
        group["representative_refs"] = [refs[0][1]] + ([refs[-1][1]] if len(refs) > 1 else [])
        group["window_modes"] = sorted(group["window_modes"])
        group["observed_seconds"] = seconds(intervals)
        group["summed_row_seconds"] = round(sum((e-s).total_seconds() for s,e in intervals), 6)
        group["known_automation_overlap_seconds"] = seconds(intersect(intervals, automation_intervals))
        if group["source"] == "foreground":
            group["ahk_state_overlap_seconds"] = {
                state: seconds(intersect(intervals, parts)) for state, parts in sorted(states.items())}
        group["first_observed_utc"] = format_utc(min(s for s,e in intervals)) if intervals else None
        group["last_observed_utc"] = format_utc(max(e for s,e in intervals)) if intervals else None
        output.append(group)

    coverage = {}
    for source in ("foreground", "ahk"):
        parts = all_intervals[source]
        gaps = missing(parts, after, until)
        merged = merge(parts)
        coverage[source] = {
            "row_count": counts[source], "recorded_union_seconds": seconds(parts),
            "uncovered_seconds": seconds(gaps), "gap_count": len(gaps),
            "largest_gap_seconds": max(((e-s).total_seconds() for s,e in gaps), default=0),
            "first_interval_utc": format_utc(merged[0][0]) if merged else None,
            "last_interval_utc": format_utc(merged[-1][1]) if merged else None,
            "overlapping_row_seconds": round(sum((e-s).total_seconds() for s,e in parts)-seconds(parts), 6),
        }
    # Gap markers are persisted records, but they do not establish capture coverage.
    coverage["ahk"]["explicit_collection_gap_seconds"] = seconds(states["collection_gap"])
    coverage["ahk"]["non_gap_recorded_seconds"] = seconds(
        [part for state, parts in states.items() if state != "collection_gap" for part in parts])
    coverage["ahk"]["cross_state_overlap_seconds"] = round(
        sum(seconds(parts) for parts in states.values())-seconds(all_intervals["ahk"]), 6)
    return {"after_utc": format_utc(after), "until_utc": format_utc(until),
            "coverage": coverage, "anomalies": dict(anomalies),
            "groups": sorted(output, key=lambda g: (g["source"], g["process_name"]))}


class PersonalDataAccessBlocked(RuntimeError):
    """Carry the owner's closed result without retaining subprocess output."""
    def __init__(self, decision):
        super().__init__("personal_access_blocked:" + str(decision.get("reason", "unknown")))
        self.decision = decision


def check_personal_access():
    path = Path(r"C:\ProgramData\PCConfig\AuthorityHost\tools\personal_data_access.py")
    spec = importlib.util.spec_from_file_location("_timeaudit_personal_access", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("personal_access_adapter_unavailable")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    result = adapter.check_access("factor")
    if not isinstance(result, dict):
        raise RuntimeError("personal_access_blocked:invalid_result")
    if result.get("status") != "pass":
        raise PersonalDataAccessBlocked(result)


def read_activity(after, until, *, automation=(), query_fn=query):
    """Check access for each query; split large windows, never truncate a month.

    Uses the existing shared personal-data business check without a second lease.
    """
    if after.tzinfo is None or until.tzinfo is None or until <= after:
        raise ValueError("explicit_ordered_timezone_aware_window_required")
    after, until = after.astimezone(UTC), until.astimezone(UTC)
    if until > dt.datetime.now(UTC):
        raise ValueError("future_window_not_observed")
    if not isinstance(automation, (list, tuple)):
        raise ValueError("automation_context_array_required")
    for item in automation:
        if not isinstance(item, dict) or not all(item.get(k) for k in ("after_utc", "until_utc", "source_ref")):
            raise ValueError("automation_interval_and_source_ref_required")
        if parse_utc(item["until_utc"]) <= parse_utc(item["after_utc"]):
            raise ValueError("invalid_automation_window")
    started = time.monotonic()
    check_personal_access()
    query_started = time.monotonic()
    retention = query_fn(RANGE_SQL, after, until)[0]
    sql_seconds = time.monotonic()-query_started
    chunks = []

    def read_chunk(start, end):
        nonlocal sql_seconds
        check_personal_access()
        tick = time.monotonic()
        try:
            rows = query_fn(SQL, start, end)
        except RuntimeError as exc:
            sql_seconds += time.monotonic()-tick
            if str(exc) in {"query_timeout", "query_output_too_large"} and end-start > dt.timedelta(hours=1):
                middle = start+(end-start)/2
                read_chunk(start, middle)
                read_chunk(middle, end)
                return
            raise
        elapsed = time.monotonic()-tick
        sql_seconds += elapsed
        summary = summarize(rows, start, end, automation)
        summary["query_elapsed_seconds"] = round(elapsed, 3)
        chunks.append(summary)

    start = after
    while start < until:
        end = min(start+dt.timedelta(days=1), until)
        read_chunk(start, end)
        start = end
    check_personal_access()
    return {"schema": SCHEMA, "status": "ok", "after_utc": format_utc(after),
            "until_utc": format_utc(until), "observation": {
                "queried_at_utc": format_utc(dt.datetime.now(UTC)),
                "reader_elapsed_seconds": round(time.monotonic()-started, 3),
                "query_elapsed_seconds": round(sql_seconds, 3),
                "chunk_count": len(chunks), "retained_start_bounds": retention,
                "retention_completeness": "unknown; bounds do not prove continuous collection",
                "snapshot": "independent read-only queries; open sessions can later close"},
            "semantics": {
                "actor_role": "device_observation_not_person_presence",
                "foreground": "window focus intervals; end timestamp authoritative; open duration unknown",
                "ahk": "current collector: physical idle threshold 60s, suppressed when system audio plays; per-row audio exemption and historical collector version not stored",
                "no_foreground_response": "Idle denotes no responsive focused window, not physical idle",
                "automation": "only supplied known intervals annotated; absence does not establish human operation",
                "aggregation": "half-open intervals; union within groups; sources and apps must not be summed as person time",
                "content": "no titles or command lines selected; no attention, efficiency or personality scores",
            }, "known_automation": list(automation), "chunks": chunks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", required=True, help="inclusive ISO timestamp with timezone")
    parser.add_argument("--until", required=True, help="exclusive ISO timestamp with timezone")
    parser.add_argument("--automation-context", type=Path,
                        help="optional JSON array of known after_utc/until_utc/source_ref intervals")
    parser.add_argument("--output", type=Path, help="private local result; otherwise JSON stdout")
    args = parser.parse_args(argv)
    try:
        automation = []
        if args.automation_context:
            check_personal_access()
            automation = json.loads(args.automation_context.read_text(encoding="utf-8"))
        result = read_activity(parse_utc(args.after), parse_utc(args.until), automation=automation)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        # No subprocess stderr, private paths or raw database rows on failures.
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        failure = {"schema": SCHEMA, "status": "error", "reason": reason}
        if isinstance(exc, PersonalDataAccessBlocked) and isinstance(exc.decision.get("diagnostic"), dict):
            failure["diagnostic"] = exc.decision["diagnostic"]
        print(json.dumps(failure))
        return 1


if __name__ == "__main__":
    sys.exit(main())
