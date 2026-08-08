# -*- coding: utf-8 -*-
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from activity_worker import ProcessActivityWorker


class ActivityCollectionStateLockTest(unittest.TestCase):
    def make_worker(self):
        worker = ProcessActivityWorker.__new__(ProcessActivityWorker)
        worker.collection_state_lock = threading.Lock()
        worker.cpu_time_cache = {"stale": object()}
        worker.io_delta_cache = {"stale": object()}
        worker.last_net_bytes_sent = 0
        worker.last_net_bytes_recv = 0
        worker.last_net_time = 0.0
        return worker

    def test_resume_reset_waits_for_inflight_process_collection(self):
        worker = self.make_worker()
        collection_entered = threading.Event()
        release_collection = threading.Event()
        reset_entered = threading.Event()
        reset_finished = threading.Event()
        thread_errors = []

        def slow_unlocked_collection():
            collection_entered.set()
            if not release_collection.wait(timeout=2.0):
                raise TimeoutError("test did not release process collection")
            return ["snapshot"]

        worker._collect_active_processes_unlocked = slow_unlocked_collection

        def run_collection():
            try:
                worker.collect_active_processes()
            except Exception as exc:  # pragma: no cover - assertion reports details
                thread_errors.append(exc)

        def run_reset():
            try:
                reset_entered.set()
                worker.reset_on_resume()
                reset_finished.set()
            except Exception as exc:  # pragma: no cover - assertion reports details
                thread_errors.append(exc)

        collect_thread = threading.Thread(target=run_collection, daemon=True)
        reset_thread = threading.Thread(target=run_reset, daemon=True)

        with mock.patch(
            "activity_worker.psutil.net_io_counters",
            return_value=SimpleNamespace(bytes_sent=10, bytes_recv=20),
        ):
            collect_thread.start()
            self.assertTrue(collection_entered.wait(timeout=1.0))
            self.assertFalse(worker.is_collection_state_idle())

            reset_thread.start()
            self.assertTrue(reset_entered.wait(timeout=1.0))
            self.assertFalse(
                reset_finished.wait(timeout=0.05),
                "resume reset must not mutate rate baselines mid-collection",
            )

            release_collection.set()
            collect_thread.join(timeout=1.0)
            reset_thread.join(timeout=1.0)

        self.assertFalse(collect_thread.is_alive())
        self.assertFalse(reset_thread.is_alive())
        self.assertEqual([], thread_errors)
        self.assertTrue(reset_finished.is_set())
        self.assertTrue(worker.is_collection_state_idle())
        self.assertEqual({}, worker.cpu_time_cache)
        self.assertEqual({}, worker.io_delta_cache)


if __name__ == "__main__":
    unittest.main()
