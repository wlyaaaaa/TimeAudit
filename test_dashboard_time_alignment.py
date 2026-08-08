# -*- coding: utf-8 -*-
import json
import re
import unittest
from pathlib import Path


DASHBOARD_PATH = (
    Path(__file__).resolve().parent
    / "grafana_dashboards"
    / "addforex__🔍 进程取证与安全审计舱.json"
)
PANEL_TITLE = "💀 敏感凭据/密码窗口聚焦期后台活动进程审计舱"


def load_panel_sql():
    with DASHBOARD_PATH.open(encoding="utf-8") as dashboard_file:
        dashboard = json.load(dashboard_file)

    matching_panels = [
        panel for panel in dashboard.get("panels", []) if panel.get("title") == PANEL_TITLE
    ]
    if len(matching_panels) != 1:
        raise AssertionError(
            f"expected exactly one {PANEL_TITLE!r} panel, found {len(matching_panels)}"
        )

    targets = matching_panels[0].get("targets", [])
    matching_targets = [target for target in targets if target.get("refId") == "A"]
    if len(matching_targets) != 1 or not matching_targets[0].get("rawSql"):
        raise AssertionError("expected panel target A to contain rawSql")
    return matching_targets[0]["rawSql"]


class DashboardTimeAlignmentTests(unittest.TestCase):
    def test_sensitive_focus_includes_one_overlapping_carry_in_slice(self):
        sql = load_panel_sql()
        normalized_sql = re.sub(r"\s+", " ", sql).strip()

        sensitive_focus = re.search(
            r"WITH sensitive_focus AS \((.*?)\), active_bg AS",
            normalized_sql,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(sensitive_focus)

        in_range, union_all, carry_in = sensitive_focus.group(1).partition(
            " UNION ALL "
        )
        self.assertEqual(union_all, " UNION ALL ")
        self.assertIn("$__timeFilter(timestamp)", in_range)

        carry_in_query = re.fullmatch(
            (
                r"SELECT timestamp, end_timestamp, window_title "
                r"FROM \( SELECT timestamp, end_timestamp, window_title "
                r"FROM public\.fact_process_context WHERE (?P<latest_where>.*?) "
                r"ORDER BY timestamp DESC LIMIT 1 \) carry_in "
                r"WHERE (?P<outer_where>.*)"
            ),
            carry_in,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(carry_in_query)
        self.assertEqual(
            carry_in_query.group("latest_where"),
            "is_foreground = 1 AND timestamp < $__timeFrom()::timestamptz",
        )

        outer_where = carry_in_query.group("outer_where")
        self.assertIn("LOWER(window_title) LIKE '%keepass%'", outer_where)
        self.assertIn(
            "COALESCE(end_timestamp, now()) > $__timeFrom()::timestamptz",
            outer_where,
        )

    def test_sensitive_focus_activity_uses_context_slice_interval(self):
        sql = load_panel_sql()
        normalized_sql = re.sub(r"\s+", " ", sql).strip()

        sensitive_focus = re.search(
            r"WITH sensitive_focus AS \((.*?)\), active_bg AS",
            normalized_sql,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(sensitive_focus)
        self.assertRegex(sensitive_focus.group(1), r"\bend_timestamp\b")
        self.assertIn("$__timeFilter(timestamp)", sensitive_focus.group(1))

        self.assertNotRegex(
            normalized_sql,
            r"act\.timestamp\s*=\s*sf\.timestamp",
        )
        self.assertRegex(
            normalized_sql,
            (
                r"JOIN sensitive_focus sf ON "
                r"act\.timestamp >= sf\.timestamp AND "
                r"act\.timestamp < COALESCE\(sf\.end_timestamp, now\(\)\)"
            ),
        )
        self.assertIn("WHERE $__timeFilter(act.timestamp)", normalized_sql)


if __name__ == "__main__":
    unittest.main()
