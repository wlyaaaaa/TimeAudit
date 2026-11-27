"""Foreground and frame evidence for trusted local live consumers."""
from collections import defaultdict
import datetime as dt
from functools import lru_cache
import json
from pathlib import Path, PureWindowsPath
import time

from game_catalog import classify
from personal_activity_reader import ActivityQueryError, intersect, merge, missing, query, seconds
from timeaudit_diagnostic_summary import VALID_FRAME_SQL, format_utc, parse_utc

ROOT = Path(__file__).resolve().parent
FRESH = dt.timedelta(seconds=15)
DAY = dt.timedelta(days=1)
AWAY = {"system_idle", "system_lockscreen", "system_displayoff", "system_sleep", "lockapp.exe"}

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
WITH b AS NOT MATERIALIZED (SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e),
usage AS (
    SELECT greatest(a.start_time,b.s) AS s, least(a.start_time + a.duration_seconds * interval '1 second',b.e) AS e,
           lower(a.process_name) AS name FROM public.app_usage_logs a, b
    WHERE a.start_time >= b.s - interval '1 day' AND a.start_time < b.e AND a.duration_seconds > 0
      AND a.start_time + a.duration_seconds * interval '1 second' > b.s
),
focus AS (
    SELECT greatest(c.timestamp,b.s) AS s, least(c.end_timestamp,b.e) AS e,
           lower(r.process_name) AS name,c.os_pid,r.executable_path
    FROM public.fact_process_context c JOIN public.dim_process_registry r USING(process_key), b
    WHERE c.timestamp >= b.s - interval '1 day' AND c.timestamp < b.e AND c.end_timestamp > b.s AND c.is_foreground=1
)
SELECT json_build_array(s,e,name,NULL,NULL) FROM usage WHERE e>s
UNION ALL SELECT json_build_array(s,e,name,os_pid,executable_path) FROM focus WHERE e>s;
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


def read_game_evidence(after, until, *, timeout):
    """Join recorded foreground and activity spans by executable name and time."""
    rows = query(HISTORY_SQL, after, until, timeout=timeout)
    usage, focus = defaultdict(list), defaultdict(list)
    for s,e,name,pid,path in rows:
        spans = usage[name] if pid is None else focus[name,pid,path]
        spans.append((parse_utc(s),parse_utc(e)))
    away = merge(span for name in AWAY for span in usage[name])
    evidence = [(s,e,None,"","away",None) for s,e in away]
    verdicts = {path: classify(path) for path in {p for n,_,p in focus if n in usage and n not in AWAY}}
    for (name,pid,path),spans in focus.items():
        if name in AWAY or path not in verdicts:
            continue
        kind,title = verdicts[path]
        for s,e in intersect(usage[name],spans):
            active = missing(intersect(away, [(s,e)]),s,e) if kind == "game" else [(s,e)]
            evidence.extend((a,b,pid,path,kind,title) for a,b in active)
    return evidence


def summarize_game_history(evidence, until, *, sessions_from=None):
    after = until - DAY
    sessions_from = sessions_from or after
    covered = [(max(s,after),e) for s,e,_,_,kind,_ in evidence if e > after and kind != "unknown"]
    complete = not missing(covered, after, until)
    games = defaultdict(list)
    for s,e,_,_,kind,title in evidence:
        if kind == "game":
            games[title].append((s,e))
    recent = {name: [(max(s,after),e) for s,e in spans if e > after] for name,spans in games.items()}
    totals = {name: seconds(spans) for name,spans in recent.items() if spans}
    sessions = sorted((s,e,name) for name,spans in games.items() for s,e in merge(spans))
    sessions = [dict(game_name=name,start=format_utc(s) if s > sessions_from else None,end=format_utc(e))
                for s,e,name in sessions[-2:]]
    return dict(at=format_utc(until), played=True if totals else False if complete else None, complete=complete,
        game_name=next(iter(totals)) if len(totals) == 1 else None,
        seconds=seconds(span for spans in recent.values() for span in spans) if totals else 0 if complete else None,
        games=[dict(game_name=name,seconds=value) for name,value in totals.items()],sessions_from=format_utc(sessions_from),
        latest_session=sessions[-1] if sessions else None, previous_session=sessions[-2] if len(sessions) > 1 else None)


@lru_cache(maxsize=2)
def read_history(bucket, timeout):
    until = dt.datetime.fromtimestamp(bucket * 15, tz=dt.timezone.utc)
    return summarize_game_history(read_game_evidence(until - DAY, until, timeout=timeout), until)


def read_game_status(until=None, *, timeout=5, include_history=True):
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
    history = {"at": None, "played": None, "complete": False}
    if include_history:
        try:
            history = read_history(int(until.timestamp()) // 15, min(3, max(0, int(deadline - time.monotonic()))))
        except ActivityQueryError:
            pass
    if foreground and foreground["active"] is True and foreground["classification"] == "game":
        history = dict(history, played=True)
    return dict(at=format_utc(until), foreground=foreground, frame=frame, game_fps=game_fps, history=history)


def read_live_game_status(until=None, *, timeout=5):
    """Read current foreground and frame evidence without the history query."""
    return read_game_status(until, timeout=timeout, include_history=False)
