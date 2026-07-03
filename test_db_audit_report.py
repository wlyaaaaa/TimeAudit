# -*- coding: utf-8 -*-
import pathlib
import unittest


class DbAuditReportTest(unittest.TestCase):
    def test_gpu_consistency_report_uses_gpu_row_timestamp(self):
        source = pathlib.Path(__file__).with_name("db_audit.py").read_text(encoding="utf-8")

        self.assertNotIn("{cs['timestamp']}: Σproc_gpu", source)
        self.assertIn("{gs['timestamp']}: Σproc_gpu", source)


if __name__ == "__main__":
    unittest.main()
