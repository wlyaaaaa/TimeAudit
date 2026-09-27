"""Real PostgreSQL regression tests, restricted to a disposable test database.

Set TIMEAUDIT_RETENTION_TEST_DSN to a database named timeaudit_retention_test*.
No production credentials, rows or database are used by these tests.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import unittest

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
        await self.tx.rollback()
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
