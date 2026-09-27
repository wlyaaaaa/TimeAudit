"""Three-connection PostgreSQL tests for retirement lock ordering.

Only the disposable database validated by RetentionPostgresTests is used.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest import mock

import process_activity_retention as retention
import test_process_activity_retention as fixtures
from test_process_activity_retention import DSN, ROOT, instant

NOW = instant('2026-09-27T12:00:00+08:00')
OLD_START = instant('2026-06-01T00:00:00+08:00')


@unittest.skipUnless(DSN, 'requires disposable PostgreSQL test database')
class PartitionRetirementLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.fixture = fixtures.RetentionPostgresTests()
        await self.fixture.asyncSetUp()
        self.conn = self.fixture.conn
        await self.fixture.partition('retention_prefix', '2026-05-31+08', '2026-06-01+08')
        await self.fixture.partition('retention_old', '2026-06-01+08', '2026-06-02+08')
        await self.fixture.partition('retention_later', '2026-06-02+08', '2026-06-03+08')
        await self.fixture.partition('retention_current', '2026-09-01+08', '2026-10-01+08')
        await self.fixture.row('2026-06-01T00:00:00+08:00', 10)
        await self.fixture.row('2026-06-02T00:00:00+08:00', 50)
        await self.fixture.row('2026-09-27T00:00:00+08:00', 1)
        await self.fixture.summary('2026-06-01T00:00:00+08:00', '2026-06-03T00:00:00+08:00')
        await self.conn.execute("UPDATE activity_retention_state SET enabled=true,summaries_ready=true,summarized_until=$1", NOW)
        await self.fixture.tx.commit()
        self.fixture.tx = None
        self.other = await asyncpg.connect(DSN)
        self.monitor = await asyncpg.connect(DSN)
        self.pending = []

    async def asyncTearDown(self):
        for task in self.pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        await self.other.close(timeout=2)
        await self.monitor.close(timeout=2)
        await self.fixture.asyncTearDown()

    async def current_insert(self):
        await asyncio.wait_for(self.monitor.execute("""INSERT INTO fact_process_activity
            (timestamp,process_key,os_pid,proc_cpu_usage)
            VALUES ('2026-09-27 00:00:03+08',$1,1,3)""", self.fixture.key), timeout=2)

    async def assert_summary_phase_locks(self, pid):
        locks = await self.monitor.fetch("""SELECT relation::regclass::text AS name,mode,granted
            FROM pg_locks WHERE pid=$1 AND relation IN
            ('fact_process_activity'::regclass,'retention_old'::regclass,'retention_current'::regclass)""", pid)
        self.assertTrue(any(r['name']=='retention_old' and r['mode']=='ShareLock' and r['granted'] for r in locks))
        self.assertFalse(any(r['name']=='fact_process_activity' and r['mode']=='AccessExclusiveLock' for r in locks))
        self.assertFalse(any(r['name']=='retention_current' and r['mode'] in ('ShareLock','AccessExclusiveLock') for r in locks))

    @asynccontextmanager
    async def paused_maintenance(self, runner=None):
        paused, resume = asyncio.Event(), asyncio.Event()
        original = retention.refresh_hours
        async def refresh(conn, start, end):
            await original(conn, start, end)
            if start == OLD_START:
                paused.set()
                await resume.wait()
        with mock.patch.object(retention, 'refresh_hours', side_effect=refresh):
            task = asyncio.create_task(runner() if runner else retention.maintain(self.conn, NOW))
            self.pending.append(task)
            try:
                await asyncio.wait_for(paused.wait(), timeout=10)
                await self.assert_summary_phase_locks(self.conn.get_server_pid())
                yield task, resume
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def assert_busy_rollback(self):
        self.assertEqual(OLD_START, await self.conn.fetchval('SELECT raw_since FROM activity_retention_state'))
        self.assertIsNone(await self.conn.fetchval("SELECT to_regclass('retention_prefix')"))
        for name in ('retention_old','retention_later','retention_current'):
            self.assertIsNotNone(await self.conn.fetchval('SELECT to_regclass($1)', name))
        self.assertEqual(10, await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour WHERE bucket_start >= '2026-06-01+08' AND bucket_start < '2026-06-02+08'"))

    async def test_parent_reader_continues_and_busy_uses_existing_main_retry_signal(self):
        # A late row already exists; failed final locking must roll its new
        # summary back, while retaining the committed empty prefix retirement.
        await self.fixture.row('2026-06-01T00:00:03+08:00', 20)
        tree = ast.parse(ROOT.joinpath('main.py').read_text('utf-8-sig'))
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name=='auto_retention_cleanup')
        async def run_retention(conn):
            return await retention.maintain(conn, NOW)
        class Pool:
            @asynccontextmanager
            async def acquire(inner):
                yield self.conn
        namespace = {'maintain_activity_retention':run_retention,'RETENTION_DAYS':0}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(ROOT/'main.py'),'exec'),namespace)
        async with self.paused_maintenance(lambda: namespace['auto_retention_cleanup'](Pool())) as (task,resume):
            reader = self.other.transaction()
            await reader.start()
            try:
                count = await asyncio.wait_for(self.other.fetchval("""SELECT count(*) FROM fact_process_activity
                    WHERE timestamp >= '2026-06-01+08' AND timestamp < '2026-06-02+08'"""), timeout=2)
                self.assertEqual(2,count)
                await self.current_insert()
                resume.set()
                self.assertFalse(await asyncio.wait_for(task,timeout=2))
                await self.assert_busy_rollback()
            finally:
                await reader.rollback()
        result = await retention.maintain(self.conn,NOW)
        self.assertEqual(['retention_old','retention_later'],result['dropped'])
        self.assertEqual(30,await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour WHERE bucket_start < '2026-06-02+08'"))

    async def test_late_writer_holds_parent_without_creating_a_deadlock(self):
        await self.late_writer_case('fact_process_activity')

    async def test_direct_child_late_writer_does_not_get_overtaken_by_drop(self):
        await self.late_writer_case('retention_old')

    async def late_writer_case(self, target):
        from asyncpg import LockNotAvailableError
        async with self.paused_maintenance() as (task,resume):
            writer = asyncio.create_task(self.other.execute(f"""INSERT INTO {retention.quote_ident(target)}
                (timestamp,process_key,os_pid,proc_cpu_usage)
                VALUES ('2026-06-01 00:00:03+08',$1,1,20)""",self.fixture.key,timeout=10))
            self.pending.append(writer)
            async def wait_for_lock_order():
                while True:
                    locks = await self.monitor.fetch("""SELECT relation::regclass::text AS name,mode,granted
                        FROM pg_locks WHERE pid=$1 AND relation IN
                        ('fact_process_activity'::regclass,'retention_old'::regclass)""",self.other.get_server_pid())
                    parent = any(r['name']=='fact_process_activity' and r['mode']=='RowExclusiveLock' and r['granted'] for r in locks)
                    child = any(r['name']=='retention_old' and r['mode']=='RowExclusiveLock' and not r['granted'] for r in locks)
                    if child and parent == (target == 'fact_process_activity'):return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_lock_order(),timeout=2)
            await self.current_insert()
            resume.set()
            with self.assertRaises(LockNotAvailableError):
                await asyncio.wait_for(task,timeout=2)
            await asyncio.wait_for(writer,timeout=2)
        await self.assert_busy_rollback()
        self.assertEqual(2,await self.conn.fetchval('SELECT count(*) FROM retention_old'))
        result = await retention.maintain(self.conn,NOW)
        self.assertEqual(['retention_old','retention_later'],result['dropped'])
        self.assertEqual(30,await self.conn.fetchval("SELECT sum(proc_cpu_usage_sum) FROM activity_app_hour WHERE bucket_start < '2026-06-02+08'"))
        self.assertEqual(2,await self.conn.fetchval('SELECT count(*) FROM retention_current'))

    async def test_direct_child_reader_causes_nonwaiting_child_upgrade_rollback(self):
        from asyncpg import LockNotAvailableError
        async with self.paused_maintenance() as (task,resume):
            reader = self.other.transaction()
            await reader.start()
            try:
                await self.other.execute('LOCK TABLE ONLY retention_old IN ACCESS SHARE MODE')
                parent_count = await self.monitor.fetchval("SELECT count(*) FROM pg_locks WHERE pid=$1 AND relation='fact_process_activity'::regclass",self.other.get_server_pid())
                self.assertEqual(0,parent_count)
                await self.current_insert()
                resume.set()
                with self.assertRaises(LockNotAvailableError):
                    await asyncio.wait_for(task,timeout=2)
                await self.assert_busy_rollback()
                exclusive = await self.monitor.fetchval("""SELECT count(*) FROM pg_locks WHERE pid=$1
                    AND relation='fact_process_activity'::regclass AND mode='AccessExclusiveLock'""",self.conn.get_server_pid())
                self.assertEqual(0,exclusive)
            finally:
                await reader.rollback()
        self.assertEqual(['retention_old','retention_later'],(await retention.maintain(self.conn,NOW))['dropped'])


if __name__=='__main__':
    unittest.main()
