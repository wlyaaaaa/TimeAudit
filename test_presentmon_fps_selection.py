# -*- coding: utf-8 -*-
import ast
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

import hardware_worker


class PresentMonFpsSelectionTest(unittest.TestCase):
    def make_worker(self):
        worker = hardware_worker.HardwareTelemetryWorker.__new__(
            hardware_worker.HardwareTelemetryWorker
        )
        worker.app_windows = defaultdict(list)
        worker.app_last_update = {}
        worker.lock = threading.Lock()
        worker.active_foreground_app = ""
        worker.active_foreground_pid = None
        return worker

    def test_samples_are_owned_by_exact_pid_and_application(self):
        worker = self.make_worker()
        header_map = {
            "application": 0,
            "processid": 1,
            "msbetweenpresents": 2,
        }

        self.assertTrue(
            worker._record_presentmon_sample(
                ["game.exe", "1001", "3.0"],
                header_map,
                app_col_name="application",
                ft_idx=2,
                observed_monotonic=100.0,
            )
        )
        self.assertTrue(
            worker._record_presentmon_sample(
                ["game.exe", "2002", "4.0"],
                header_map,
                app_col_name="application",
                ft_idx=2,
                observed_monotonic=100.0,
            )
        )

        self.assertEqual([3.0], worker.app_windows[(1001, "game")])
        self.assertEqual([4.0], worker.app_windows[(2002, "game")])

    def test_non_positive_and_obviously_invalid_frame_times_are_dropped(self):
        worker = self.make_worker()
        header_map = {
            "application": 0,
            "processid": 1,
            "msbetweenpresents": 2,
        }

        for raw_frame_time in ("0", "-1", "nan", "inf", "100000"):
            self.assertFalse(
                worker._record_presentmon_sample(
                    ["game.exe", "1001", raw_frame_time],
                    header_map,
                    app_col_name="application",
                    ft_idx=2,
                    observed_monotonic=100.0,
                )
            )

        self.assertEqual([], worker.app_windows[(1001, "game")])

    def test_monotonic_record_gap_clears_the_previous_window(self):
        worker = self.make_worker()
        header_map = {
            "application": 0,
            "processid": 1,
            "msbetweenpresents": 2,
        }

        for frame_time, observed_monotonic in (("10.0", 100.0), ("20.0", 106.0)):
            self.assertTrue(
                worker._record_presentmon_sample(
                    ["game.exe", "1001", frame_time],
                    header_map,
                    app_col_name="application",
                    ft_idx=2,
                    observed_monotonic=observed_monotonic,
                )
            )

        self.assertEqual([20.0], worker.app_windows[(1001, "game")])

    def test_missing_fresh_foreground_pid_does_not_fall_back_to_other_app(self):
        worker = self.make_worker()
        now = 200.0
        worker.app_windows[(1001, "game")] = [3.57]
        worker.app_last_update[(1001, "game")] = now - 6.0
        worker.app_windows[(2002, "other")] = [3.03]
        worker.app_last_update[(2002, "other")] = now - 1.0

        selected = worker._select_presentmon_window(
            "game.exe", foreground_pid=1001, now_monotonic=now
        )

        self.assertIsNone(selected)

    def test_fresh_foreground_pid_selects_only_its_window(self):
        worker = self.make_worker()
        now = 200.0
        worker.app_windows[(1001, "game")] = [3.57]
        worker.app_last_update[(1001, "game")] = now - 1.0
        worker.app_windows[(2002, "game")] = [3.03]
        worker.app_last_update[(2002, "game")] = now - 1.0

        selected = worker._select_presentmon_window(
            "game.exe", foreground_pid=1001, now_monotonic=now
        )

        self.assertEqual([3.57], selected)

    def test_wall_clock_rollback_cannot_keep_a_stale_window_fresh(self):
        worker = self.make_worker()
        header_map = {
            "application": 0,
            "processid": 1,
            "msbetweenpresents": 2,
        }

        with (
            mock.patch(
                "hardware_worker.time.monotonic",
                side_effect=[100.0, 106.0],
            ),
            mock.patch("hardware_worker.time.time", side_effect=[200.0, 100.0]),
        ):
            self.assertTrue(
                worker._record_presentmon_sample(
                    ["game.exe", "1001", "10.0"],
                    header_map,
                    app_col_name="application",
                    ft_idx=2,
                )
            )
            selected = worker._select_presentmon_window(
                "game.exe",
                foreground_pid=1001,
            )

        self.assertIsNone(selected)


class PresentMonForegroundWiringTest(unittest.TestCase):
    def test_main_passes_tracker_pid_with_foreground_application(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "collect_hardware_snapshot"
        ]

        self.assertEqual(1, len(calls))
        self.assertEqual(2, len(calls[0].args))
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual("hardware_pid", calls[0].args[1].id)

        identity_assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Tuple) for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_sample_current_foreground_identity"
        ]
        self.assertEqual(1, len(identity_assignments))
        target_names = {
            element.id
            for target in identity_assignments[0].targets
            for element in target.elts
            if isinstance(element, ast.Name)
        }
        self.assertIn("hardware_pid", target_names)
        self.assertLess(identity_assignments[0].lineno, calls[0].lineno)


if __name__ == "__main__":
    unittest.main()
