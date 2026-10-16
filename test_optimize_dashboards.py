import unittest

from optimize_dashboards import optimize_sql


MONTH = "WHERE timestamp >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai')"
CAP = " AND timestamp <= now()"


class OptimizeSqlTests(unittest.TestCase):
    def test_existing_upper_bounds_are_preserved(self):
        for upper in (CAP, "\nAND\t timestamp\n<=\tnow ( \n)", CAP + CAP):
            sql = "SELECT timestamp FROM samples " + MONTH + upper
            with self.subTest(upper=upper):
                self.assertEqual(optimize_sql(sql), sql)
                self.assertEqual(optimize_sql(optimize_sql(sql)), sql)

    def test_missing_and_mixed_upper_bounds_are_added_once(self):
        missing = "SELECT timestamp FROM samples " + MONTH
        bounded = missing + CAP
        for sql, expected in (
            (missing, bounded),
            (bounded + " UNION ALL " + missing, bounded + " UNION ALL " + bounded),
        ):
            with self.subTest(sql=sql):
                once = optimize_sql(sql)
                self.assertEqual(once, expected)
                self.assertEqual(optimize_sql(once), expected)
