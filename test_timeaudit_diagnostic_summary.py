import datetime as dt
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import timeaudit_diagnostic_summary as summary


UTC = dt.timezone.utc
AFTER = dt.datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
UNTIL = dt.datetime(2026, 8, 29, 1, 0, tzinfo=UTC)


def aggregate_fixture():
    return {
        "hardware_sample_count": 3600,
        "first_sample_utc": "2026-08-29T00:00:01+00:00",
        "last_sample_utc": "2026-08-29T00:59:59+00:00",
        "max_internal_gap_seconds": 2,
        "cpu_usage_avg_pct": 25,
        "cpu_usage_max_pct": 80,
        "cpu_temp_avg_c": 55,
        "cpu_temp_max_c": 96,
        "cpu_power_avg_w": 70,
        "cpu_power_max_w": 180,
        "gpu_usage_avg_pct": 20,
        "gpu_usage_max_pct": 99,
        "gpu_temp_avg_c": 48,
        "gpu_temp_max_c": 88,
        "gpu_hotspot_max_c": 104,
        "gpu_power_avg_w": 60,
        "gpu_power_max_w": 400,
        "ram_usage_avg_pct": 50,
        "ram_usage_max_pct": 96,
        "disk_latency_avg_ms": 2,
        "disk_latency_p95_ms": 20,
        "disk_latency_max_ms": 1500,
        "network_ping_avg_ms": 10,
        "network_ping_max_ms": 100,
        "packet_loss_samples": 2,
        "fps_positive_sample_count": 130,
        "fps_sample_count": 120,
        "fps_avg": 100,
        "fps_min": 30,
        "fps_one_percent_low_avg": 55,
        "frametime_p95_ms": 18,
        "frametime_max_ms": 70,
        "frametime_spike_samples": 3,
        "cpu_thermal_samples": 1,
        "gpu_thermal_samples": 0,
        "memory_pressure_samples": 2,
        "storage_latency_samples": 1,
        "telemetry_out_of_bounds_samples": 0,
        "state_event_count": 25,
        "active_seconds": 1800,
        "idle_seconds": 600,
        "display_off_seconds": 300,
        "lock_seconds": 60,
        "sleep_seconds": 600,
        "summed_state_seconds": 3360,
        "recorded_coverage_seconds": 3300,
        "requested_window_seconds": 3600,
        "uncovered_seconds": 300,
        "cross_state_overlap_seconds": 60,
    }


class TimeAuditDiagnosticSummaryTests(unittest.TestCase):
    def test_summary_is_bounded_aggregate_with_explicit_limits(self):
        result = summary.build_summary(
            aggregate_fixture(),
            after_utc=AFTER,
            until_utc=UNTIL,
            generated_at_utc=UNTIL,
        )
        self.assertEqual(result["schema"], summary.SCHEMA)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["window"]["coverage_status"], "fresh")
        self.assertEqual(result["window"]["max_gap_seconds"], 2.0)
        self.assertEqual(result["game_performance"]["status"], "observed")
        self.assertEqual(
            result["game_performance"]["quality"],
            "mixed_valid_and_rejected",
        )
        self.assertEqual(result["game_performance"]["rejected_positive_samples"], 10)
        self.assertEqual(result["activity_state"]["quality"], "overlap_detected")
        self.assertEqual(
            {item["signal_id"] for item in result["signals"]},
            {
                "cpu_thermal_occurrences",
                "memory_pressure_occurrences",
                "storage_latency_occurrences",
                "packet_loss_occurrences",
                "frametime_spike_occurrences",
            },
        )
        self.assertEqual(result["interpretation"]["causality"], "correlation_only")
        self.assertTrue(all(value is False for value in result["privacy"].values()))

    def test_empty_hardware_and_no_frames_are_not_reported_as_healthy_game_data(self):
        raw = aggregate_fixture()
        raw.update(
            hardware_sample_count=0,
            first_sample_utc=None,
            last_sample_utc=None,
            fps_sample_count=0,
            fps_positive_sample_count=0,
            fps_avg=None,
            fps_min=None,
            fps_one_percent_low_avg=None,
            frametime_p95_ms=None,
            frametime_max_ms=None,
            frametime_spike_samples=0,
        )
        result = summary.build_summary(raw, after_utc=AFTER, until_utc=UNTIL)
        self.assertEqual(result["window"]["coverage_status"], "empty")
        self.assertEqual(result["game_performance"]["status"], "no_game_frames")
        self.assertIsNone(result["game_performance"]["fps_average"])
        self.assertIsNone(result["window"]["max_gap_seconds"])

    def test_max_gap_includes_window_boundaries(self):
        raw = aggregate_fixture()
        raw.update(
            first_sample_utc="2026-08-29T00:30:00Z",
            last_sample_utc="2026-08-29T00:59:59Z",
            max_internal_gap_seconds=1,
        )
        result = summary.build_summary(raw, after_utc=AFTER, until_utc=UNTIL)
        self.assertEqual(result["window"]["coverage_status"], "fresh")
        self.assertEqual(result["window"]["window_start_gap_seconds"], 1800.0)
        self.assertEqual(result["window"]["window_end_gap_seconds"], 1.0)
        self.assertEqual(result["window"]["max_gap_seconds"], 1800.0)

    def test_sql_is_one_bounded_aggregate_and_excludes_private_payload_columns(self):
        sql = summary.build_aggregate_sql()
        self.assertIn("fact_system_hardware", sql)
        self.assertIn("app_usage_logs", sql)
        self.assertIn("percentile_cont", sql)
        self.assertIn(":'after_utc'::timestamptz", sql)
        self.assertIn("current_fps BETWEEN 0.5 AND 1000", sql)
        self.assertIn("frametime_ms BETWEEN 0.5 AND 2000", sql)
        self.assertIn("* 0.35", sql)
        self.assertNotIn("SELECT h.*\n  FROM public.fact_system_hardware", sql)
        self.assertNotIn("window_title", sql)
        self.assertNotIn("proc_remote_ip_port", sql)
        self.assertNotIn("executable_path", sql)
        self.assertNotIn("SELECT *", sql.upper())

    def test_query_uses_fixed_container_without_password_or_shell(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(aggregate_fixture()), stderr=""
        )
        with mock.patch("subprocess.run", return_value=completed) as run:
            result = summary.query_aggregate(AFTER, UNTIL, docker_executable="docker.exe")
        self.assertEqual(result["hardware_sample_count"], 3600)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["docker.exe", "exec", "-i", "audit-postgres"])
        self.assertNotIn("password", " ".join(command).lower())
        self.assertFalse(run.call_args.kwargs["shell"] if "shell" in run.call_args.kwargs else False)

    def test_query_timeout_and_invalid_output_fail_closed(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired([], 10)):
            with self.assertRaisesRegex(RuntimeError, "query_timeout"):
                summary.query_aggregate(AFTER, UNTIL, docker_executable="docker.exe")
        bad = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")
        with mock.patch("subprocess.run", return_value=bad):
            with self.assertRaisesRegex(RuntimeError, "query_output_invalid"):
                summary.query_aggregate(AFTER, UNTIL, docker_executable="docker.exe")

    def test_cli_unavailable_is_structured_and_payload_free(self):
        output = io.StringIO()
        with mock.patch.object(summary, "query_aggregate", side_effect=RuntimeError("query_failed")):
            with redirect_stdout(output):
                exit_code = summary.main(
                    [
                        "--after-utc",
                        "2026-08-29T00:00:00Z",
                        "--until-utc",
                        "2026-08-29T01:00:00Z",
                    ]
                )
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "query_failed")
        self.assertTrue(all(value is False for value in result["privacy"].values()))

    def test_window_validation_rejects_more_than_seven_days(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = summary.main(
                [
                    "--after-utc",
                    "2026-08-01T00:00:00Z",
                    "--until-utc",
                    "2026-08-10T00:00:00Z",
                ]
            )
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["reason"], "window_too_large")

    def test_relative_hours_is_a_quick_bounded_entry(self):
        output = io.StringIO()
        with mock.patch.object(
            summary, "query_aggregate", return_value=aggregate_fixture()
        ) as query:
            with redirect_stdout(output):
                exit_code = summary.main(["--hours", "3"])
        result = json.loads(output.getvalue())
        requested = summary.parse_utc(result["window"]["until_inclusive_utc"]) - summary.parse_utc(
            result["window"]["after_exclusive_utc"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(requested, dt.timedelta(hours=3))
        self.assertEqual(query.call_count, 1)

    def test_relative_hours_cannot_mix_with_exact_bounds(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = summary.main(
                ["--hours", "3", "--until-utc", "2026-08-29T01:00:00Z"]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "window_argument_conflict")

    def test_invalid_cli_value_returns_only_contract_json(self):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = summary.main(["--hours", "not-a-number"])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["reason"], "argument_invalid")
        self.assertEqual(errors.getvalue(), "")

    def test_non_finite_database_value_is_not_emitted_as_invalid_json(self):
        raw = aggregate_fixture()
        raw["cpu_temp_avg_c"] = float("nan")
        result = summary.build_summary(raw, after_utc=AFTER, until_utc=UNTIL)
        self.assertIsNone(result["hardware"]["cpu"]["package_temp_c"]["average"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
