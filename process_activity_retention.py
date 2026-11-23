"""60-day process samples and durable system minute and application/system hour/day statistics.

All SQL windows are half-open. Rate totals keep the established 3.1-second
estimate; absent samples never become elapsed-time integration.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import time

BASE_METRICS = (
    "proc_cpu_usage", "proc_gpu_usage", "proc_ram_mb", "proc_vram_used_gb",
    "proc_vram_shared_mb", "proc_disk_read_rate_mb", "proc_disk_write_rate_mb",
    "proc_disk_iops", "proc_network_send_kb", "proc_network_recv_kb",
    "proc_active_connections", "proc_thread_count", "proc_process_count",
)
MINUTE_SERIES = (
    ("proc_network_send_kb", "upload_bps", 6, 1024),
    ("proc_network_recv_kb", "download_bps", 6, 1024),
    ("proc_active_connections", "tcp_connections", 2, 1),
    ("proc_disk_read_rate_mb", "disk_read_bps", 6, 1048576),
    ("proc_disk_write_rate_mb", "disk_write_bps", 6, 1048576),
    ("proc_disk_iops", "disk_iops", 2, 1),
    ("proc_process_count", "process_count", None, 1),
    ("proc_thread_count", "thread_count", 2, 1),
)
MINUTE_METRICS = tuple(name for name, *_ in MINUTE_SERIES)
MINUTE = dt.timedelta(minutes=1)
MINUTE_RETENTION = dt.timedelta(days=7)
MINUTE_BATCH = dt.timedelta(minutes=30)
MINUTE_CATCHUP = dt.timedelta(minutes=5)
MINUTE_BACKFILL_SETTLE = 2 * MINUTE
MINUTE_ONLINE_LIMIT = 2.0
MINUTE_BACKFILL_LIMIT = 30.0
MAINTENANCE_QUERY_TIMEOUT_SEC = 900.0
DETAIL_DAYS = 60
LOCK_ID = 607202609
UTC = dt.timezone.utc
CN = dt.timezone(dt.timedelta(hours=8))
DISK_ACTIVE = "(proc_disk_read_rate_mb > 0.001 OR proc_disk_write_rate_mb > 0.001 OR proc_disk_iops > 0)"
NETWORK_ACTIVE = "(proc_network_send_kb > 0 OR proc_network_recv_kb > 0)"
FILTERS = {m: DISK_ACTIVE if m.startswith("proc_disk_") else NETWORK_ACTIVE
           for m in BASE_METRICS if m.startswith(("proc_disk_", "proc_network_")) or m == "proc_active_connections"}
PROCESS_METRICS = BASE_METRICS + tuple("active_" + m for m in FILTERS)
METRICS = PROCESS_METRICS + tuple("app_total_" + m for m in BASE_METRICS)
STAT_COLUMNS = ["sample_count", "not_responding_count", "disk_sample_count", "network_sample_count"] + [
    f"{metric}_{suffix}" for metric in METRICS for suffix in ("sum", "count", "max")
]


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def stat_definitions(metrics=None):
    if metrics is not None:
        return "sample_count bigint, " + ", ".join(f"{m}_sum double precision, {m}_count bigint" for m in metrics)
    return "sample_count bigint, not_responding_count bigint, disk_sample_count bigint, network_sample_count bigint, " + ", ".join(
        f"{m}_sum double precision, {m}_count bigint, {m}_max double precision"
        for m in METRICS
    )


def aggregate_columns(prefix="", merge=False, app_totals=True):
    if merge:
        return ", ".join(
            [f"sum({prefix}{c})::bigint" for c in STAT_COLUMNS[:4]]
            + [(f"{op}({prefix}{m}_{s})" + ("::bigint" if s == "count" else ""))
               if app_totals or not m.startswith("app_total_")
               else ("0::bigint" if s == "count" else "NULL::double precision")
               for m in METRICS for s, op in (("sum", "sum"), ("count", "sum"), ("max", "max"))]
        )
    expressions = ["count(*)", "count(*) FILTER (WHERE is_not_responding = 1)",
                   f"count(*) FILTER (WHERE {DISK_ACTIVE})", f"count(*) FILTER (WHERE {NETWORK_ACTIVE})"]
    for m in METRICS:
        expressions.extend((f"sum({m}::double precision)",
                            f"count({m})", f"max({m}::double precision)"))
    return ", ".join(expressions)


IDENTITY_SQL = """COALESCE(r.process_name, '[unregistered:' || a.process_key::text || ']') AS process_name,
COALESCE(r.executable_path, '') AS executable_path"""


def raw_source(system=False, *, bounded_identities=False, app_totals=True, metrics=None):
    if metrics is not None:
        fields = ", ".join(
            f"count(*)::double precision AS {m}" if m == "proc_process_count" else
            f"sum(a.{m}::double precision) AS {m}"
            for m in metrics
        )
        return f"""SELECT a.timestamp, {fields} FROM public.fact_process_activity a
                   WHERE a.timestamp >= $1 AND a.timestamp < $2 GROUP BY a.timestamp"""
    active = ", ".join(f"CASE WHEN {condition} THEN a.{m} END AS active_{m}" for m, condition in FILTERS.items())
    raw = f"""SELECT a.*, 1 AS proc_process_count, {IDENTITY_SQL}, {active}
          FROM public.fact_process_activity a
          LEFT JOIN public.dim_process_registry r USING(process_key)
          WHERE a.timestamp >= $1 AND a.timestamp < $2"""
    if not system and bounded_identities:
        # A day can contain millions of samples but only thousands of keys.
        # Bound identity lookups to those keys, instead of hashing the complete
        # multi-million-row registry for each requested day.
        # OFFSET 0 keeps each LATERAL lookup on the registry primary key;
        # only the small identity set is materialized, not the activity rows.
        identities = """SELECT keys.process_key, registry.process_name, registry.executable_path
          FROM (SELECT DISTINCT a.process_key
                FROM public.fact_process_activity a
                WHERE a.timestamp >= $1 AND a.timestamp < $2) keys
          LEFT JOIN LATERAL (
            SELECT r.process_name, r.executable_path
            FROM public.dim_process_registry r WHERE r.process_key = keys.process_key OFFSET 0
          ) registry ON true"""
        raw = f"WITH identities AS MATERIALIZED ({identities}) " + raw.replace(
            "LEFT JOIN public.dim_process_registry r", "LEFT JOIN identities r"
        )
    if not system and not app_totals:
        # Per-process readers skip the per-instant application window; its
        # sort over every raw sample dominates a 24-hour ranking read.
        totals = ", ".join(f"NULL::double precision AS app_total_{m}" for m in BASE_METRICS)
        return f"SELECT samples.*, {totals} FROM ({raw}) samples"
    # One contribution per application and sampling instant. Multiple PIDs
    # sum before the time aggregation; independent metric NULLs stay unknown.
    totals = ", ".join(
        f"CASE WHEN row_number() OVER app_sample = 1 THEN sum({m}::double precision) OVER app_sample END AS app_total_{m}"
        for m in BASE_METRICS
    )
    per_app_sample = f"SELECT samples.*, {totals} FROM ({raw}) samples WINDOW app_sample AS (PARTITION BY timestamp, process_name, executable_path)"
    if not system:
        return per_app_sample
    system_fields = [f"sum({m}::double precision) AS {m}" for m in PROCESS_METRICS]
    system_fields += [f"sum({m}::double precision) AS app_total_{m}" for m in BASE_METRICS]
    return "SELECT timestamp, max(is_not_responding) AS is_not_responding, " + ", ".join(system_fields) + f" FROM ({raw}) per_process GROUP BY timestamp"


def minute_schema_sql():
    columns = stat_definitions(MINUTE_METRICS)
    means = []
    for name, label, digits, scale in MINUTE_SERIES:
        value = f"m.{name}_sum / NULLIF(m.{name}_count, 0)"
        if digits is not None:
            value = f"round(({value})::numeric, {digits}) * {scale}"
        means.append(f"({value})::double precision AS {label}")
    final_columns = ", ".join(label + " double precision" for _, label, _, _ in MINUTE_SERIES)
    return f"""ALTER TABLE public.activity_retention_state
          ADD COLUMN IF NOT EXISTS minute_covered_from timestamptz,
          ADD COLUMN IF NOT EXISTS minute_covered_until timestamptz;
      CREATE TABLE IF NOT EXISTS public.activity_system_minute (
          bucket_start timestamptz PRIMARY KEY, {columns});
      CREATE OR REPLACE FUNCTION public.activity_system_minute_coverage()
      RETURNS TABLE(covered_from timestamptz, covered_until timestamptz) LANGUAGE sql STABLE AS $function$
          SELECT minute_covered_from, minute_covered_until FROM public.activity_retention_state WHERE singleton
      $function$;
      CREATE OR REPLACE FUNCTION public.activity_system_minute_stats(p_from timestamptz, p_to timestamptz)
      RETURNS TABLE(bucket_start timestamptz, {columns}, {final_columns}) LANGUAGE sql STABLE AS $function$
          SELECT m.*, {", ".join(means)}
          FROM public.activity_system_minute m CROSS JOIN public.activity_retention_state s
          WHERE s.singleton AND m.bucket_start >= greatest(p_from, s.minute_covered_from)
            AND m.bucket_start < least(p_to, s.minute_covered_until)
            AND s.minute_covered_from IS NOT NULL AND s.minute_covered_until IS NOT NULL
      $function$;"""


def schema_sql():
    statements = ["""CREATE TABLE IF NOT EXISTS public.activity_retention_state (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        enabled boolean NOT NULL DEFAULT false,
        summaries_ready boolean NOT NULL DEFAULT false,
        summarized_until timestamptz NOT NULL DEFAULT '-infinity',
        backfill_until timestamptz NOT NULL DEFAULT '-infinity',
        raw_since timestamptz NOT NULL DEFAULT '-infinity'
    ); INSERT INTO public.activity_retention_state(singleton) VALUES(true) ON CONFLICT DO NOTHING;"""]
    # activity_process_stats is activity_app_stats with the same signature and
    # rows, minus the app_total_* columns (always NULL/0): per-process rankings
    # do not read them and their raw-tail window sort is most of the cost.
    for name, kind, app_totals in (("app", "app", True), ("system", "system", True), ("process", "app", False)):
        identity = "process_name text NOT NULL, executable_path text NOT NULL, " if kind == "app" else ""
        keys = ", process_name, executable_path" if kind == "app" else ""
        for grain in ("hour", "day") if name == kind else ():
            statements.append(f"""CREATE TABLE IF NOT EXISTS public.activity_{kind}_{grain} (
              bucket_start timestamptz NOT NULL, {identity}{stat_definitions()},
              PRIMARY KEY(bucket_start{keys}));""")
        # The raw side keeps arbitrary recent boundaries exact. Only retired
        # hours/days use whole buckets. Their precision is surfaced in dashboards.
        returns = f"bucket_start timestamptz, {identity.replace(' NOT NULL', '')}{stat_definitions()}"
        group_identity = ", process_name, executable_path" if kind == "app" else ""
        result_identity = ", process_name, executable_path" if kind == "app" else ""
        source_raw = raw_source(kind == "system", bounded_identities=kind == "app", app_totals=app_totals)
        source_head = source_raw.replace("$1", "GREATEST(p_from, cutoff)").replace(
            "$2", "LEAST(p_to, full_from) AND full_to > full_from")
        source_tail = source_raw.replace("$1", "CASE WHEN full_to > full_from THEN GREATEST(p_from, cutoff, full_to) ELSE GREATEST(p_from, cutoff) END").replace("$2", "p_to")
        source = f"({source_head}) UNION ALL ({source_tail})"
        statements.append(f"""CREATE OR REPLACE FUNCTION public.activity_{name}_stats(
            p_from timestamptz, p_to timestamptz, p_step integer DEFAULT 3600)
          RETURNS TABLE({returns}) LANGUAGE sql STABLE AS $function$
          WITH state AS (SELECT raw_since AS cutoff,
              CASE WHEN summaries_ready AND p_step >= 3600 AND p_step % 3600 = 0
                THEN summarized_until ELSE raw_since END AS covered_until
              FROM public.activity_retention_state WHERE singleton AND p_to > p_from),
          bounds AS (SELECT *,
              date_trunc('hour', greatest(p_from, cutoff)) + CASE
                WHEN greatest(p_from, cutoff) > date_trunc('hour', greatest(p_from, cutoff))
                THEN interval '1 hour' ELSE interval '0' END AS full_from,
              least(date_trunc('hour', p_to), covered_until) AS full_to
              FROM state),
          raw AS ({source}),
          recent AS (
            SELECT date_bin(make_interval(secs => greatest(p_step, 1)), timestamp, '2000-01-01 00:00:00+08'::timestamptz) AS bucket_start
              {result_identity}, {aggregate_columns()}
            FROM raw GROUP BY 1{group_identity}
          ),
          history AS (
            SELECT * FROM public.activity_{kind}_hour, bounds
            WHERE bucket_start < covered_until AND bucket_start < p_to
              AND (bucket_start < cutoff OR (bucket_start >= p_from AND bucket_start + interval '1 hour' <= p_to))
              AND bucket_start >= date_trunc('hour', p_from)
              AND NOT (p_step >= 86400
                AND date_trunc('day', bucket_start, 'Asia/Shanghai') >= p_from
                AND date_trunc('day', bucket_start, 'Asia/Shanghai') + interval '1 day' <= least(p_to, covered_until))
            UNION ALL
            SELECT * FROM public.activity_{kind}_day, bounds
            WHERE p_step >= 86400 AND bucket_start >= p_from
              AND bucket_start + interval '1 day' <= least(p_to, covered_until)
          )
          SELECT * FROM recent
          UNION ALL
          SELECT date_bin(make_interval(secs => greatest(p_step, 3600)), bucket_start, '2000-01-01 00:00:00+08'::timestamptz)
            {result_identity}, {aggregate_columns(merge=True, app_totals=app_totals)}
          FROM history GROUP BY 1{group_identity}
          $function$;""".replace("FROM public.fact_process_activity a\n", "FROM public.fact_process_activity a CROSS JOIN bounds\n"))
    return "\n".join((*statements, minute_schema_sql()))


async def install(conn):
    await conn.execute(schema_sql(), timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    await conn.execute("ANALYZE public.activity_retention_state", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)


def minute_floor(at):
    return at.replace(second=0, microsecond=0)


async def minute_state(conn, earliest, timeout, *, wait=True):
    lock = "FOR UPDATE" if wait else "FOR UPDATE NOWAIT"
    return await conn.fetchrow(f"""SELECT minute_covered_from, minute_covered_until,
        greatest(raw_since, $1::timestamptz) AS earliest
        FROM public.activity_retention_state WHERE singleton {lock}""", earliest, timeout=timeout)


def minute_rows_sql():
    stats = ", ".join(f"sum({m}) AS {m}_sum, count({m}) AS {m}_count" for m in MINUTE_METRICS)
    return f"""SELECT date_bin('1 minute', timestamp, '2000-01-01 00:00:00+08'::timestamptz) AS bucket_start,
        count(*) AS sample_count, {stats} FROM ({raw_source(metrics=MINUTE_METRICS)}) AS samples GROUP BY 1"""


async def refresh_minutes(conn, start, end, timeout):
    await conn.execute("DELETE FROM public.activity_system_minute WHERE bucket_start >= $1 AND bucket_start < $2",
                       start, end, timeout=timeout)
    await conn.execute("INSERT INTO public.activity_system_minute " + minute_rows_sql(), start, end, timeout=timeout)


async def cover_minutes(conn, start, end, timeout):
    # Only touching ranges can share one coverage receipt.
    return await conn.fetchrow("""UPDATE public.activity_retention_state SET
        minute_covered_from = CASE WHEN minute_covered_until >= $1 AND minute_covered_from <= $2
            THEN least(minute_covered_from, $1) ELSE $1 END,
        minute_covered_until = CASE WHEN minute_covered_until >= $1 AND minute_covered_from <= $2
            THEN greatest(minute_covered_until, $2) ELSE $2 END
        WHERE singleton RETURNING minute_covered_from, minute_covered_until""", start, end, timeout=timeout)


async def advance_minutes(conn, sample_at):
    """Close past minutes after a sample from the next minute has committed."""
    end, timeout = minute_floor(sample_at), MINUTE_ONLINE_LIMIT
    async with conn.transaction():
        state = await minute_state(conn, end-MINUTE_RETENTION, timeout, wait=False)
        covered = state["minute_covered_until"]
        start = max(state["earliest"], covered if covered and covered < end else end-MINUTE)
        if start >= end:
            return
        scan_from, stop = start, end
        if end-start > MINUTE_CATCHUP:
            # Skip a long power-off gap only after a bounded source lookup finds its next fact.
            first = await conn.fetchval("""SELECT timestamp FROM public.fact_process_activity
                WHERE timestamp >= $1 AND timestamp < $2 ORDER BY timestamp LIMIT 1""", start, end, timeout=timeout)
            if first is None:
                return await cover_minutes(conn, start, end, timeout)
            scan_from = minute_floor(first)
            stop = min(end, scan_from+MINUTE_CATCHUP)
        await refresh_minutes(conn, scan_from, stop, timeout)
        return await cover_minutes(conn, start, stop, timeout)


async def backfill_minutes(conn, now=None, hours=72, max_batches=None):
    # Leave recent in-flight samples to the single collector's post-commit rollup.
    end = minute_floor(now or dt.datetime.now(UTC))-MINUTE_BACKFILL_SETTLE
    requested_from = end-dt.timedelta(hours=hours)
    timeout, batches = MINUTE_BACKFILL_LIMIT, 0
    while max_batches is None or batches < max_batches:
        started = time.monotonic()
        async with conn.transaction():
            state = await minute_state(conn, requested_from, timeout)
            earliest = state["earliest"]
            limited = {"raw_available_from": earliest.isoformat()} if earliest > requested_from else {}
            if earliest >= end:
                return {"complete": False, "batches": batches, **limited}
            lower, upper = state["minute_covered_from"], state["minute_covered_until"]
            if lower and lower <= earliest and upper >= end:
                return {"complete": not limited, "batches": batches, **limited}
            if upper and earliest <= upper < end:
                start, stop = upper, min(end, upper+MINUTE_BATCH)
            else:
                stop = lower if lower and upper >= end else end
                start = max(earliest, stop-MINUTE_BATCH)
            await refresh_minutes(conn, start, stop, timeout)
            covered = await cover_minutes(conn, start, stop, timeout)
        batches += 1
        print(json.dumps(dict(minute_from=start.isoformat(), minute_until=stop.isoformat(),
                              elapsed_seconds=round(time.monotonic()-started, 3))), flush=True)
        if covered["minute_covered_from"] <= earliest and covered["minute_covered_until"] >= end:
            return {"complete": not limited, "batches": batches, **limited}
    return {"complete": False, "batches": batches}


async def prune_minutes(conn, now):
    cutoff, timeout = minute_floor(now)-MINUTE_RETENTION, MINUTE_BACKFILL_LIMIT
    async with conn.transaction():
        await conn.execute("""UPDATE public.activity_retention_state SET
            minute_covered_from = CASE WHEN minute_covered_until > $1 THEN $1 END,
            minute_covered_until = CASE WHEN minute_covered_until > $1 THEN minute_covered_until END
            WHERE singleton AND minute_covered_from < $1""", cutoff, timeout=timeout)
        await conn.execute("DELETE FROM public.activity_system_minute WHERE bucket_start < $1", cutoff, timeout=timeout)


async def refresh_hours(conn, start, end):
    """Replace only a raw-backed, whole-hour interval; rebuild days from hours.

    Caller serializes maintenance. Old retired hours are never deleted on retry.
    Each transaction is bounded to at most one day except a final partition pass.
    """
    if start.minute or start.second or start.microsecond or end.minute or end.second or end.microsecond:
        raise ValueError("summary boundaries must be whole hours")
    raw_since = await conn.fetchval("SELECT CASE WHEN raw_since = '-infinity'::timestamptz THEN '0001-01-01 00:00:00+00'::timestamptz ELSE raw_since END FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    if start < raw_since:
        raise ValueError("cannot replace retired hours from absent raw rows")
    if end <= start:
        return
    for kind in ("app", "system"):
        keys = ", process_name, executable_path" if kind == "app" else ""
        table = f"public.activity_{kind}_hour"
        await conn.execute(f"DELETE FROM {table} WHERE bucket_start >= $1 AND bucket_start < $2", start, end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        await conn.execute(f"""INSERT INTO {table}
            SELECT date_trunc('hour', timestamp){keys}, {aggregate_columns()}
            FROM ({raw_source(kind == 'system', bounded_identities=True)}) raw
            GROUP BY 1{keys}""", start, end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        day_start = start.astimezone(CN).replace(hour=0)
        day_end = (end - dt.timedelta(microseconds=1)).astimezone(CN).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        day = f"public.activity_{kind}_day"
        await conn.execute(f"DELETE FROM {day} WHERE bucket_start >= $1 AND bucket_start < $2", day_start, day_end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        await conn.execute(f"""INSERT INTO {day}
          SELECT date_trunc('day', bucket_start, 'Asia/Shanghai'){keys}, {aggregate_columns(merge=True)}
          FROM {table} WHERE bucket_start >= $1 AND bucket_start < $2
          GROUP BY 1{keys}""", day_start, day_end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)


async def partitions(conn):
    rows = await conn.fetch("""SELECT child.relname AS name, child.relnamespace::regnamespace::text AS schema,
      pg_get_expr(child.relpartbound, child.oid) AS bound
      FROM pg_inherits i JOIN pg_class child ON child.oid=i.inhrelid
      JOIN pg_class parent ON parent.oid=i.inhparent
      JOIN pg_namespace ns ON ns.oid=parent.relnamespace
      WHERE ns.nspname='public' AND parent.relname='fact_process_activity'""", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    result = []
    for row in rows:
        if row['schema'] != 'public':
            raise ValueError("activity partition outside the public schema; retention not advanced")
        match = re.fullmatch(r"FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)", row["bound"] or "")
        if not match:
            raise ValueError(f"unsupported activity partition bound: {row['name']}")
        start, end = map(dt.datetime.fromisoformat, match.groups())
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("partition bounds require explicit timezone")
        result.append((start, end, row["name"]))
    return sorted(result)


async def backfill(conn, now=None, max_days=None):
    now = now or dt.datetime.now(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    raw_since = await conn.fetchval("SELECT CASE WHEN raw_since = '-infinity'::timestamptz THEN '0001-01-01 00:00:00+00'::timestamptz ELSE raw_since END FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    state = await conn.fetchrow("SELECT summaries_ready, CASE WHEN backfill_until = '-infinity'::timestamptz THEN '0001-01-01 00:00+00'::timestamptz ELSE backfill_until END AS backfill_until FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    resume_from = raw_since if state['summaries_ready'] else max(raw_since, state['backfill_until'])
    completed_days = 0
    for lower, upper, name in await partitions(conn):
        cursor = max(lower, resume_from)
        limit = min(upper, end)
        while cursor < limit:
            stop = min(cursor + dt.timedelta(days=1), limit)
            started = time.monotonic()
            async with conn.transaction():
                await refresh_hours(conn, cursor, stop)
                rows = await conn.fetchval("SELECT COALESCE(sum(sample_count),0)::bigint FROM public.activity_app_hour WHERE bucket_start >= $1 AND bucket_start < $2", cursor, stop, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                await conn.execute("UPDATE public.activity_retention_state SET backfill_until=$1 WHERE singleton", stop, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
            elapsed = time.monotonic() - started
            print(json.dumps({"refreshed_from": cursor.isoformat(), "to": stop.isoformat(),
                              "source_rows": rows, "elapsed_seconds": round(elapsed, 3),
                              "source_rows_per_second": round(rows / max(elapsed, 0.001), 1)}), flush=True)
            cursor = stop
            completed_days += 1
            if max_days is not None and completed_days >= max_days:
                # Partial probe cannot claim complete coverage or enable expiry.
                return {"complete": False, "batches": completed_days}
    await conn.execute("UPDATE public.activity_retention_state SET summaries_ready=true, summarized_until=$1 WHERE singleton", end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
    await conn.execute("ANALYZE public.activity_app_hour, public.activity_app_day, public.activity_system_hour, public.activity_system_day", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)


async def maintain(conn, now=None):
    """Refresh recent statistics; summarize and retire whole old partitions.

    The state switch is enabled only after the migration's dashboard deployment.
    Hold SHARE on the old child while summarizing: readers and current-partition
    inserts continue, but late writes to that child wait. The final parent/child
    exclusive locks never wait, avoiding the inverse parent-first reader/writer
    lock order. Any conflict rolls back this partition and the caller retries.
    """
    now = now or dt.datetime.now(UTC)
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC):
        return {"busy": True, "dropped": []}
    try:
        await install(conn)
        await prune_minutes(conn, now)
        end = now.replace(minute=0, second=0, microsecond=0)
        raw_since = await conn.fetchval("SELECT CASE WHEN raw_since = '-infinity'::timestamptz THEN '0001-01-01 00:00:00+00'::timestamptz ELSE raw_since END FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        state = await conn.fetchrow("SELECT summaries_ready, summarized_until FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        start = end - dt.timedelta(days=2)
        if state['summaries_ready']:
            start = min(start, state['summarized_until'])
        cursor = max(start, raw_since)
        while cursor < end:
            stop = min(cursor + dt.timedelta(days=1), end)
            async with conn.transaction():
                await refresh_hours(conn, cursor, stop)
            cursor = stop
        if state['summaries_ready']:
            await conn.execute("UPDATE public.activity_retention_state SET summarized_until=$1 WHERE singleton", end, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        enabled = await conn.fetchval("SELECT enabled FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        dropped = []
        if enabled:
            for start, upper, name in await partitions(conn):
                if upper > now - dt.timedelta(days=DETAIL_DAYS):
                    continue
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '5s'", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    await conn.execute(f"LOCK TABLE public.{quote_ident(name)} IN SHARE MODE", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    cursor = start
                    while cursor < upper:
                        stop = min(cursor + dt.timedelta(days=1), upper)
                        await refresh_hours(conn, cursor, stop)
                        cursor = stop
                    counts = await conn.fetchrow(f"""SELECT
                      (SELECT count(*) FROM public.{quote_ident(name)}) AS raw_count,
                      (SELECT COALESCE(sum(sample_count), 0) FROM public.activity_app_hour
                         WHERE bucket_start >= $1 AND bucket_start < $2) AS summary_count""", start, upper, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    if counts['raw_count'] != counts['summary_count']:
                        raise RuntimeError("activity summary row count mismatch; partition retained")
                    # DROP also locks the parent. Never wait here while holding
                    # the child's SHARE lock: a late writer can already hold
                    # RowExclusive on the parent while waiting for this child.
                    # ONLY avoids recursively locking unrelated/current leaves.
                    await conn.execute("LOCK TABLE ONLY public.fact_process_activity IN ACCESS EXCLUSIVE MODE NOWAIT", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    await conn.execute(f"LOCK TABLE public.{quote_ident(name)} IN ACCESS EXCLUSIVE MODE NOWAIT", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    await conn.execute(f"DROP TABLE public.{quote_ident(name)}", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                    await conn.execute("UPDATE public.activity_retention_state SET raw_since=$1 WHERE singleton", upper, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
                dropped.append(name)
        return {"enabled": enabled, "dropped": dropped}
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)


async def main_async(args):
    import asyncpg
    from db_config import local_dsn
    conn = await asyncpg.connect(local_dsn(), server_settings={"timezone": "Asia/Shanghai"})
    status_sql = """SELECT enabled, raw_since::text AS raw_since, summaries_ready,
        summarized_until::text AS summarized_until, backfill_until::text AS backfill_until,
        to_jsonb(s)->>'minute_covered_from' AS minute_covered_from,
        to_jsonb(s)->>'minute_covered_until' AS minute_covered_until
        FROM public.activity_retention_state s WHERE singleton"""
    try:
        if args.action == "status":
            row = await conn.fetchrow(status_sql, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
            print(json.dumps(dict(row), default=str))
            return
        if args.action == "backfill-minutes":
            print(json.dumps(await backfill_minutes(conn, hours=args.hours, max_batches=args.max_minute_batches)))
            return
        if args.action == "maintain":
            print(json.dumps(await maintain(conn)))
            return
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        await install(conn)
        if args.action == "backfill":
            await backfill(conn, max_days=args.max_days)
        elif args.action in ("enable", "disable"):
            if args.action == "enable" and not await conn.fetchval("SELECT summaries_ready FROM public.activity_retention_state WHERE singleton", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC):
                raise RuntimeError("complete backfill and dashboard deployment before enabling retention")
            await conn.execute("UPDATE public.activity_retention_state SET enabled=$1 WHERE singleton", args.action == "enable", timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        row = await conn.fetchrow(status_sql, timeout=MAINTENANCE_QUERY_TIMEOUT_SEC)
        print(json.dumps(dict(row), default=str))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "backfill", "backfill-minutes", "enable", "disable", "maintain", "status"))
    parser.add_argument("--max-days", type=int, help="backfill only: stop after this many day batches without declaring coverage complete")
    parser.add_argument("--hours", type=int, default=72, help="backfill-minutes: number of recent hours")
    parser.add_argument("--max-minute-batches", type=int, help="backfill-minutes: stop after this many half-hour batches")
    args = parser.parse_args()
    if args.max_days is not None and (args.action != "backfill" or args.max_days < 1):
        parser.error("--max-days requires backfill and a positive value")
    if args.action == "backfill-minutes" and (not 1 <= args.hours <= 168 or
            args.max_minute_batches is not None and args.max_minute_batches < 1):
        parser.error("backfill-minutes requires 1–168 hours and a positive batch limit")
    asyncio.run(main_async(args))
