import datetime as dt
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import personal_activity_reader as reader

BASE = reader.parse_utc("2026-09-01T00:00:00Z")


def row(source, start, end, process="Example.exe", key=1):
    return {"source": source, "start": reader.format_utc(BASE+dt.timedelta(seconds=start)),
            "end": None if end is None else reader.format_utc(BASE+dt.timedelta(seconds=end)),
            "process_name": process, "id": {"id": key}}


class ActivityTests(unittest.TestCase):
    def test_access_failure_retains_owner_diagnostic_without_query_or_retry(self):
        decision = {"status": "blocked", "reason": "personal_access_adapter_unavailable",
                    "business_data_read": False, "diagnostic": {"stage": "broker", "code": "broker_timeout",
                    "elapsed_ms": 30001, "timeout_seconds": 30}}
        adapter = unittest.mock.Mock()
        adapter.check_access.return_value = decision
        with patch.object(reader.importlib.util, "module_from_spec", return_value=adapter), \
             patch.object(reader.importlib.util, "spec_from_file_location") as spec, \
             patch.object(reader, "query") as query, contextlib.redirect_stdout(io.StringIO()) as output:
            spec.return_value.loader.exec_module.return_value = None
            code = reader.main(["--after", "2026-09-01T00:00:00Z", "--until", "2026-09-02T00:00:00Z"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "personal_access_blocked:" + decision["reason"])
        self.assertEqual(result["diagnostic"], decision["diagnostic"])
        adapter.check_access.assert_called_once_with("factor")
        query.assert_not_called()

    def test_clipping_overlap_and_gap(self):
        rows = [row("foreground", -30, 20), row("foreground", 10, 40),
                row("foreground", 60, 140), row("ahk", 0, 50, "System_Idle"),
                row("ahk", 40, 70, "System_CollectionGap"), row("ahk", 70, 100)]
        result = reader.summarize(rows, BASE, BASE+dt.timedelta(seconds=100))
        fg = result["coverage"]["foreground"]
        self.assertEqual((fg["recorded_union_seconds"], fg["uncovered_seconds"],
                          fg["overlapping_row_seconds"]), (80, 20, 10))
        app = next(g for g in result["groups"] if g["source"] == "foreground")
        self.assertEqual(app["ahk_state_overlap_seconds"]["physical_idle"], 40)
        self.assertEqual(result["coverage"]["ahk"]["cross_state_overlap_seconds"], 10)
        self.assertEqual(result["coverage"]["ahk"]["explicit_collection_gap_seconds"], 30)

    def test_open_and_invalid_do_not_create_duration(self):
        result = reader.summarize([row("foreground", 0, None), row("foreground", 30, 10)],
                                  BASE, BASE+dt.timedelta(seconds=100))
        self.assertEqual(result["coverage"]["foreground"]["recorded_union_seconds"], 0)
        self.assertEqual(result["anomalies"]["open_foreground_rows_not_extrapolated"], 1)
        self.assertEqual(result["anomalies"]["foreground_nonpositive_intervals"], 1)

    def test_no_foreground_is_not_physical_idle_and_automation_does_not_subtract(self):
        result = reader.summarize([row("ahk", 0, 100, "Idle"), row("foreground", 0, 100)], BASE,
                                  BASE+dt.timedelta(seconds=100), [{"after_utc": reader.format_utc(BASE),
                                  "until_utc": reader.format_utc(BASE+dt.timedelta(seconds=30)),
                                  "source_ref": "test automation"}])
        self.assertEqual(result["groups"][0]["state"], "no_foreground_response")
        self.assertEqual(result["groups"][1]["observed_seconds"], 100)
        self.assertEqual(result["groups"][1]["known_automation_overlap_seconds"], 30)

    @patch.object(reader, "check_personal_access")
    def test_month_not_limited_to_diagnostic_168_hours(self, access):
        calls = []
        def query(sql, start, end):
            calls.append((sql, start, end))
            return [{}] if sql == reader.RANGE_SQL else []
        result = reader.read_activity(BASE-dt.timedelta(days=31), BASE, query_fn=query)
        self.assertEqual(len(result["chunks"]), 31)
        self.assertEqual(access.call_count, 32)
        self.assertTrue(all(end-start <= dt.timedelta(days=1) for sql,start,end in calls[1:]))

    @patch.object(reader, "check_personal_access", side_effect=RuntimeError("locked"))
    def test_denial_reads_no_database(self, access):
        with patch.object(reader, "query") as query:
            with self.assertRaisesRegex(RuntimeError, "locked"):
                reader.read_activity(BASE, BASE+dt.timedelta(days=1), query_fn=query)
            query.assert_not_called()

    @patch.object(reader, "check_personal_access", side_effect=[None, RuntimeError("locked")])
    def test_denial_before_delivery_discards_read_result(self, access):
        calls = []
        def query(sql, start, end):
            calls.append(sql)
            return [{}] if sql == reader.RANGE_SQL else []
        with self.assertRaisesRegex(RuntimeError, "locked"):
            reader.read_activity(BASE, BASE+dt.timedelta(hours=1), query_fn=query)
        self.assertEqual(calls, [reader.RANGE_SQL, reader.SQL])
        self.assertEqual(access.call_count, 2)

    @patch.object(reader, "check_personal_access")
    def test_load_splits_without_truncation(self, access):
        def query(sql, start, end):
            if sql == reader.RANGE_SQL:
                return [{}]
            if end-start > dt.timedelta(hours=12):
                raise RuntimeError("query_timeout")
            return []
        result = reader.read_activity(BASE, BASE+dt.timedelta(days=1), query_fn=query)
        self.assertEqual(len(result["chunks"]), 2)
        self.assertEqual(result["chunks"][0]["until_utc"], result["chunks"][1]["after_utc"])
        self.assertEqual(access.call_count, 4)

    @patch.object(reader, "check_personal_access")
    def test_week_summary_crosses_beijing_midnight_without_losing_days(self, access):
        after = reader.parse_utc("2026-09-01T15:00:00Z")
        until = after+dt.timedelta(days=7)
        crossing = {"source": "foreground", "start": "2026-09-01T15:30:00Z",
                    "end": "2026-09-01T16:30:00Z", "process_name": "Work.exe",
                    "id": {"id": 1}}
        idle = {**crossing, "source": "ahk", "process_name": "System_Idle", "id": {"id": 2}}
        lock = {**crossing, "source": "ahk", "process_name": "System_LockScreen", "id": {"id": 3}}
        calls = []
        def query(sql, start, end):
            calls.append((sql, start, end))
            return [{}] if sql == reader.RANGE_SQL else [crossing, idle, lock]
        result = reader.read_activity_summary(after, until, query_fn=query)
        self.assertEqual([x["date_beijing"] for x in result["days"]],
                         [f"2026-09-{day:02d}" for day in range(1, 9)])
        self.assertEqual(result["days"][0]["coverage"]["foreground"]["recorded_union_seconds"], 1800)
        self.assertEqual(result["days"][1]["coverage"]["foreground"]["recorded_union_seconds"], 1800)
        self.assertEqual(result["period"]["coverage"]["foreground"]["recorded_union_seconds"], 3600)
        self.assertEqual(result["period"]["states"]["physical_idle"]["recorded_union_seconds"], 3600)
        self.assertEqual(result["period"]["states"]["lock"]["recorded_union_seconds"], 3600)
        self.assertEqual(result["period"]["coverage"]["ahk"]["cross_state_overlap_seconds"], 3600)
        self.assertEqual(len(calls), 2)
        self.assertEqual(access.call_count, 3)

    @patch.object(reader, "check_personal_access")
    def test_summary_deduplicates_cross_batch_rows_and_reports_all_apps(self, access):
        after = reader.parse_utc("2026-09-01T00:00:00Z")
        until = after+dt.timedelta(days=8)
        crossing = {"source": "foreground", "start": "2026-09-07T23:00:00Z",
                    "end": "2026-09-08T01:00:00Z", "process_name": "Alpha.exe", "id": {"id": 1}}
        other = {"source": "foreground", "start": "2026-09-08T01:00:00Z",
                 "end": "2026-09-08T02:00:00Z", "process_name": "Beta.exe", "id": {"id": 2}}
        gap = {"source": "ahk", "start": "2026-09-08T00:00:00Z",
               "end": "2026-09-08T00:30:00Z", "process_name": "System_CollectionGap",
               "id": {"id": 3}}
        def query(sql, start, end):
            if sql == reader.RANGE_SQL:
                return [{}]
            return [crossing] if start == after else [crossing, other, gap]
        result = reader.read_activity_summary(after, until, top_n=1, query_fn=query)
        self.assertEqual(result["observation"]["native_row_count"], 3)
        self.assertEqual(result["period"]["coverage"]["foreground"]["row_count"], 2)
        self.assertEqual(result["period"]["coverage"]["foreground"]["recorded_union_seconds"], 10800)
        apps = result["period"]["applications"]["foreground"]
        self.assertEqual((apps["total_application_count"], apps["other_application_count"]), (2, 1))
        self.assertEqual(apps["top"][0]["process_name"], "Alpha.exe")
        self.assertEqual(result["period"]["coverage"]["ahk"]["explicit_collection_gap_seconds"], 1800)
        self.assertEqual(result["period"]["coverage"]["ahk"]["non_gap_recorded_seconds"], 0)
        self.assertEqual(access.call_count, 4)

    @patch.object(reader, "check_personal_access")
    def test_summary_preserves_overlap_open_and_nonpositive_anomalies(self, access):
        after = reader.parse_utc("2026-09-01T00:00:00Z")
        until = after+dt.timedelta(hours=1)
        rows = [row("foreground", -30, 20), row("foreground", 10, 40, key=2),
                row("foreground", 50, None, key=3), row("foreground", 60, 40, key=4),
                row("ahk", 0, 30, "Idle", key=5), row("ahk", 0, 30, "System_Idle", key=6)]
        def query(sql, start, end):
            return [{}] if sql == reader.RANGE_SQL else rows
        result = reader.read_activity_summary(after, until, query_fn=query)
        fg = result["period"]["coverage"]["foreground"]
        self.assertEqual((fg["recorded_union_seconds"], fg["overlapping_row_seconds"]), (40, 10))
        self.assertEqual(result["period"]["anomalies"],
                         {"open_foreground_rows_not_extrapolated": 1,
                          "foreground_nonpositive_intervals": 1})
        self.assertEqual(result["period"]["states"]["physical_idle"]["recorded_union_seconds"], 30)
        self.assertEqual(result["period"]["states"]["no_foreground_response"]["recorded_union_seconds"], 30)

    @patch.object(reader, "check_personal_access", side_effect=[None, None, RuntimeError("locked")])
    def test_summary_split_rechecks_before_retry_and_stops_on_lock(self, access):
        after = reader.parse_utc("2026-09-01T00:00:00Z")
        calls = []
        def query(sql, start, end):
            calls.append(sql)
            if sql == reader.SQL:
                raise RuntimeError("query_timeout")
            return [{}]
        with self.assertRaisesRegex(RuntimeError, "locked"):
            reader.read_activity_summary(after, after+dt.timedelta(days=1), query_fn=query)
        self.assertEqual(calls, [reader.RANGE_SQL, reader.SQL])
        self.assertEqual(access.call_count, 3)

    @patch.object(reader, "check_personal_access")
    def test_summary_successful_retry_preserves_whole_window(self, access):
        after = reader.parse_utc("2026-09-01T00:00:00Z")
        calls = []
        crossing = row("ahk", 11*3600, 13*3600, "System_Sleep")
        def query(sql, start, end):
            calls.append((sql, start, end))
            if sql == reader.RANGE_SQL:
                return [{}]
            if end-start > dt.timedelta(hours=12):
                raise RuntimeError("query_output_too_large")
            return [crossing]
        result = reader.read_activity_summary(after, after+dt.timedelta(days=1), query_fn=query)
        self.assertEqual(result["observation"]["sql_batch_count"], 2)
        self.assertEqual(result["observation"]["native_row_count"], 1)
        self.assertEqual(result["period"]["states"]["sleep"]["recorded_union_seconds"], 7200)
        self.assertEqual(access.call_count, 5)
        self.assertEqual([end-start for sql,start,end in calls if sql == reader.SQL],
                         [dt.timedelta(days=1), dt.timedelta(hours=12), dt.timedelta(hours=12)])

    @patch.object(reader, "check_personal_access", side_effect=[None, None, RuntimeError("locked")])
    def test_summary_denial_before_delivery_discards_aggregated_result(self, access):
        after = reader.parse_utc("2026-09-01T00:00:00Z")
        calls = []
        def query(sql, start, end):
            calls.append(sql)
            return [{}] if sql == reader.RANGE_SQL else [row("foreground", 0, 60)]
        with self.assertRaisesRegex(RuntimeError, "locked"):
            reader.read_activity_summary(after, after+dt.timedelta(hours=1), query_fn=query)
        self.assertEqual(calls, [reader.RANGE_SQL, reader.SQL])
        self.assertEqual(access.call_count, 3)


if __name__ == "__main__":
    unittest.main()
