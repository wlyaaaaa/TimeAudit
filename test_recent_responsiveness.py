import datetime as dt
import unittest
from unittest.mock import patch

import recent_responsiveness as recent
from timeaudit_diagnostic_summary import build_aggregate_sql, VALID_FRAME_SQL


class RecentResponsivenessTests(unittest.TestCase):
    def test_fixed_window_budget_and_result(self):
        until = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        result = {"hardware_sample_count": 0, "current_hung": None}
        with patch.object(recent, "query", return_value=[result]) as query:
            self.assertIs(recent.query_recent_responsiveness(until, timeout=0.5), result)
        query.assert_called_once_with(recent.SQL, until-dt.timedelta(minutes=5), until, timeout=0.5)
        with self.assertRaises(ValueError):
            recent.query_recent_responsiveness(until.replace(tzinfo=None))

    def test_partition_bounds_freshness_and_shared_frame_quality(self):
        self.assertIn(VALID_FRAME_SQL, build_aggregate_sql())
        self.assertIn(VALID_FRAME_SQL, recent.SQL)
        self.assertIn("b AS NOT MATERIALIZED", recent.SQL)
        self.assertIn("h.timestamp >= b.s AND h.timestamp < b.e", recent.SQL)
        self.assertIn("a.timestamp >= b.s AND a.timestamp < b.e", recent.SQL)
        self.assertIn("latest.sampled_at >= b.e - interval '15 seconds'", recent.SQL)
        self.assertIn("last_observed_at = latest.sampled_at", recent.SQL)
        self.assertIn("AND a.is_not_responding = 1", recent.SQL)
        self.assertIn("valid_frame AND frametime_ms >= 50", recent.SQL)
        self.assertNotIn("window_title", recent.SQL)
        self.assertNotIn("executable_path", recent.SQL)
        self.assertNotIn("command_line", recent.SQL)
