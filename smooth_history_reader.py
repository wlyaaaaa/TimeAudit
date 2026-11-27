"""Local observations for the smoothness page, with their original attribution."""
import datetime as dt
from bisect import bisect_right
from collections import Counter, defaultdict
from hashlib import blake2s
import json
from pathlib import PureWindowsPath
import time

from game_live_reader import fill_archive_game_sessions, read_game_evidence, summarize_game_history
from personal_activity_reader import merge, query
from timeaudit_diagnostic_summary import VALID_FRAME_SQL, format_utc, parse_utc

CST = dt.timezone(dt.timedelta(hours=8))
DAY = dt.timedelta(days=1)
HARDWARE_STEP, GAME_WINDOW, HANG_STEP, HANG_THRESHOLD = 1, 3, 3, 5
MICRO, SEVERE = 50, 100

SQL = """
WITH b AS NOT MATERIALIZED (
  SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e
), frames AS MATERIALIZED (
  SELECT h.timestamp AS at, lower(nullif(h.measurement_quality->'frame_source'->>'exe','')) AS name,
         h.current_fps AS fps, CASE WHEN h.average_fps BETWEEN 0.5 AND 1000 THEN h.average_fps END AS average
  FROM public.fact_system_hardware h, b WHERE h.timestamp >= b.e - interval '1 day'
    AND h.timestamp < b.e AND ({valid})
), edges AS (
  SELECT *, name IS DISTINCT FROM lag(name) OVER w OR at - lag(at) OVER w > interval '2 seconds' AS changed
  FROM frames WINDOW w AS (ORDER BY at)
), numbered AS (
  SELECT *, count(*) FILTER (WHERE changed) OVER (ORDER BY at) AS segment FROM edges
), limits AS (
  SELECT name, min(at) AS s, max(at) + interval '{hardware_step} seconds' AS e, avg(fps) AS fps
  FROM numbered GROUP BY segment,name
), bands AS (
  SELECT name,s,least(e,lead(s) OVER (ORDER BY s),(SELECT e FROM b)) AS e,fps FROM limits
), durations AS (
  SELECT name,sum(extract(epoch FROM e-s)) AS seconds FROM bands WHERE name IS NOT NULL GROUP BY name
), app_samples AS (
  SELECT name,date_bin('30 seconds',at,'2000-01-01+00'::timestamptz) AS bin,sum(average) AS total,
         count(average) AS n,avg(average) AS fps FROM frames WHERE name IS NOT NULL GROUP BY name,bin
), apps AS (
  SELECT a.name AS process_name,round(d.seconds,1) AS seconds,sum(total)/nullif(sum(n),0) AS fps,
         percentile_cont(0.1) WITHIN GROUP (ORDER BY fps) AS low
  FROM app_samples a JOIN durations d USING(name) GROUP BY a.name,d.seconds
), game_frames AS (
  SELECT g.seq,date_bin('{game_window} seconds',h.timestamp,'2000-01-01+00'::timestamptz) AS at,
         max(h.frametime_ms) AS ms,max(h.timestamp) AS observed_at,
         sum(h.current_fps::double precision) AS fps_sum,count(*) AS fps_samples
  FROM jsonb_to_recordset('{games}'::jsonb) g(seq int,s timestamptz,e timestamptz,pid int,exe text)
  CROSS JOIN b CROSS JOIN LATERAL (
    SELECT h.timestamp,h.frametime_ms,h.current_fps FROM public.fact_system_hardware h
    WHERE h.timestamp >= b.s AND h.timestamp < b.e AND h.timestamp >= g.s AND h.timestamp < g.e
      AND h.measurement_quality->'frame_source'->'pid'=to_jsonb(g.pid)
      AND lower(h.measurement_quality->'frame_source'->>'exe')=g.exe
      AND h.measurement_quality->'frame_source'->'foreground_match'='true'::jsonb
      AND ({valid}) OFFSET 0
  ) h GROUP BY 1,2
), hung AS (
  SELECT a.timestamp,a.process_key,a.os_pid,r.process_name FROM public.fact_process_activity a
    LEFT JOIN public.dim_process_registry r USING(process_key), b
  WHERE a.timestamp >= b.s AND a.timestamp < b.e AND a.is_not_responding=1
), windows AS (
  SELECT first+i*interval '24 hours' AS s,least(e,first+(i+1)*interval '24 hours') AS e
  FROM (SELECT date_trunc('day',s,'Asia/Shanghai') AS first,e FROM b) days,generate_series(1,7) i
  UNION SELECT e-interval '24 hours',e FROM b
), sampled AS (
  SELECT w.s,hit.at FROM windows w CROSS JOIN b CROSS JOIN LATERAL (
    SELECT a.timestamp AS at FROM public.fact_process_activity a
    WHERE a.timestamp>=b.s AND a.timestamp<b.e AND a.timestamp>=w.s AND a.timestamp<w.e
    ORDER BY a.timestamp DESC LIMIT 1
  ) hit
)
SELECT jsonb_build_object(
  'bands',(SELECT json_agg(json_build_array(extract(epoch FROM s),extract(epoch FROM e),name,fps) ORDER BY s) FROM bands),
  'last_frame',(SELECT max(at) FROM frames),
  'apps',(SELECT json_agg(apps ORDER BY seconds DESC,process_name) FROM apps),
  'games',(SELECT json_agg(json_build_array(seq,extract(epoch FROM at),ms,observed_at,fps_sum,fps_samples)
                          ORDER BY seq,at) FROM game_frames),
  'hung',(SELECT json_agg(json_build_array(extract(epoch FROM timestamp),process_key,os_pid,process_name)
                       ORDER BY timestamp,process_key,os_pid) FROM hung),
  'sampled',(SELECT json_agg(json_build_array(extract(epoch FROM s),at)) FROM sampled));
""".replace("{valid}", VALID_FRAME_SQL).replace("{game_window}", str(GAME_WINDOW)).replace("{hardware_step}",str(HARDWARE_STEP))


def session_performance(session, games, samples_by_seq, events_by_seq):
    if session is None:
        return None
    result = dict(session, avg_fps=None, low_fps=None, game_events=None)
    if session["start"] is None:
        return dict(result, fps_reason="session_start_unknown", low_fps_reason="session_start_unknown",
                    game_events_reason="session_start_unknown")

    start, end = (parse_utc(session[key]).timestamp() for key in ("start", "end"))
    sequences = [seq for seq, (a, b, title, _, _) in enumerate(games)
                 if title == session["game_name"] and start <= a.timestamp() < b.timestamp() <= end]
    records = [sample for seq in sequences for sample in samples_by_seq.get(seq, ())]
    if not records:
        return dict(result, fps_reason="no_frame_samples", low_fps_reason="no_frame_samples",
                    game_events_reason="no_frame_samples")

    # Current FPS is sampled once per hardware reading. Rolling FPS and 1% low
    # do not preserve the original frames across an entire session.
    result.update(avg_fps=sum(fps_sum for _, _, fps_sum, _ in records) / sum(n for _, _, _, n in records),
                  fps_reason=None, low_fps_reason="raw_frame_times_unavailable")
    complete = merge((a, b) for a, b, _, _ in records) == [(start, end)]
    result.update(game_events=sum(events_by_seq[seq] for seq in sequences) if complete else None,
                  game_events_reason=None if complete else "frame_coverage_gap")
    return result


def read_smooth_history(until=None, *, timeout=20):
    """Read observed events and their coverage for 24 hours and seven Beijing calendar days."""
    until = until or dt.datetime.now(dt.timezone.utc)
    if until.utcoffset() is None:
        raise ValueError("until must include a timezone")
    until = until.astimezone(dt.timezone.utc)
    deadline = time.monotonic() + timeout
    midnight = until.astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    week = midnight - 6 * DAY
    lower = until - 7 * DAY
    evidence = read_game_evidence(lower, until, timeout=min(10, timeout))
    games = [(s, e, title, pid, PureWindowsPath(path).name.lower())
             for s,e,pid,path,kind,title in evidence if kind == "game"]
    selected = [dict(seq=i,s=format_utc(s),e=format_utc(e),pid=pid,exe=exe)
                for i,(s,e,_,pid,exe) in enumerate(games)]
    values = json.dumps(selected, ensure_ascii=False).replace("'", "''")
    data, = query(SQL.replace("{games}", values), lower, until, timeout=deadline-time.monotonic())
    away = merge((s.timestamp(),e.timestamp()) for s,e,_,_,kind,_ in evidence if kind == "away")
    starts = [s for s,_ in away]
    events, active, measured = [], {}, []
    frame_rows = data["games"] or []
    samples_by_seq, events_by_seq = defaultdict(list), Counter()
    for seq, at, ms, observed_at, fps_sum, n in frame_rows:
        s,e,title,_,exe = games[seq]
        start,end = max(s.timestamp(),at),min(e.timestamp(),at+GAME_WINDOW)
        measured.append((start,end))
        samples_by_seq[seq].append((start, end, fps_sum, n))
        if ms < MICRO:
            continue
        last_observed = parse_utc(observed_at).timestamp()
        prior = active.get(seq)
        if prior and prior["end"] == start:
            prior.update(end=end,max_frame_ms=max(prior["max_frame_ms"],ms),last_observed=last_observed)
        else:
            prior = dict(id=f"game:{start}:{exe}",kind="game",start=start,end=end,game_name=title,
                         process_name=exe,max_frame_ms=ms,last_observed=last_observed)
            events.append(prior)
            events_by_seq[seq] += 1
        active[seq] = prior

    active = {}
    for at, key, pid, name in data["hung"] or []:
        i = bisect_right(starts, at) - 1
        if i >= 0 and at < away[i][1]:
            active.pop((key,pid),None)
            continue
        end = min(at+HANG_STEP, until.timestamp(), away[i+1][0] if i+1 < len(away) else float("inf"))
        prior = active.get((key,pid))
        if (prior and 0 < at-prior["last_observed"] < HANG_STEP*1.5
                and (i < 0 or away[i][1] <= prior["last_observed"])):
            prior.update(end=end,last_observed=at,samples=prior["samples"]+1)
        else:
            identity = blake2s(f"{at}/{key}/{pid}".encode(),digest_size=8).hexdigest()
            prior = dict(id=f"hang:{identity}",kind="hang",start=at,end=end,process_name=name,
                         last_observed=at,samples=1)
            events.append(prior)
        active[key,pid] = prior

    known = merge((s.timestamp(),e.timestamp()) for s,e,_,_,kind,_ in evidence if kind in ("away","non_game"))
    sampled = dict(data["sampled"] or [])

    def counts(start, end):
        a,b = start.timestamp(),end.timestamp()
        seen = [event for event in events if a <= event["start"] < b]
        observed = sum(event["kind"] == "game" for event in seen)
        hangs = sum(event["kind"] == "hang" for event in seen)
        covered = merge((max(a,s),min(b,e)) for s,e in known+measured if a < e and b > s)
        game_complete = covered == [(a,b)]
        return dict(game_events=observed if observed or game_complete else None, game_complete=game_complete,
                    game_uncovered_seconds=round(max(0,b-a-sum(e-s for s,e in covered)),3),
                    program_hangs=hangs if hangs or a in sampled else None,hang_complete=False,
                    program_sampled_at=sampled.get(a))

    start = until - DAY
    window = {"from":format_utc(start),"to":format_utc(until)}
    history = summarize_game_history(evidence, until, sessions_from=lower)
    for key in ("latest_session", "previous_session"):
        history[key] = session_performance(history[key], games, samples_by_seq, events_by_seq)
    fill_archive_game_sessions(history, lower, deadline)
    return dict(summary=counts(start,until),game_history=history,
                daily_stutters=[dict(day=(week+i*DAY).date().isoformat(),
                    **counts(week+i*DAY,min(week+(i+1)*DAY,until))) for i in range(7)],
                stutters=dict(window=window,events=sorted(
                    (event for event in events if event["start"]>=start.timestamp()),key=lambda event: event["start"]),
                    observed_at=dict(hang=sampled.get(start.timestamp()),
                        game=max((row[3] for row in frame_rows if parse_utc(row[3])>=start),default=None)),
                    method=dict(game_window_seconds=GAME_WINDOW,hang_sample_seconds=HANG_STEP,
                        hang_threshold_seconds=HANG_THRESHOLD,hardware_sample_seconds=HARDWARE_STEP,
                        game_min_ms=MICRO,severe_min_ms=SEVERE,frame_peak_basis="recorded_latest_frame_samples",
                        hang_basis="recorded_active_process_samples")),
                fps=dict(sources_at=data["last_frame"],
                         sources=[dict(start=s,end=e,process_name=PureWindowsPath(name).name if name else None,fps=fps)
                                  for s,e,name,fps in data["bands"] or []],apps=data["apps"] or []))
