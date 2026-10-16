# -*- coding: utf-8 -*-
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    async def test_duplicate_and_snapshot_missing_handles_close_once(self):
        for query_fails in (False, True):
            with self.subTest(query_fails=query_fails):
                worker = ProcessLifecycleWorker({})
                key = (4242, 100.0)
                retained, duplicate = 0x100000001, 0x100000002
                closed, queried = [], []
                ticks = 0

                def sleep(_seconds):
                    nonlocal ticks
                    ticks += 1
                    if ticks == 3:
                        worker.is_running = False

                def exit_code(handle, code):
                    queried.append(handle)
                    if query_fails:
                        raise OSError("synthetic exit query failure")
                    code._obj.value = 7
                    return True

                native = SimpleNamespace(
                    OpenProcess=Mock(side_effect=[retained, duplicate]),
                    CloseHandle=Mock(side_effect=closed.append),
                    GetExitCodeProcess=Mock(side_effect=exit_code),
                )
                snapshots = Mock(return_value=[])
                loop = SimpleNamespace(call_soon_threadsafe=lambda callback, event: callback(event))
                with (
                    patch("lifecycle_worker.kernel32", native),
                    patch.dict("sys.modules", {"activity_worker": SimpleNamespace(fetch_system_processes=snapshots)}),
                    patch("lifecycle_worker.time.sleep", side_effect=sleep),
                    patch("lifecycle_worker.psutil.process_iter", side_effect=AssertionError("no real sampling")) as fallback,
                ):
                    worker._track_pid_handle(key)
                    worker._track_pid_handle(key)
                    self.assertEqual([duplicate], closed)
                    self.assertEqual({key: retained}, worker.pid_handles)
                    worker.pid_start_time[key] = key[1]
                    worker.is_running = True
                    worker._differential_scanner_loop(loop)
                    self.assertEqual([duplicate, retained], closed)
                    self.assertEqual([retained], queried)
                    self.assertEqual({}, worker.pid_handles)
                    self.assertEqual({}, worker.pid_start_time)
                    self.assertEqual(3, snapshots.call_count)
                    fallback.assert_not_called()
                    if not query_fails:
                        event = worker.event_queue.get_nowait()
                        self.assertEqual(key, event["instance_key"])
                        self.assertEqual("0x00000007", event["exit_code"])

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
