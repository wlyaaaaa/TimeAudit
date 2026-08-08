# -*- coding: utf-8 -*-
import datetime
import unittest

from context_worker import WindowStateTracker


UTC = datetime.timezone.utc


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeConnection:
    def __init__(
        self,
        *,
        fetch_failure=None,
        execute_failure=None,
        fetch_value=101,
    ):
        self.fetch_failure = fetch_failure
        self.execute_failure = execute_failure
        self.fetch_value = fetch_value
        self.fetches = []
        self.executions = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query, *args):
        if self.fetch_failure is not None:
            raise self.fetch_failure
        self.fetches.append((query, args))
        return self.fetch_value

    async def execute(self, query, *args):
        if self.execute_failure is not None:
            raise self.execute_failure
        self.executions.append((query, args))


class _FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def _metadata(*, process_name="test.exe", process_create_time=100.0):
    return {
        "process_name": process_name,
        "executable_path": process_name,
        "parent_process": None,
        "command_line": "",
        "service_name": None,
        "is_elevated": 0,
        "signature_status": 0,
        "process_create_time": process_create_time,
    }


class ContextWorkerHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_foreground_window_is_a_healthy_poll(self):
        tracker = WindowStateTracker()
        tracker.check_foreground_window_fast = lambda: None

        healthy = await tracker.poll_heartbeat(
            _FakePool(_FakeConnection()),
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
        )

        self.assertIs(healthy, True)

    async def test_no_foreground_does_not_hide_pending_database_work(self):
        tracker = WindowStateTracker()
        tracker.pending_updates.append({"test": "pending"})
        tracker.check_foreground_window_fast = lambda: None

        healthy = await tracker.poll_heartbeat(
            _FakePool(_FakeConnection()),
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
        )

        self.assertIs(healthy, False)

    async def test_focus_change_updates_tracker_before_poll_reports_healthy(self):
        tracker = WindowStateTracker()
        tracker.check_foreground_window_fast = lambda: {
            "hwnd": 1,
            "os_pid": 202,
            "window_title": "test-window",
            "window_mode": 2,
        }
        tracker.harvest_process_metadata = lambda pid: _metadata()
        sample_time = datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)

        healthy = await tracker.poll_heartbeat(
            _FakePool(_FakeConnection()),
            sample_time,
        )

        self.assertIs(healthy, True)
        self.assertEqual(202, tracker.last_pid)
        self.assertEqual(sample_time, tracker.active_slice["timestamp"])

    async def test_context_database_failure_is_reported_as_unhealthy(self):
        tracker = WindowStateTracker()
        tracker.check_foreground_window_fast = lambda: {
            "hwnd": 1,
            "os_pid": 303,
            "window_title": "test-window",
            "window_mode": 2,
        }
        tracker.harvest_process_metadata = lambda pid: _metadata()

        healthy = await tracker.poll_heartbeat(
            _FakePool(
                _FakeConnection(execute_failure=RuntimeError("test failure"))
            ),
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
        )

        self.assertIs(healthy, False)
        self.assertEqual(1, len(tracker.pending_inserts))
        self.assertIsNone(tracker.pending_inserts[0]["process_key"])
        self.assertNotIn(303, tracker.last_pid_key_map)
        self.assertIsNone(tracker.active_slice["process_key"])

    async def test_reused_pid_registers_new_process_identity(self):
        tracker = WindowStateTracker()
        pid = 404
        old_time = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        tracker.last_pid = pid
        tracker.last_title = "old-window"
        tracker.last_pid_key_map[pid] = 17
        tracker.active_slice = {
            "timestamp": old_time,
            "os_pid": pid,
            "window_title": "old-window",
            "window_mode": 2,
            "metadata": _metadata(
                process_name="old.exe",
                process_create_time=100.0,
            ),
            "process_key": 17,
        }
        tracker.check_foreground_window_fast = lambda: {
            "hwnd": 1,
            "os_pid": pid,
            "window_title": "new-window",
            "window_mode": 2,
        }
        tracker.harvest_process_metadata = lambda current_pid: _metadata(
            process_name="new.exe",
            process_create_time=200.0,
        )
        connection = _FakeConnection(fetch_value=202)

        healthy = await tracker.poll_heartbeat(
            _FakePool(connection),
            old_time + datetime.timedelta(seconds=1),
        )

        self.assertIs(healthy, True)
        self.assertEqual(1, len(connection.fetches))
        context_insert = next(
            args
            for query, args in connection.executions
            if "INSERT INTO public.fact_process_context" in query
        )
        self.assertEqual(202, context_insert[1])
        self.assertEqual(202, tracker.active_slice["process_key"])
        self.assertEqual(202, tracker.last_pid_key_map[pid])

    async def test_same_pid_and_title_with_new_hwnd_revalidates_identity(self):
        tracker = WindowStateTracker()
        pid = 454
        old_time = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        tracker.last_pid = pid
        tracker.last_title = "same-window"
        tracker.last_hwnd = 1
        tracker.last_pid_key_map[pid] = 17
        tracker._last_pid_identity_map[pid] = (
            100.0,
            "old.exe",
            "old.exe",
            "",
        )
        tracker.active_slice = {
            "timestamp": old_time,
            "os_pid": pid,
            "window_title": "same-window",
            "window_mode": 2,
            "metadata": _metadata(
                process_name="old.exe",
                process_create_time=100.0,
            ),
            "process_key": 17,
        }
        tracker.check_foreground_window_fast = lambda: {
            "hwnd": 2,
            "os_pid": pid,
            "window_title": "same-window",
            "window_mode": 2,
        }
        tracker.harvest_process_metadata = lambda current_pid: _metadata(
            process_name="new.exe",
            process_create_time=200.0,
        )
        connection = _FakeConnection(fetch_value=252)

        healthy = await tracker.poll_heartbeat(
            _FakePool(connection),
            old_time + datetime.timedelta(seconds=1),
        )

        self.assertIs(healthy, True)
        self.assertEqual(1, len(connection.fetches))
        self.assertEqual(252, tracker.active_slice["process_key"])
        self.assertEqual(2, tracker.last_hwnd)

    async def test_title_change_reuses_key_for_same_stable_process_identity(self):
        tracker = WindowStateTracker()
        pid = 505
        foreground = {
            "hwnd": 1,
            "os_pid": pid,
            "window_title": "first-title",
            "window_mode": 2,
        }
        tracker.check_foreground_window_fast = lambda: foreground.copy()
        tracker.harvest_process_metadata = lambda current_pid: _metadata(
            process_name="stable.exe",
            process_create_time=300.0,
        )
        connection = _FakeConnection(fetch_value=303)
        pool = _FakePool(connection)
        first_time = datetime.datetime(2026, 1, 1, tzinfo=UTC)

        self.assertIs(await tracker.poll_heartbeat(pool, first_time), True)
        foreground["window_title"] = "second-title"
        self.assertIs(
            await tracker.poll_heartbeat(
                pool,
                first_time + datetime.timedelta(seconds=1),
            ),
            True,
        )

        self.assertEqual(1, len(connection.fetches))
        self.assertEqual(303, tracker.active_slice["process_key"])

    async def test_foreground_process_switch_resolves_fresh_metadata(self):
        tracker = WindowStateTracker()
        foreground = {
            "hwnd": 1,
            "os_pid": 601,
            "window_title": "process-a",
            "window_mode": 2,
        }
        tracker.check_foreground_window_fast = lambda: foreground.copy()
        tracker.harvest_process_metadata = lambda current_pid: _metadata(
            process_name=f"process-{current_pid}.exe",
            process_create_time=float(current_pid),
        )
        connection = _FakeConnection(fetch_value=404)
        pool = _FakePool(connection)
        first_time = datetime.datetime(2026, 1, 1, tzinfo=UTC)

        self.assertIs(await tracker.poll_heartbeat(pool, first_time), True)
        foreground.update(os_pid=602, window_title="process-b")
        self.assertIs(
            await tracker.poll_heartbeat(
                pool,
                first_time + datetime.timedelta(seconds=1),
            ),
            True,
        )
        foreground.update(os_pid=601, window_title="process-a-again")
        self.assertIs(
            await tracker.poll_heartbeat(
                pool,
                first_time + datetime.timedelta(seconds=2),
            ),
            True,
        )

        self.assertEqual(3, len(connection.fetches))


if __name__ == "__main__":
    unittest.main()
