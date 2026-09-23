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


if __name__ == "__main__":
    unittest.main()
