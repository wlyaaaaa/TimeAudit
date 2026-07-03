# -*- coding: utf-8 -*-
import unittest

from test_telemetry_health import evaluate_presentmon_process_status


class PresentMonGateHealthTest(unittest.TestCase):
    def test_idle_gated_presentmon_without_process_is_healthy(self):
        ok, detail = evaluate_presentmon_process_status(None)

        self.assertTrue(ok)
        self.assertIn("按需门控", detail)

    def test_running_presentmon_reports_pid(self):
        ok, detail = evaluate_presentmon_process_status(12345)

        self.assertTrue(ok)
        self.assertIn("PID=12345", detail)


if __name__ == "__main__":
    unittest.main()
