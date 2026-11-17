"""Five-minute, payload-free evidence for trusted internal TimeAudit consumers."""
import datetime as dt

from personal_activity_reader import query
from timeaudit_diagnostic_summary import VALID_FRAME_SQL


SQL = r"""
WITH b AS NOT MATERIALIZED (SELECT :'after_utc'::timestamptz AS s, :'until_utc'::timestamptz AS e),
hardware AS (
  SELECT h.timestamp, h.frametime_ms, {valid_frame} AS valid_frame
  FROM public.fact_system_hardware h, b WHERE h.timestamp >= b.s AND h.timestamp < b.e
), latest AS (
  SELECT count(DISTINCT a.timestamp) AS snapshot_count, max(a.timestamp) AS sampled_at
  FROM public.fact_process_activity a, b
  WHERE a.timestamp >= b.s AND a.timestamp < b.e
), hung AS (
  SELECT COALESCE(r.process_name,'[unresolved_process]') AS process_name, count(*) AS sample_count,
         min(a.timestamp) AS first_observed_at, max(a.timestamp) AS last_observed_at
  FROM public.fact_process_activity a LEFT JOIN public.dim_process_registry r USING(process_key), b
  WHERE a.timestamp >= b.s AND a.timestamp < b.e AND a.is_not_responding = 1
  GROUP BY 1
)
SELECT jsonb_build_object(
  'window_start',b.s, 'window_end',b.e,
  'hardware_sample_count',(SELECT count(*) FROM hardware),
  'hardware_last_sample_at',(SELECT max(timestamp) FROM hardware),
  'valid_frame_samples',(SELECT count(*) FROM hardware WHERE valid_frame),
  'frametime_spike_samples',(SELECT count(*) FROM hardware WHERE valid_frame AND frametime_ms >= 50),
  'last_spike_at',(SELECT max(timestamp) FROM hardware WHERE valid_frame AND frametime_ms >= 50),
  'activity_snapshot_count',latest.snapshot_count,
  'activity_last_sample_at',latest.sampled_at,
  'recent_hung',COALESCE((SELECT json_agg(hung ORDER BY process_name) FROM hung),'[]'::json),
  'current_hung',CASE WHEN latest.sampled_at >= b.e - interval '15 seconds' THEN
    COALESCE((SELECT json_agg(process_name ORDER BY process_name) FROM hung
              WHERE last_observed_at = latest.sampled_at),'[]'::json) END)
FROM b CROSS JOIN latest;
""".replace("{valid_frame}", VALID_FRAME_SQL)


def query_recent_responsiveness(until=None, *, timeout=25):
    """Read sampled frame spikes and active-process hangs; current data expires after 15 seconds.

    Counts represent sampled observations. Fully idle processes can be absent from
    activity collection, so an empty current_hung list covers sampled active processes.
    The caller supplies the applicable personal-data access and publication policy.
    """
    until = until or dt.datetime.now(dt.timezone.utc)
    if until.tzinfo is None or until.utcoffset() is None:
        raise ValueError("until must include a timezone")
    until = until.astimezone(dt.timezone.utc)
    return query(SQL, until - dt.timedelta(minutes=5), until, timeout=timeout)[0]
