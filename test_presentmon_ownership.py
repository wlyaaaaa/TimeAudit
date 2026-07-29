# -*- coding: utf-8 -*-
import threading
import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import hardware_worker


ROOT = Path(__file__).resolve().parent


class FakePopen:
    def __init__(
        self,
        pid=41001,
        running=True,
        terminate_error=None,
        kill_error=None,
        wait_errors=None,
    ):
        self.pid = pid
        self._running = running
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_errors = list(wait_errors or [])
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error:
            raise self.terminate_error
        self._running = False

    def kill(self):
        self.kill_calls += 1
        if self.kill_error:
            raise self.kill_error
        self._running = False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_errors:
            raise self.wait_errors.pop(0)
        self._running = False
        return 0


class PresentMonOwnershipTest(unittest.TestCase):
    def make_worker(self, process):
        worker = hardware_worker.HardwareTelemetryWorker.__new__(
            hardware_worker.HardwareTelemetryWorker
        )
        worker.presentmon_process = process
        worker._presentmon_process_lock = threading.Lock()
        worker._presentmon_process_condition = threading.Condition(
            worker._presentmon_process_lock
        )
        worker._presentmon_claims_inflight = 0
        worker.stop_event = threading.Event()
        return worker

    def test_command_uses_private_named_session(self):
        command = hardware_worker.HardwareTelemetryWorker._presentmon_command(
            r"E:\Tools\PresentMonConsole.exe"
        )

        self.assertEqual(
            [
                r"E:\Tools\PresentMonConsole.exe",
                "--session_name",
                "TimeAuditPresentMon",
                "--output_stdout",
                "--stop_existing_session",
                "--no_console_stats",
            ],
            command,
        )

    def test_stop_owned_process_only_walks_owned_process_tree(self):
        owned = FakePopen()
        child = Mock()
        child.is_running.return_value = True
        root = Mock()
        root.children.return_value = [child]
        worker = self.make_worker(owned)

        with (
            patch.object(hardware_worker.psutil, "Process", return_value=root) as process_ctor,
            patch.object(
                hardware_worker.psutil,
                "process_iter",
                side_effect=AssertionError("must not scan unrelated PresentMon processes"),
            ),
            patch.object(
                hardware_worker.psutil,
                "wait_procs",
                return_value=([child], []),
            ),
        ):
            stopped = worker._stop_owned_presentmon_process(owned)

        self.assertTrue(stopped)
        process_ctor.assert_called_once_with(owned.pid)
        root.children.assert_called_once_with(recursive=True)
        child.terminate.assert_called_once_with()
        self.assertEqual(1, owned.terminate_calls)
        self.assertIsNone(worker.presentmon_process)

    def test_stale_cleanup_cannot_stop_a_newer_owned_process(self):
        old_process = FakePopen(pid=41001)
        new_process = FakePopen(pid=41002)
        worker = self.make_worker(new_process)

        with patch.object(
            hardware_worker.psutil,
            "Process",
            side_effect=AssertionError("must not inspect an unowned stale process"),
        ):
            stopped = worker._stop_owned_presentmon_process(old_process)

        self.assertFalse(stopped)
        self.assertIs(new_process, worker.presentmon_process)
        self.assertEqual(0, old_process.terminate_calls)
        self.assertEqual(0, new_process.terminate_calls)

    def test_stop_and_claim_are_atomic_and_rejected_process_is_cleaned(self):
        process = FakePopen()
        worker = self.make_worker(None)
        worker._presentmon_process_lock.acquire()
        result = []

        thread = threading.Thread(
            target=lambda: result.append(worker._claim_presentmon_process(process))
        )
        root = Mock()
        root.children.return_value = []
        with patch.object(hardware_worker.psutil, "Process", return_value=root):
            thread.start()
            worker.stop_event.set()
            worker._presentmon_process_lock.release()
            thread.join(timeout=1.0)

        self.assertEqual([False], result)
        self.assertEqual(1, process.terminate_calls)
        self.assertIsNone(worker.presentmon_process)

    def test_cleanup_failure_returns_false_and_retains_retry_handle(self):
        owned = FakePopen(
            terminate_error=OSError("terminate denied"),
            kill_error=OSError("kill denied"),
            wait_errors=[OSError("wait denied")],
        )
        root = Mock()
        root.children.return_value = []
        worker = self.make_worker(owned)

        with (
            patch.object(hardware_worker.psutil, "Process", return_value=root),
            self.assertLogs("PresentMon_Debugger", level="WARNING") as logs,
        ):
            stopped = worker._stop_owned_presentmon_process(owned, timeout=0.01)

        self.assertFalse(stopped)
        self.assertIs(owned, worker.presentmon_process)
        self.assertTrue(any("未确认退出" in line for line in logs.output))

    def test_kill_fallback_can_confirm_exit_and_clear_ownership(self):
        owned = FakePopen(
            terminate_error=OSError("terminate denied"),
            wait_errors=[
                subprocess.TimeoutExpired("PresentMon", 0.01),
            ],
        )
        root = Mock()
        root.children.return_value = []
        worker = self.make_worker(owned)

        with patch.object(hardware_worker.psutil, "Process", return_value=root):
            stopped = worker._stop_owned_presentmon_process(owned, timeout=0.01)

        self.assertTrue(stopped)
        self.assertEqual(1, owned.kill_calls)
        self.assertIsNone(worker.presentmon_process)

    def test_child_tree_enumeration_failure_is_retryable(self):
        owned = FakePopen()
        worker = self.make_worker(owned)

        with (
            patch.object(
                hardware_worker.psutil,
                "Process",
                side_effect=OSError("process tree unavailable"),
            ),
            self.assertLogs("PresentMon_Debugger", level="WARNING"),
        ):
            stopped = worker._stop_owned_presentmon_process(owned)

        self.assertFalse(stopped)
        self.assertIs(owned, worker.presentmon_process)

    def test_terminate_sets_stop_joins_listener_then_retries_cleanup(self):
        owned = FakePopen()
        worker = self.make_worker(owned)
        worker.dpc_checker = Mock()
        worker.presentmon_thread = Mock()
        worker.pdh_thread = None
        worker.nvml_initialized = False
        order = []
        worker.presentmon_thread.join.side_effect = lambda timeout: order.append(
            ("join", timeout, worker.stop_event.is_set())
        )
        worker._stop_owned_presentmon_process = Mock(
            side_effect=lambda **kwargs: order.append(("cleanup", kwargs["timeout"]))
            or True
        )

        worker.terminate()

        self.assertTrue(worker.stop_event.is_set())
        self.assertEqual(
            [("join", 2.0, True), ("cleanup", 1.0)],
            order,
        )

    def test_terminate_waits_for_rejected_claim_failure_then_retries_owner(self):
        process = FakePopen()
        worker = self.make_worker(None)
        worker.stop_event.set()
        worker.dpc_checker = Mock()
        worker.presentmon_thread = Mock()
        worker.pdh_thread = None
        worker.nvml_initialized = False
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        listener_joined = threading.Event()
        cleanup_calls = []
        worker.presentmon_thread.join.side_effect = (
            lambda timeout: listener_joined.set()
        )

        def cleanup_side_effect(target, timeout):
            cleanup_calls.append((target, timeout))
            if len(cleanup_calls) == 1:
                cleanup_entered.set()
                release_cleanup.wait()
                return False
            target._running = False
            return True

        with patch.object(
            worker,
            "_terminate_presentmon_process_tree",
            side_effect=cleanup_side_effect,
        ):
            claim_result = []
            claim_thread = threading.Thread(
                target=lambda: claim_result.append(
                    worker._claim_presentmon_process(process)
                )
            )
            claim_thread.start()
            self.assertTrue(cleanup_entered.wait(timeout=1.0))

            terminate_thread = threading.Thread(target=worker.terminate)
            terminate_thread.start()
            self.assertTrue(listener_joined.wait(timeout=1.0))
            worker.presentmon_thread.join.assert_called_once_with(timeout=2.0)
            self.assertTrue(
                terminate_thread.is_alive(),
                "terminate must wait for the rejected claim cleanup transaction",
            )

            release_cleanup.set()
            claim_thread.join(timeout=1.0)
            terminate_thread.join(timeout=1.0)

        self.assertFalse(terminate_thread.is_alive())
        self.assertEqual([False], claim_result)
        self.assertEqual(2, len(cleanup_calls))
        self.assertIsNone(worker.presentmon_process)
        self.assertIsNotNone(process.poll())

    def test_source_has_no_global_presentmon_name_sweep_and_keeps_gate_policy(self):
        source = (ROOT / "hardware_worker.py").read_text(encoding="utf-8")

        self.assertNotIn("psutil.process_iter(['name'])", source)
        self.assertIn("if self.stop_event.wait(0.5):", source)
        self.assertIn("self.presentmon_thread = t", source)
        self.assertEqual(18.0, hardware_worker.HardwareTelemetryWorker.RENDER_GPU_THRESHOLD)
        self.assertEqual(75.0, hardware_worker.HardwareTelemetryWorker.RENDER_HYSTERESIS_SEC)


if __name__ == "__main__":
    unittest.main()
