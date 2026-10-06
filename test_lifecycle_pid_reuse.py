# -*- coding: utf-8 -*-
import datetime
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from lifecycle_worker import ProcessLifecycleWorker


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self):
        self.fetches = []
        self.executions = []

    async def fetchval(self, query, *args):
        self.fetches.append(args)
        # Make process identities visibly distinct in the assertions below.
        return 11 if args[0] == "old.exe" else 22

    async def execute(self, query, *args):
        self.executions.append(args)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def _metadata(name):
    return {
        "name": name,
        "exe": f"C:\\{name}",
        "parent_name": None,
        "cmdline": "",
        "service_name": None,
        "is_elevated": 0,
        "signature_status": 0,
    }


class LifecyclePidReuseTest(unittest.IsolatedAsyncioTestCase):
    def test_zero_thread_crash_object_releases_handle(self):
        worker = ProcessLifecycleWorker({})
        worker.is_running = True
        worker.pid_handles[(42, 100.0)] = 99
        snapshots = [
            [SimpleNamespace(info=dict(pid=42, name="crash.exe", create_time=100.0, num_threads=n))]
            for n in (1, 0)
        ]
        loop = SimpleNamespace(call_soon_threadsafe=lambda fn, event: fn(event))
        def snapshot(_):
            worker.is_running = len(snapshots) > 1
            return snapshots.pop(0)
        with ExitStack() as stack:
            stack.enter_context(patch("activity_worker.fetch_system_processes", return_value=None))
            stack.enter_context(patch("lifecycle_worker.psutil.process_iter", side_effect=snapshot))
            stack.enter_context(patch("lifecycle_worker.time.sleep"))
            stack.enter_context(patch("lifecycle_worker.kernel32.GetExitCodeProcess", return_value=0))
            close = stack.enter_context(patch("lifecycle_worker.kernel32.CloseHandle"))
            worker._differential_scanner_loop(loop)
        self.assertEqual({}, worker.pid_handles)
        close.assert_called_once_with(99)
        self.assertEqual("EXIT", worker.event_queue.get_nowait()["type"])

    async def test_old_exit_stays_bound_when_pid_is_reused(self):
        shared_map = {}
        worker = ProcessLifecycleWorker(shared_map)
        connection = _Connection()
        pool = _Pool(connection)
        old_start = datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc)

        await worker._process_queued_event(
            pool,
            {
                "type": "START",
                "os_pid": 4242,
                "create_time": 100.0,
                "instance_key": (4242, 100.0),
                "name": "old.exe",
                "exe": r"C:\old.exe",
                "metadata": _metadata("old.exe"),
                "timestamp": old_start,
            },
        )

        # The scanner can observe START(new) and EXIT(old) in one differential
        # pass.  Process the replacement START before the old EXIT to reproduce
        # the former PID-only misattribution.
        await worker._process_queued_event(
            pool,
            {
                "type": "START",
                "os_pid": 4242,
                "create_time": 200.0,
                "instance_key": (4242, 200.0),
                "name": "new.exe",
                "exe": r"C:\new.exe",
                "metadata": _metadata("new.exe"),
                "timestamp": old_start + datetime.timedelta(seconds=10),
            },
        )

        await worker._process_queued_event(
            pool,
            {
                "type": "EXIT",
                "os_pid": 4242,
                "create_time": 100.0,
                "instance_key": (4242, 100.0),
                "lifetime_sec": 10,
                "exit_code": "0x00000000",
                "timestamp": old_start + datetime.timedelta(seconds=10),
            },
        )

        exit_calls = [args for args in connection.executions if len(args) >= 4 and args[3] == "EXIT"]
        self.assertEqual(1, len(exit_calls))
        self.assertEqual(11, exit_calls[0][1])
        self.assertEqual(22, shared_map[(4242, 200.0)])
        self.assertNotIn((4242, 100.0), shared_map)


if __name__ == "__main__":
    unittest.main()
