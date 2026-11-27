"""Foreground and frame evidence for trusted local live consumers."""
import datetime as dt
from functools import lru_cache
import json
from pathlib import Path, PureWindowsPath
import time

from game_catalog import classify
from personal_activity_reader import ActivityQueryError, missing, query
from timeaudit_diagnostic_summary import VALID_FRAME_SQL, format_utc, parse_utc

ROOT = Path(__file__).resolve().parent
FRESH = dt.timedelta(seconds=15)
DAY = dt.timedelta(days=1)

CURRENT_SQL = """
WITH b AS NOT MATERIALIZED (SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e),
  c AS MATERIALIZED (SELECT timestamp, end_timestamp, os_pid, process_key
    FROM public.fact_process_context, b
    WHERE timestamp >= b.e - interval '1 day' AND timestamp <= b.e AND is_foreground=1
    ORDER BY timestamp DESC LIMIT 1)
SELECT json_build_object('focus', (SELECT row_to_json(f) FROM (
    SELECT c.timestamp AS at, c.end_timestamp AS stop, c.os_pid AS pid, r.process_name AS name,
           r.executable_path AS path FROM c LEFT JOIN public.dim_process_registry r USING(process_key)) f),
  'frame', (SELECT row_to_json(f) FROM (
    SELECT h.timestamp AS at, h.current_fps AS fps, h.measurement_quality->'frame_source' AS source
    FROM public.fact_system_hardware h, b WHERE h.timestamp >= b.s AND h.timestamp <= b.e
      AND jsonb_typeof(h.measurement_quality->'frame_source'->'pid') = 'number'
      AND h.measurement_quality->'frame_source'->>'exe' <> '' AND ({valid_frame})
    ORDER BY h.timestamp DESC LIMIT 1) f));
""".replace("{valid_frame}", VALID_FRAME_SQL)

HISTORY_SQL = """
WITH b AS (SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e),
usage AS MATERIALIZED (
    SELECT greatest(a.start_time,b.s) AS s, least(a.start_time + a.duration_seconds * interval '1 second',b.e) AS e,
           lower(a.process_name) AS name FROM public.app_usage_logs a, b
    WHERE a.start_time >= b.s - interval '1 day' AND a.start_time < b.e AND a.duration_seconds > 0
      AND a.start_time + a.duration_seconds * interval '1 second' > b.s
),
checked AS (
    SELECT s,e,''::text AS path FROM usage
    WHERE name IN ('idle','system_idle','system_lockscreen','system_displayoff','system_sleep')
    UNION ALL
    SELECT greatest(u.s,c.timestamp), least(u.e,c.end_timestamp), r.executable_path
    FROM public.fact_process_context c JOIN public.dim_process_registry r USING(process_key)
    JOIN usage u ON u.name=lower(r.process_name) AND u.s<c.end_timestamp AND u.e>c.timestamp CROSS JOIN b
    WHERE c.timestamp >= b.s - interval '1 day' AND c.timestamp < b.e AND c.is_foreground=1
)
SELECT json_build_array(s,e,path) FROM checked WHERE e>s;
"""


def read_focus(until):
    try:
        health = json.loads((ROOT / "log/ahk_health.json").read_text(encoding="utf-8"))
        sampled, since = (dt.datetime.strptime(health[key], "%Y%m%d%H%M%S.%f").replace(tzinfo=dt.timezone.utc)
                          for key in ("focus_sample_utc", "focus_since_utc"))
        if until - FRESH <= sampled <= until and since <= sampled:
            return health, sampled, since
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}, None, None


@lru_cache(maxsize=2)
def read_history(bucket, timeout):
    until = dt.datetime.fromtimestamp(bucket * 15, tz=dt.timezone.utc)
    after = until - DAY
    rows = query(HISTORY_SQL, after, until, timeout=timeout)
    verdicts = {path: "non_game" if path == "" else classify(path)[0] for path in {r[2] for r in rows}}
    covered = [(parse_utc(start), parse_utc(stop)) for start, stop, path in rows if verdicts[path] != "unknown"]
    complete = not missing(covered, after, until)
    played = "game" in verdicts.values()
    return {"at": format_utc(until), "played": True if played else False if complete else None, "complete": complete}


def read_game_status(until=None, *, timeout=5):
    """Read source clocks with at most 15 seconds of live age; callers gate access."""
    until = until or dt.datetime.now(dt.timezone.utc)
    if until.utcoffset() is None:
        raise ValueError("until must include a timezone")
    deadline = time.monotonic() + timeout
    health, sampled, since = read_focus(until)
    foreground = frame = game_fps = None
    try:
        current, = query(CURRENT_SQL, until - FRESH, until, timeout=min(3, timeout))
    except ActivityQueryError:
        current = {}
    if sampled:
        focus = current.get("focus") or {}
        pid = health.get("focus_pid")
        matches = bool(pid and pid == focus.get("pid") and not focus.get("stop"))
        name = health.get("focus_process") or (focus.get("name") if matches else None)
        active = False if health.get("locked") or health.get("screen_off") else health.get("focus_active")
        same_name = name and name.casefold() == (focus.get("name") or "").casefold()
        category, title = classify(focus.get("path") if matches and same_name else None)
        if name or active is not None:
            foreground = dict(at=format_utc(sampled), process_name=PureWindowsPath(name).name if name else None,
                              classification=category, game_name=title, active=active)
    if (observation := current.get("frame")) and (source := observation["source"]):
        frame = dict(at=observation["at"], fps=observation["fps"], process_name=PureWindowsPath(source["exe"]).name)
        if (foreground and foreground["active"] is True and foreground["classification"] == "game"
                and source.get("foreground_match") is True and source["pid"] == health.get("focus_pid")
                and frame["process_name"].casefold() == foreground["process_name"].casefold()
                and parse_utc(frame["at"]) >= max(since, parse_utc(focus["at"]))):
            game_fps = frame["fps"]
    try:
        history = read_history(int(until.timestamp()) // 15, min(3, max(0, int(deadline - time.monotonic()))))
    except ActivityQueryError:
        history = {"at": None, "played": None, "complete": False}
    if foreground and foreground["active"] is True and foreground["classification"] == "game":
        history = dict(history, played=True)
    return dict(at=format_utc(until), foreground=foreground, frame=frame, game_fps=game_fps, history=history)
