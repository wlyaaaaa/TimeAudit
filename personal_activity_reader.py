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
SUMMARY_SCHEMA = "timeaudit.personal-activity-summary.v1"
BEIJING = dt.timezone(dt.timedelta(hours=8))
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
    """Check access before reading, between chunks and before delivery.

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
    chunk_started = False

    def read_chunk(start, end):
        nonlocal sql_seconds, chunk_started
        # The entry check just authorized the range query and first chunk.
        # Recheck before each later chunk, including retries after a timeout.
        if chunk_started:
            check_personal_access()
        chunk_started = True
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


def _coverage(parts, after, until, row_count):
    """Report recorded interval unions; absence is an uncovered source window."""
    united = merge(parts)
    gaps = missing(united, after, until)
    observed = seconds(united)
    return {
        "row_count": row_count,
        "recorded_union_seconds": observed,
        "uncovered_seconds": seconds(gaps),
        "gap_count": len(gaps),
        "largest_gap_seconds": round(max(((e-s).total_seconds() for s, e in gaps), default=0), 6),
        "first_interval_utc": format_utc(united[0][0]) if united else None,
        "last_interval_utc": format_utc(united[-1][1]) if united else None,
        "overlapping_row_seconds": round(sum((e-s).total_seconds() for s, e in parts)-observed, 6),
    }


def _beijing_days(after, until):
    cursor = after
    while cursor < until:
        local = cursor.astimezone(BEIJING)
        next_midnight = dt.datetime.combine(local.date()+dt.timedelta(days=1), dt.time(), BEIJING)
        end = min(until, next_midnight.astimezone(UTC))
        yield local.date().isoformat(), cursor, end
        cursor = end


def _period_summary(rows, after, until, automation, top_n):
    sources = defaultdict(list)
    states = defaultdict(list)
    apps = defaultdict(lambda: {"parts": [], "row_count": 0})
    row_counts = defaultdict(int)
    anomalies = defaultdict(int)
    day_windows = list(_beijing_days(after, until))
    daily = [{"date_beijing": date, "after_utc": format_utc(start),
              "until_utc": format_utc(end), "window_seconds": round((end-start).total_seconds(), 6),
              "source_parts": defaultdict(list), "state_parts": defaultdict(list),
              "row_counts": defaultdict(int)} for date, start, end in day_windows]
    automation_parts = [(max(after, parse_utc(a["after_utc"])),
                         min(until, parse_utc(a["until_utc"]))) for a in automation]

    for row in rows:
        source = row["source"]
        row_counts[source] += 1
        start = parse_utc(row["start"])
        process = row.get("process_name") or "[unresolved_process]"
        state = STATE_NAMES.get(process, "app_observed") if source == "ahk" else "foreground"
        if row.get("end") is None:
            anomalies["open_foreground_rows_not_extrapolated"] += 1
            continue
        end = parse_utc(row["end"])
        if end <= start:
            anomalies[f"{source}_nonpositive_intervals"] += 1
            continue
        if source == "foreground" and row.get("duration_ms") is not None:
            if abs((end-start).total_seconds()*1000-row["duration_ms"]) > 1000:
                anomalies["foreground_duration_mismatch_rows"] += 1
        part = max(start, after), min(end, until)
        if part[1] <= part[0]:
            continue
        sources[source].append(part)
        if source == "ahk":
            states[state].append(part)
        if source == "foreground" or state == "app_observed":
            app = apps[(source, process)]
            app["parts"].append(part)
            app["row_count"] += 1
        for item, (_, day_start, day_end) in zip(daily, day_windows):
            s, e = max(part[0], day_start), min(part[1], day_end)
            if e > s:
                item["source_parts"][source].append((s, e))
                item["row_counts"][source] += 1
                if source == "ahk":
                    item["state_parts"][state].append((s, e))

    coverage = {source: _coverage(sources[source], after, until, row_counts[source])
                for source in ("foreground", "ahk")}
    coverage["ahk"]["explicit_collection_gap_seconds"] = seconds(states.get("collection_gap", []))
    coverage["ahk"]["non_gap_recorded_seconds"] = seconds(
        [part for state, parts in states.items() if state != "collection_gap" for part in parts])
    coverage["ahk"]["cross_state_overlap_seconds"] = round(
        sum(seconds(parts) for parts in states.values())-seconds(sources["ahk"]), 6)
    app_output = {}
    for source in ("foreground", "ahk"):
        ranked = []
        for (app_source, name), info in apps.items():
            if app_source == source:
                parts = info["parts"]
                ranked.append({"process_name": name, "row_count": info["row_count"],
                               "observed_union_seconds": seconds(parts),
                               "overlapping_row_seconds": round(sum((e-s).total_seconds() for s,e in parts)-seconds(parts), 6),
                               "known_automation_overlap_seconds": seconds(intersect(parts, automation_parts))})
        ranked.sort(key=lambda item: (-item["observed_union_seconds"], item["process_name"]))
        app_output[source] = {"total_application_count": len(ranked), "top_n": top_n,
                              "other_application_count": max(0, len(ranked)-top_n),
                              "top": ranked[:top_n]}
    days = []
    for item, (_, start, end) in zip(daily, day_windows):
        source_parts = item.pop("source_parts")
        state_parts = item.pop("state_parts")
        counts = item.pop("row_counts")
        item["coverage"] = {}
        for source in ("foreground", "ahk"):
            full = _coverage(source_parts[source], start, end, counts[source])
            item["coverage"][source] = {
                "intersecting_row_count": full["row_count"],
                "recorded_union_seconds": full["recorded_union_seconds"],
                "uncovered_seconds": full["uncovered_seconds"],
                "gap_count": full["gap_count"],
                "largest_gap_seconds": full["largest_gap_seconds"],
                "overlapping_row_seconds": full["overlapping_row_seconds"],
            }
        item["coverage"]["ahk"]["explicit_collection_gap_seconds"] = seconds(state_parts.get("collection_gap", []))
        item["coverage"]["ahk"]["non_gap_recorded_seconds"] = seconds(
            [part for state, parts in state_parts.items() if state != "collection_gap" for part in parts])
        item["states"] = {state: {"recorded_union_seconds": seconds(parts),
                                   "interval_count": len(merge(parts))}
                          for state, parts in sorted(state_parts.items())}
        days.append(item)
    return {"period": {"window_seconds": round((until-after).total_seconds(), 6),
                       "coverage": coverage,
                       "states": {state: {"recorded_union_seconds": seconds(parts),
                                           "interval_count": len(merge(parts))}
                                  for state, parts in sorted(states.items())},
                       "applications": app_output, "anomalies": dict(anomalies)},
            "days": days}


def read_activity_summary(after, until, *, automation=(), top_n=10, query_fn=query):
    """Compact complete-window observation, with Beijing calendar-day detail.

    Native rows are deduplicated across disjoint SQL windows before aggregation.
    The legacy read_activity API remains available for full group/source evidence.
    """
    if after.tzinfo is None or until.tzinfo is None or until <= after:
        raise ValueError("explicit_ordered_timezone_aware_window_required")
    after, until = after.astimezone(UTC), until.astimezone(UTC)
    if until > dt.datetime.now(UTC):
        raise ValueError("future_window_not_observed")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("positive_top_n_required")
    if not isinstance(automation, (list, tuple)):
        raise ValueError("automation_context_array_required")
    for item in automation:
        if not isinstance(item, dict) or not all(item.get(k) for k in ("after_utc", "until_utc", "source_ref")):
            raise ValueError("automation_interval_and_source_ref_required")
        if parse_utc(item["until_utc"]) <= parse_utc(item["after_utc"]):
            raise ValueError("invalid_automation_window")
    started = time.monotonic()
    sql_seconds = 0.0
    check_personal_access()
    tick = time.monotonic()
    retention = query_fn(RANGE_SQL, after, until)[0]
    sql_seconds += time.monotonic()-tick
    seen = set()
    batch_count = 0

    def read_batch(start, end):
        nonlocal sql_seconds, batch_count
        check_personal_access()
        tick = time.monotonic()
        try:
            batch = query_fn(SQL, start, end)
        except RuntimeError as exc:
            sql_seconds += time.monotonic()-tick
            if str(exc) in {"query_timeout", "query_output_too_large"} and end-start > dt.timedelta(hours=1):
                middle = start+(end-start)/2
                yield from read_batch(start, middle)
                yield from read_batch(middle, end)
                return
            raise
        sql_seconds += time.monotonic()-tick
        batch_count += 1
        for row in batch:
            key = (row["source"], json.dumps(row["id"], sort_keys=True, ensure_ascii=False))
            if key not in seen:
                seen.add(key)
                yield row

    def iter_rows():
        cursor = after
        while cursor < until:
            end = min(cursor+dt.timedelta(days=7), until)
            yield from read_batch(cursor, end)
            cursor = end

    summary = _period_summary(iter_rows(), after, until, automation, top_n)
    check_personal_access()
    return {"schema": SUMMARY_SCHEMA, "status": "ok", "after_utc": format_utc(after),
            "until_utc": format_utc(until),
            "observation": {"queried_at_utc": format_utc(dt.datetime.now(UTC)),
                            "reader_elapsed_seconds": round(time.monotonic()-started, 3),
                            "query_elapsed_seconds": round(sql_seconds, 3),
                            "sql_batch_count": batch_count, "native_row_count": len(seen),
                            "retained_start_bounds": retention,
                            "retention_completeness": "unknown; bounds do not prove continuous collection",
                            "snapshot": "independent read-only queries; open sessions can later close"},
            "semantics": {"actor_role": "device_observation_not_person_presence",
                          "coverage": "per-source union of recorded half-open intervals; uncovered means no interval from that source, not proof of device inactivity",
                          "states": "AHK collector categories remain distinct; state unions may overlap and must not be added",
                          "physical_idle": "60s threshold in current collector, suppressed while system audio plays; per-row exemption and historical version unknown",
                          "no_foreground_response": "Idle means no responsive focused window, not physical idle",
                          "foreground": "focus intervals; end timestamp authoritative; open duration unknown",
                          "days": "Asia/Shanghai UTC+08 calendar days; intervals clipped at local midnight; intersecting_row_count may repeat a row on adjacent days",
                          "applications": "separate source rankings by observed interval union; top N is partial, other_application_count names omitted groups; groups are not person time",
                          "automation": "only supplied known intervals annotated; absence does not establish human operation",
                          "detail": "read_activity(after, until) retains grouped source evidence on demand",
                          "content": "no titles or command lines selected; no attention, effective-work or personality scores"},
            "known_automation": list(automation), **summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", required=True, help="inclusive ISO timestamp with timezone")
    parser.add_argument("--until", required=True, help="exclusive ISO timestamp with timezone")
    parser.add_argument("--automation-context", type=Path,
                        help="optional JSON array of known after_utc/until_utc/source_ref intervals")
    parser.add_argument("--summary", action="store_true",
                        help="compact complete-window and Beijing calendar-day observation")
    parser.add_argument("--top-n", type=int, default=10,
                        help="applications per source in summary mode (default: 10)")
    parser.add_argument("--output", type=Path, help="private local result; otherwise JSON stdout")
    args = parser.parse_args(argv)
    try:
        automation = []
        if args.automation_context:
            check_personal_access()
            automation = json.loads(args.automation_context.read_text(encoding="utf-8"))
        if args.summary:
            result = read_activity_summary(parse_utc(args.after), parse_utc(args.until),
                                           automation=automation, top_n=args.top_n)
        else:
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
        failure = {"schema": SUMMARY_SCHEMA if args.summary else SCHEMA,
                   "status": "error", "reason": reason}
        if isinstance(exc, PersonalDataAccessBlocked) and isinstance(exc.decision.get("diagnostic"), dict):
            failure["diagnostic"] = exc.decision["diagnostic"]
        print(json.dumps(failure))
        return 1


if __name__ == "__main__":
    sys.exit(main())
