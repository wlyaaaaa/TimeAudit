import datetime as dt
import unittest

import pcconfig_anomaly_digest as digest


UTC = dt.timezone.utc
AFTER = dt.datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
UNTIL = dt.datetime(2026, 7, 30, 1, 0, tzinfo=UTC)


def base_aggregate():
    value = {
        "sample_count": 1200,
        "first_sample_utc": "2026-07-30T00:00:03+00:00",
        "last_sample_utc": "2026-07-30T00:59:57+00:00",
    }
    for rule in digest.RULES:
        value[f"{rule.anomaly_id}_count"] = 0
        value[f"{rule.anomaly_id}_first"] = None
        value[f"{rule.anomaly_id}_last"] = None
    return value


class PcConfigAnomalyDigestTests(unittest.TestCase):
    def test_fresh_window_has_no_anomalies(self):
        result = digest.build_digest(
            base_aggregate(),
            after_utc=AFTER,
            until_utc=UNTIL,
            generated_at_utc=UNTIL,
        )
        self.assertEqual(result["schema"], digest.SCHEMA)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["window"]["coverage_status"], "fresh")
        self.assertEqual(result["summary"]["anomaly_count"], 0)
        self.assertFalse(result["summary"]["projection_recheck_recommended"])
        self.assertEqual(result["cursor"]["next_after_utc"], "2026-07-30T01:00:00.000000Z")
        self.assertFalse(result["privacy"]["raw_samples_included"])
        self.assertFalse(result["privacy"]["process_activity_included"])

    def test_threshold_requires_minimum_samples_and_returns_no_values(self):
        aggregate = base_aggregate()
        aggregate["cpu_thermal_pressure_count"] = 10
        aggregate["cpu_thermal_pressure_first"] = "2026-07-30T00:10:00Z"
        aggregate["cpu_thermal_pressure_last"] = "2026-07-30T00:11:00Z"
        aggregate["gpu_thermal_pressure_count"] = 9
        aggregate["gpu_thermal_pressure_first"] = "2026-07-30T00:20:00Z"
        aggregate["gpu_thermal_pressure_last"] = "2026-07-30T00:21:00Z"
        result = digest.build_digest(
            aggregate,
            after_utc=AFTER,
            until_utc=UNTIL,
            generated_at_utc=UNTIL,
        )
        self.assertEqual(result["summary"]["critical_count"], 1)
        self.assertTrue(result["summary"]["projection_recheck_recommended"])
        self.assertEqual(
            [item["anomaly_id"] for item in result["anomalies"]],
            ["cpu_thermal_pressure"],
        )
        self.assertEqual(
            set(result["anomalies"][0]),
            {
                "anomaly_id",
                "severity",
                "sample_count",
                "first_seen_utc",
                "last_seen_utc",
                "threshold_ref",
                "projection_recheck_recommended",
            },
        )

    def test_stale_and_empty_windows_report_gap_without_projection_recheck(self):
        stale = base_aggregate()
        stale["last_sample_utc"] = "2026-07-30T00:50:00Z"
        stale_result = digest.build_digest(
            stale,
            after_utc=AFTER,
            until_utc=UNTIL,
            generated_at_utc=UNTIL,
        )
        self.assertEqual(stale_result["window"]["coverage_status"], "stale")
        self.assertEqual(stale_result["anomalies"][0]["anomaly_id"], "telemetry_gap")
        self.assertFalse(
            stale_result["summary"]["projection_recheck_recommended"]
        )

        empty = base_aggregate()
        empty.update(
            sample_count=0,
            first_sample_utc=None,
            last_sample_utc=None,
        )
        empty_result = digest.build_digest(
            empty,
            after_utc=AFTER,
            until_utc=UNTIL,
            generated_at_utc=UNTIL,
        )
        self.assertEqual(empty_result["window"]["coverage_status"], "empty")
        self.assertEqual(empty_result["summary"]["anomaly_count"], 1)

    def test_sql_is_aggregate_only_and_never_queries_activity_payloads(self):
        sql = digest.build_aggregate_sql()
        self.assertIn("fact_system_hardware", sql)
        self.assertIn("COUNT(*) FILTER", sql)
        self.assertIn(":'after_utc'::timestamptz", sql)
        self.assertNotIn("fact_process", sql)
        self.assertNotIn("window_title", sql)
        self.assertNotIn("SELECT *", sql.upper())

    def test_unavailable_result_is_payload_free(self):
        result = digest.unavailable_digest("query_failed")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["privacy"]["database_credentials_included"])
        self.assertNotIn("dsn", str(result).lower())


if __name__ == "__main__":
    unittest.main()
