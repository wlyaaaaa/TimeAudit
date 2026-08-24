# -*- coding: utf-8 -*-
import unittest

from test_telemetry_health import (
    evaluate_presentmon_capture_status,
    evaluate_presentmon_process_status,
)


class PresentMonGateHealthTest(unittest.TestCase):
    def test_idle_gated_presentmon_without_process_is_healthy(self):
        ok, detail = evaluate_presentmon_process_status(None)

        self.assertTrue(ok)
        self.assertIn("按需门控", detail)

    def test_running_presentmon_reports_pid(self):
        ok, detail = evaluate_presentmon_process_status(12345)

        self.assertTrue(ok)
        self.assertIn("PID=12345", detail)

    def test_recent_positive_fps_is_live_capture_evidence(self):
        ok, detail = evaluate_presentmon_capture_status(12, 1.0)

        self.assertTrue(ok)
        self.assertIn("正帧率样本", detail)

    def test_fresh_zero_fps_is_idle_not_a_failure(self):
        ok, detail = evaluate_presentmon_capture_status(0, 1.0)

        self.assertTrue(ok)
        self.assertIn("IDLE", detail)
        self.assertIn("未执行正帧率 canary", detail)

    def test_zero_fps_with_stale_channel_is_a_failure(self):
        ok, detail = evaluate_presentmon_capture_status(0, 90.0)

        self.assertFalse(ok)
        self.assertIn("不新鲜", detail)


if __name__ == "__main__":
    unittest.main()
