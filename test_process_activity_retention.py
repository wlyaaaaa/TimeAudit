"""Real PostgreSQL regression tests, restricted to a disposable test database.

Set TIMEAUDIT_RETENTION_TEST_DSN to a database named timeaudit_retention_test*.
No production credentials, rows or database are used by these tests.
"""
import ast
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

import process_activity_retention as retention

ROOT = Path(__file__).resolve().parent
DSN = os.environ.get("TIMEAUDIT_RETENTION_TEST_DSN")


def instant(value):
    return dt.datetime.fromisoformat(value)


@unittest.skipUnless(DSN, "requires disposable PostgreSQL test database")
class RetentionPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.conn = await asyncpg.connect(DSN, server_settings={"timezone": "Asia/Shanghai"})
        database = await self.conn.fetchval("SELECT current_database()")
        if not database.startswith("timeaudit_retention_test"):
            await self.conn.close()
            raise RuntimeError("refusing to test in a non-test database")
        self.tx = self.conn.transaction()
        await self.tx.start()
        await self.conn.execute(ROOT.joinpath("schema.sql").read_text("utf-8").replace("OWNER TO leyang", "OWNER TO postgres"))
        await retention.install(self.conn)
        self.key = await self.conn.fetchval("""INSERT INTO dim_process_registry(process_name, executable_path)
            VALUES('example.exe', 'E:/apps/example.exe') RETURNING process_key""")

    async def asyncTearDown(self):
        if self.tx is not None:
            await self.tx.rollback()
        else:
            # Only the explicit pool-wiring test commits its synthetic schema.
            # asyncSetUp checked the disposable database identity first.
            await self.conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await self.conn.close()

    async def partition(self, name, start, end):
        await self.conn.execute(f"""CREATE TABLE {retention.quote_ident(name)} PARTITION OF fact_process_activity
            FOR VALUES FROM ('{start}') TO ('{end}')""")

    async def row(self, when, cpu=None, pid=1, key=None, **metrics):
        columns = ["timestamp", "process_key", "os_pid", "proc_cpu_usage", *metrics]
        values = [instant(when), self.key if key is None else key, pid, cpu, *metrics.values()]
        await self.conn.execute(f"INSERT INTO fact_process_activity ({','.join(columns)}) VALUES ({','.join('$'+str(i+1) for i in range(len(values)))})", *values)

    async def summary(self, start, end):
        await retention.refresh_hours(self.conn, instant(start), instant(end))

    async def test_weighted_days_nulls_multiple_pids_and_application_identity(self):
        await self.partition("old", "2026-06-01+08", "2026-06-03+08")
        second_key = await self.conn.fetchval("""INSERT INTO dim_process_registry(process_name, executable_path, command_line)
            VALUES('example.exe', 'E:/apps/example.exe', '--another-instance') RETURNING process_key""")
        await self.row("2026-06-01T00:00:00+08:00", 40, proc_gpu_usage=None)
        await self.row("2026-06-01T00:00:00+08:00", 30, pid=2, key=second_key)
        await self.row("2026-06-01T01:00:00+08:00", 10, proc_gpu_usage=5)
        await self.row("2026-06-01T01:00:03+08:00", None)
        await self.summary("2026-06-01T00:00:00+08:00", "2026-06-02T00:00:00+08:00")
        app = await self.conn.fetchrow("SELECT * FROM activity_app_day")
        self.assertEqual((4, 80, 3, 40), (app["sample_count"], app["proc_cpu_usage_sum"], app["proc_cpu_usage_count"], app["proc_cpu_usage_max"]))
        self.assertEqual((5, 1), (app["proc_gpu_usage_sum"], app["proc_gpu_usage_count"]))
        system = await self.conn.fetchrow("SELECT * FROM activity_system_day")
        self.assertEqual((3, 70, 2), (system["sample_count"], system["proc_cpu_usage_max"], system["proc_cpu_usage_count"]))
        self.assertEqual(4, system["proc_process_count_sum"])
        self.assertNotIn("process_key", dict(app))
        self.assertNotIn("command_line", dict(app))

    async def test_midday_partition_boundary_daily_rebuild_and_repeat_backfill(self):
        await self.partition("old_a", "2026-06-01 00:00+08", "2026-06-01 12:00+08")
        await self.partition("old_b", "2026-06-01 12:00+08", "2026-06-02 00:00+08")
        await self.row("2026-06-01T11:59:00+08:00", 40)
        await self.row("2026-06-01T12:00:00+08:00", 30)
        now = instant("2026-09-27T12:00:00+08:00")
        await retention.backfill(self.conn, now)
        before = await self.conn.fetch("SELECT * FROM activity_app_day")
        await retention.backfill(self.conn, now)
        self.assertEqual(before, await self.conn.fetch("SELECT * FROM activity_app_day"))
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true")
        result = await retention.maintain(self.conn, now)
        self.assertEqual(["old_a", "old_b"], result["dropped"])
        self.assertEqual(70, await self.conn.fetchval("SELECT proc_cpu_usage_sum FROM activity_app_day"))
        await retention.backfill(self.conn, now)
        self.assertEqual(70, await self.conn.fetchval("SELECT proc_cpu_usage_sum FROM activity_app_day"))

    async def test_old_recent_union_is_disjoint_and_partial_recent_window_exact(self):
        await self.partition("old", "2026-06-01+08", "2026-06-02+08")
        await self.partition("recent", "2026-09-01+08", "2026-10-01+08")
        await self.row("2026-06-01T00:05:00+08:00", 10)
        await self.row("2026-09-27T00:05:00+08:00", 20)
        await self.row("2026-09-27T00:45:00+08:00", 30)
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true")
        await retention.maintain(self.conn, instant("2026-09-27T12:00:00+08:00"))
        for step in (3, 3600, 86400, 172800):
            result = await self.conn.fetchrow("""SELECT sum(sample_count) AS n, sum(proc_cpu_usage_sum) AS s
              FROM activity_app_stats('2026-06-01+08','2026-09-27 00:30+08',$1)""", step)
            self.assertEqual((2, 30), (result["n"], result["s"]))
        self.assertEqual(30, await self.conn.fetchval("""SELECT sum(proc_cpu_usage_sum)
            FROM activity_app_stats('2026-09-27 00:30+08','2026-09-27 01:00+08',3)"""))

    async def test_empty_unknown_apps_and_estimates_do_not_bridge_sleep(self):
        await self.partition("old", "2026-06-01+08", "2026-06-03+08")
        await self.row("2026-06-01T00:00:00+08:00", 5, proc_disk_read_rate_mb=2)
        await self.row("2026-06-02T00:00:00+08:00", 5, key=999999, proc_disk_read_rate_mb=2)
        await retention.backfill(self.conn, instant("2026-06-03T00:00:00+08:00"))
        self.assertEqual(2, await self.conn.fetchval("SELECT count(DISTINCT process_name) FROM activity_app_hour"))
        self.assertAlmostEqual(12.4, await self.conn.fetchval("SELECT sum(proc_disk_read_rate_mb_sum)*3.1 FROM activity_app_hour"))
        self.assertIsNone(await self.conn.fetchval("SELECT sum(proc_gpu_usage_sum) FROM activity_app_hour"))
        self.assertEqual(0, await self.conn.fetchval("SELECT count(*) FROM activity_app_stats('2027-01-01','2027-01-02',3600)"))

    async def test_active_filter_does_not_erase_unfiltered_totals(self):
        await self.partition("old", "2026-06-01+08", "2026-06-02+08")
        await self.row("2026-06-01T00:00:00+08:00", 1, proc_disk_read_rate_mb=0.0005)
        await self.row("2026-06-01T00:00:03+08:00", 1, proc_disk_read_rate_mb=2)
        await self.summary("2026-06-01T00:00:00+08:00", "2026-06-02T00:00:00+08:00")
        row = await self.conn.fetchrow("SELECT * FROM activity_app_hour")
        self.assertAlmostEqual(2.0005, row["proc_disk_read_rate_mb_sum"])
        self.assertEqual((2, 1, 1), (row["active_proc_disk_read_rate_mb_sum"], row["active_proc_disk_read_rate_mb_count"], row["disk_sample_count"]))

    async def test_disabled_then_final_refresh_captures_late_rows_and_keeps_boundary(self):
        await self.partition("old", "2026-06-01+08", "2026-06-02+08")
        await self.partition("boundary", "2026-07-27+08", "2026-08-03+08")
        await self.row("2026-06-01T00:00:00+08:00", 10)
        now = instant("2026-09-27T12:00:00+08:00")
        await retention.backfill(self.conn, now)
        self.assertEqual([], (await retention.maintain(self.conn, now))["dropped"])
        await self.row("2026-06-01T00:00:03+08:00", 30)
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true")
        self.assertEqual(["old"], (await retention.maintain(self.conn, now))["dropped"])
        self.assertEqual(40, await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour"))
        self.assertIsNotNone(await self.conn.fetchval("SELECT to_regclass('boundary')"))

    async def test_drop_failure_rolls_back_summary_and_state(self):
        await self.partition("old", "2026-06-01+08", "2026-06-02+08")
        await self.row("2026-06-01T00:00:00+08:00", 10)
        await self.conn.execute("CREATE VIEW prevents_drop AS SELECT * FROM old")
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true")
        from asyncpg import DependentObjectsStillExistError
        with self.assertRaises(DependentObjectsStillExistError):
            await retention.maintain(self.conn, instant("2026-09-27T12:00:00+08:00"))
        self.assertEqual(1, await self.conn.fetchval("SELECT count(*) FROM old"))
        self.assertEqual(0, await self.conn.fetchval("SELECT count(*) FROM activity_app_hour"))
        self.assertEqual('-infinity', await self.conn.fetchval("SELECT raw_since::text FROM activity_retention_state"))

    async def test_recent_hourly_summary_uses_edges_without_losing_fine_resolution(self):
        await self.partition("recent", "2026-09-01+08", "2026-10-01+08")
        for when, cpu in (("00:05", 10), ("00:45", 20), ("01:05", 30), ("02:05", 40), ("02:45", 50)):
            await self.row(f"2026-09-27T{when}:00+08:00", cpu)
        await retention.backfill(self.conn, instant("2026-09-27T03:00:00+08:00"))
        for step in (3, 60, 3600, 5400, 86400):
            result = await self.conn.fetchrow("""SELECT sum(sample_count) AS n, sum(proc_cpu_usage_sum) AS s
                FROM activity_app_stats('2026-09-27 00:30+08','2026-09-27 02:30+08',$1)""", step)
            self.assertEqual((3, 90), (result["n"], result["s"]))
        fine = await self.conn.fetch("SELECT * FROM activity_system_stats('2026-09-27+08','2026-09-28+08',60)")
        self.assertEqual(5, len(fine))
        coarse = await self.conn.fetch("SELECT * FROM activity_system_stats('2026-09-27+08','2026-09-28+08',3600)")
        self.assertEqual(3, len(coarse))

    async def test_long_maintenance_gap_is_filled_before_advancing_coverage(self):
        await self.partition("recent", "2026-09-01+08", "2026-10-01+08")
        await self.row("2026-09-01T00:00:00+08:00", 10)
        await retention.backfill(self.conn, instant("2026-09-02T00:00:00+08:00"))
        await self.row("2026-09-10T00:00:00+08:00", 20)
        await retention.maintain(self.conn, instant("2026-09-27T12:00:00+08:00"))
        self.assertEqual(30, await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour"))
        self.assertEqual(30, await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_stats('2026-09-01+08','2026-09-28+08',86400)"))

    async def test_full_summarized_hours_prune_raw_partition_scan(self):
        await self.partition("recent", "2026-09-01+08", "2026-10-01+08")
        await self.row("2026-09-27T00:00:00+08:00", 10)
        await retention.backfill(self.conn, instant("2026-09-27T03:00:00+08:00"))
        result = await self.conn.fetchval("""EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT sum(proc_cpu_usage_sum) FROM activity_app_stats('2026-09-27 00:00+08','2026-09-27 03:00+08',3600)""")
        plan = json.loads(result)
        def walk(value):
            if isinstance(value, dict):
                if value.get('Relation Name') == 'recent':
                    self.assertEqual(0, value['Actual Rows'])
                    self.assertEqual(0, value.get('Rows Removed by Filter', 0))
                    if value['Actual Loops']:
                        self.assertIn(value['Node Type'], ('Index Scan', 'Index Only Scan', 'Bitmap Heap Scan'))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(plan)

    async def test_another_maintenance_owner_does_not_refresh_or_drop(self):
        import asyncpg
        other = await asyncpg.connect(DSN)
        try:
            await other.execute("SELECT pg_advisory_lock($1)", retention.LOCK_ID)
            result = await retention.maintain(self.conn)
            self.assertEqual({"busy": True, "dropped": []}, result)
        finally:
            await other.close()

    async def test_application_totals_survive_retirement_without_summing_separate_peaks(self):
        await self.partition("old", "2026-06-01+08", "2026-06-02+08")
        other_key = await self.conn.fetchval("""INSERT INTO dim_process_registry(process_name,executable_path)
            VALUES('other.exe','E:/apps/other.exe') RETURNING process_key""")
        await self.row("2026-06-01T00:00:00+08:00", 40, proc_gpu_usage=5)
        await self.row("2026-06-01T00:00:00+08:00", 30, pid=2)
        await self.row("2026-06-01T00:00:00+08:00", 20, pid=3, key=other_key)
        await self.row("2026-06-01T01:00:00+08:00", 40)
        await self.row("2026-06-01T01:00:03+08:00", 30, pid=2)
        await retention.backfill(self.conn, instant("2026-06-02T00:00:00+08:00"))
        hourly = await self.conn.fetch("SELECT * FROM activity_app_hour WHERE process_name='example.exe' ORDER BY bucket_start")
        self.assertEqual([70, 40], [r['app_total_proc_cpu_usage_max'] for r in hourly])
        daily = await self.conn.fetchrow("SELECT * FROM activity_app_day WHERE process_name='example.exe'")
        self.assertEqual((140, 3, 70), (daily['app_total_proc_cpu_usage_sum'], daily['app_total_proc_cpu_usage_count'], daily['app_total_proc_cpu_usage_max']))
        self.assertEqual((5, 1), (daily['app_total_proc_gpu_usage_sum'], daily['app_total_proc_gpu_usage_count']))
        self.assertEqual(40, daily['proc_cpu_usage_max'])
        self.assertEqual(90, await self.conn.fetchval("SELECT proc_cpu_usage_max FROM activity_system_day"))
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true")
        await retention.maintain(self.conn, instant("2026-09-27T12:00:00+08:00"))
        self.assertEqual(70, await self.conn.fetchval("""SELECT max(app_total_proc_cpu_usage_max)
            FROM activity_app_stats('2026-06-01+08','2026-06-02+08',86400) WHERE process_name='example.exe'"""))

    def chart_queries(self, start, end, interval):
        for path in ROOT.joinpath('grafana_dashboards').glob('*.json'):
            dashboard = json.loads(path.read_text('utf-8'))
            for panel in dashboard.get('panels', []):
                for target in panel.get('targets', []):
                    sql = target.get('rawSql', '')
                    if 'activity_system_stats' in sql or (panel.get('type') == 'timeseries' and 'activity_app_stats(' in sql):
                        yield panel, sql.replace('$__timeFrom()', repr(start)).replace('$__timeTo()', repr(end)).replace('$__interval', interval)

    async def test_chart_missing_hours_and_days_have_null_breaks(self):
        await self.partition('old', '2026-06-01+08', '2026-06-05+08')
        await self.row('2026-06-01T00:00:00+08:00', 10, proc_ram_mb=100)
        await self.row('2026-06-01T02:00:00+08:00', 20, proc_ram_mb=200)
        await self.row('2026-06-03T00:00:00+08:00', 30, proc_ram_mb=300)
        for retired in (False, True):
            if retired:
                await retention.backfill(self.conn, instant('2026-06-05T00:00:00+08:00'))
                await self.conn.execute('UPDATE activity_retention_state SET enabled=true')
                await retention.maintain(self.conn, instant('2026-09-27T12:00:00+08:00'))
            for panel, sql in self.chart_queries('2026-06-01 00:00+08', '2026-06-01 03:00+08', '1h'):
                rows = await self.conn.fetch(sql)
                breaks = [r for r in rows if r['time'] == instant('2026-06-01T01:00:00+08:00')]
                self.assertTrue(breaks, panel['title'])
                if (panel.get('type') == 'timeseries' and 'activity_app_stats(' in sql):
                    self.assertEqual({r['metric'] for r in rows}, {r['metric'] for r in breaks})
                for row in breaks:
                    self.assertTrue(all(v is None for k, v in row.items() if k not in ('time', 'metric')), panel['title'])
        for panel, sql in self.chart_queries('2026-06-01 00:00+08', '2026-07-02 00:00+08', '1d'):
            rows = await self.conn.fetch(sql)
            self.assertTrue(any(r['time'] == instant('2026-06-02T00:00:00+08:00') for r in rows), panel['title'])

    async def test_first_backfill_resumes_completed_batches_without_claiming_complete(self):
        await self.partition('old', '2026-06-01+08', '2026-06-03+08')
        await self.row('2026-06-01T00:00:00+08:00', 10)
        await self.row('2026-06-02T00:00:00+08:00', 20)
        result = await retention.backfill(self.conn, instant('2026-06-03T00:00:00+08:00'), max_days=1)
        self.assertFalse(result['complete'])
        self.assertFalse(await self.conn.fetchval('SELECT summaries_ready FROM activity_retention_state'))
        original = retention.refresh_hours
        with mock.patch.object(retention, 'refresh_hours', wraps=original) as refresh:
            await retention.backfill(self.conn, instant('2026-06-03T00:00:00+08:00'))
            self.assertEqual(1, refresh.await_count)
            self.assertEqual(instant('2026-06-02T00:00:00+08:00'), refresh.await_args.args[1])
        self.assertTrue(await self.conn.fetchval('SELECT summaries_ready FROM activity_retention_state'))
        self.assertEqual(30, await self.conn.fetchval('SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour'))

    async def test_real_main_maintenance_overrides_pool_timeout_without_changing_collector(self):
        import asyncpg
        tree = ast.parse(ROOT.joinpath('main.py').read_text('utf-8-sig'))
        pools = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'create_pool']
        self.assertEqual([5.0, 5.0], [ast.literal_eval(next(k.value for k in n.keywords if k.arg == 'command_timeout')) for n in pools])
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'auto_retention_cleanup')
        namespace = {'maintain_activity_retention': retention.maintain, 'RETENTION_DAYS': 0}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / 'main.py'), 'exec'), namespace)
        await self.partition('recent', '2000-01-01+08', '2100-01-01+08')
        now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
        await self.row(now.isoformat(), 10)
        await self.conn.execute("""CREATE FUNCTION slow_summary() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep(0.05); RETURN NULL; END $$;
            CREATE TRIGGER slow_summary BEFORE INSERT ON activity_app_hour FOR EACH STATEMENT EXECUTE FUNCTION slow_summary()""")
        await self.tx.commit()
        self.tx = None
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, command_timeout=0.01)
        try:
            async with pool.acquire() as conn:
                with self.assertRaises(asyncio.TimeoutError):
                    await conn.execute('SELECT pg_sleep(0.05)')
            self.assertTrue(await namespace['auto_retention_cleanup'](pool))
            self.assertEqual(10, await self.conn.fetchval('SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour'))
            async with pool.acquire() as conn:
                with self.assertRaises(asyncio.TimeoutError):
                    await conn.execute('SELECT pg_sleep(0.05)')
        finally:
            try:
                await pool.close()
            except asyncio.TimeoutError:
                # The deliberately 10ms default also bounds asyncpg.close().
                pool.terminate()

    async def test_cancelled_maintenance_rolls_back_current_summary_batch(self):
        await self.partition('recent', '2026-09-01+08', '2026-10-01+08')
        await self.row('2026-09-27T00:00:00+08:00', 10)
        await self.summary('2026-09-27T00:00:00+08:00', '2026-09-27T01:00:00+08:00')
        await self.row('2026-09-27T00:00:03+08:00', 30)
        await self.conn.execute("""CREATE FUNCTION slow_summary() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep(0.3); RETURN NULL; END $$;
            CREATE TRIGGER slow_summary BEFORE INSERT ON activity_app_hour FOR EACH STATEMENT EXECUTE FUNCTION slow_summary()""")
        with mock.patch.object(retention, 'install', new=mock.AsyncMock()):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(retention.maintain(self.conn, instant('2026-09-28T00:00:00+08:00')), timeout=0.05)
        self.assertEqual(10, await self.conn.fetchval('SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour'))
        self.assertEqual(2, await self.conn.fetchval('SELECT count(*) FROM fact_process_activity'))

    async def test_identity_lookup_is_bounded_to_keys_in_the_activity_window(self):
        await self.partition('recent', '2026-09-01+08', '2026-10-01+08')
        await self.conn.execute("""INSERT INTO dim_process_registry(process_name,executable_path)
            SELECT 'unused-' || n || '.exe', 'E:/unused/' || n FROM generate_series(1,5000) n""")
        await self.conn.execute('ANALYZE dim_process_registry')
        for second in range(20):
            await self.row(f'2026-09-27T00:00:{second:02d}+08:00', 10)
        await self.row('2026-09-27T00:00:00+08:00', 20, key=999999)
        raw = retention.raw_source(bounded_identities=True).replace('$1', "'2026-09-27+08'::timestamptz").replace('$2', "'2026-09-28+08'::timestamptz")
        plan = json.loads(await self.conn.fetchval('EXPLAIN (ANALYZE, FORMAT JSON) SELECT sum(app_total_proc_cpu_usage) FROM (' + raw + ') samples'))
        lookups = []
        def walk(node):
            if isinstance(node, dict):
                if node.get('Relation Name') == 'dim_process_registry':
                    lookups.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(plan)
        self.assertTrue(lookups)
        self.assertTrue(all(n['Node Type'] in ('Index Scan', 'Index Only Scan') for n in lookups))
        self.assertEqual(2, sum(n['Actual Loops'] for n in lookups))
        await self.summary('2026-09-27T00:00:00+08:00', '2026-09-28T00:00:00+08:00')
        self.assertEqual(220, await self.conn.fetchval('SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour'))
        self.assertEqual(2, await self.conn.fetchval('SELECT count(*) FROM activity_app_hour'))

    async def test_reader_boundaries_match_direct_samples_for_every_statistic(self):
        await self.partition('recent', '2026-09-01+08', '2026-10-01+08')
        another_key = await self.conn.fetchval("""INSERT INTO dim_process_registry(process_name,executable_path,command_line)
            VALUES('example.exe','E:/apps/example.exe','--second-key') RETURNING process_key""")
        await self.row('2026-09-27T00:05:00+08:00', 99)  # Outside the window.
        await self.row('2026-09-27T00:45:00+08:00', 10, proc_ram_mb=100, proc_disk_read_rate_mb=0.0005)
        await self.row('2026-09-27T00:45:00+08:00', 20, pid=2, key=another_key, proc_ram_mb=None)
        await self.row('2026-09-27T01:00:00+08:00', 30, proc_gpu_usage=None, proc_network_send_kb=5)
        await self.row('2026-09-27T02:00:00+08:00', None, key=999998, proc_ram_mb=200)
        await self.row('2026-09-27T03:00:00+08:00', 40, key=999999, proc_disk_write_rate_mb=1)
        await self.row('2026-09-27T03:45:00+08:00', 99)  # Outside the window.
        await retention.backfill(self.conn, instant('2026-09-27T03:00:00+08:00'))
        start, end = instant('2026-09-27T00:30:00+08:00'), instant('2026-09-27T03:30:00+08:00')
        for step in (60, 3600, 86400):
            expected_sql = f"""SELECT date_bin(make_interval(secs => {step}),timestamp,'2000-01-01+08'::timestamptz),
                process_name,executable_path,{retention.aggregate_columns()}
                FROM ({retention.raw_source()}) samples GROUP BY 1,process_name,executable_path ORDER BY 1,2,3"""
            expected = await self.conn.fetch(expected_sql, start, end)
            actual = await self.conn.fetch(f"""SELECT bucket_start,process_name,executable_path,{retention.aggregate_columns(merge=True)}
                FROM activity_app_stats($1,$2,$3) GROUP BY 1,process_name,executable_path ORDER BY 1,2,3""", start, end, step)
            self.assertEqual([tuple(row) for row in expected], [tuple(row) for row in actual], step)

    async def test_empty_retired_partial_hour_and_inverted_windows_are_empty(self):
        await self.partition('old', '2026-06-01+08', '2026-06-02+08')
        await self.row('2026-06-01T00:05:00+08:00', 10)
        await retention.backfill(self.conn, instant('2026-06-02T00:00:00+08:00'))
        await self.conn.execute('UPDATE activity_retention_state SET enabled=true')
        await retention.maintain(self.conn, instant('2026-09-27T12:00:00+08:00'))
        for kind in ('app','system'):
            for start,end in (('00:30','00:30'),('00:45','00:30')):
                rows = await self.conn.fetch(f"SELECT * FROM activity_{kind}_stats($1,$2,3600)",
                    instant(f'2026-06-01T{start}:00+08:00'), instant(f'2026-06-01T{end}:00+08:00'))
                self.assertEqual([], rows)

    async def test_reader_raw_head_and_tail_only_lookup_window_identities(self):
        await self.partition('recent', '2026-09-01+08', '2026-10-01+08')
        await self.conn.execute("""INSERT INTO dim_process_registry(process_name,executable_path)
            SELECT 'unused-' || n || '.exe', 'E:/unused/' || n FROM generate_series(1,5000) n""")
        await self.conn.execute('ANALYZE dim_process_registry')
        await self.row('2026-09-27T00:45:00+08:00', 10)
        await self.row('2026-09-27T01:00:00+08:00', 20)
        await self.row('2026-09-27T03:00:00+08:00', 30, key=999999)
        await retention.backfill(self.conn, instant('2026-09-27T03:00:00+08:00'))
        plan=json.loads(await self.conn.fetchval("""EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT sum(app_total_proc_cpu_usage_sum)
            FROM activity_app_stats('2026-09-27 00:30+08','2026-09-27 03:30+08',3600)"""))
        lookups=[]
        def walk(node):
            if isinstance(node,dict):
                if node.get('Relation Name')=='dim_process_registry':lookups.append(node)
                for child in node.values():walk(child)
            elif isinstance(node,list):
                for child in node:walk(child)
        walk(plan)
        self.assertEqual(2,len(lookups))
        self.assertTrue(all(node['Node Type'] in ('Index Scan','Index Only Scan') for node in lookups))
        self.assertEqual(2,sum(node['Actual Loops'] for node in lookups))

    async def test_application_trend_projection_keeps_paths_and_mean_peak_series(self):
        await self.partition('recent', '2026-09-01+08', '2026-10-01+08')
        second=await self.conn.fetchval("""INSERT INTO dim_process_registry(process_name,executable_path)
            VALUES('example.exe','E:/other-version/example.exe') RETURNING process_key""")
        await self.row('2026-09-27T00:00:00+08:00',10,proc_ram_mb=100)
        await self.row('2026-09-27T00:00:00+08:00',20,key=second,proc_ram_mb=200)
        for panel,sql in self.chart_queries('2026-09-27+08','2026-09-28+08','1h'):
            if 'activity_app_stats(' not in sql:continue
            rows=await self.conn.fetch(sql)
            self.assertEqual(4,len(rows),panel['title'])
            self.assertEqual({'均值 · example.exe · E:/apps/example.exe','峰值 · example.exe · E:/apps/example.exe',
                              '均值 · example.exe · E:/other-version/example.exe','峰值 · example.exe · E:/other-version/example.exe'},
                             {row['metric'] for row in rows})

    async def test_dashboard_queries_compile_in_postgresql(self):
        checked = 0
        for path in ROOT.joinpath("grafana_dashboards").glob("*.json"):
            dashboard = json.loads(path.read_text("utf-8"))
            for panel in dashboard.get("panels", []):
                for target in panel.get("targets", []):
                    sql = target.get("rawSql", "")
                    if not any(f"activity_{kind}_stats" in sql for kind in ("app", "system")):
                        continue
                    sql = sql.replace("$__timeFrom()", "'2026-06-01 00:00+08'")
                    sql = sql.replace("$__timeTo()", "'2026-09-27 00:00+08'")
                    sql = sql.replace("$__interval", "1h")
                    await self.conn.fetch(sql)
                    checked += 1
        self.assertEqual(13, checked)


if __name__ == "__main__":
    unittest.main()
