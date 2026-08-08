# -*- coding: utf-8 -*-
import pathlib
import unittest


class DbAuditReportTest(unittest.TestCase):
    def test_gpu_consistency_report_uses_gpu_row_timestamp(self):
        source = pathlib.Path(__file__).with_name("db_audit.py").read_text(encoding="utf-8")

        self.assertNotIn("{cs['timestamp']}: Σproc_gpu", source)
        self.assertIn("{gs['timestamp']}: Σproc_gpu", source)

    def test_timestamp_overlap_is_measured_against_activity_samples(self):
        source = pathlib.Path(__file__).with_name("db_audit.py").read_text(encoding="utf-8")

        self.assertIn(
            "overlap['overlap_count'] / overlap['act_ts_count'] * 100",
            source,
        )
        self.assertNotIn(
            "overlap['overlap_count'] / overlap['hw_ts_count'] * 100",
            source,
        )
        self.assertIn("if overlap['act_ts_count'] > 0", source)
        self.assertIn(
            "overlap['overlap_count'] == overlap['act_ts_count']",
            source,
        )


if __name__ == "__main__":
    unittest.main()
