from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate_website_dashboards as generate
from grafana_dashboard_contract import DashboardContractError


ROOT = Path(__file__).resolve().parent
CANONICAL_PATH = generate.DASHBOARD_DIR / f"{generate.CANONICAL_UID}.json"


class WebsiteDashboardGenerationTests(unittest.TestCase):
    def setUp(self):
        self.canonical = json.loads(CANONICAL_PATH.read_text(encoding="utf-8"))

    def test_exact_4_plus_2_subsets_preserve_full_queries_styles_and_time(self):
        original = deepcopy(self.canonical)
        views = generate.derive_website_dashboards(self.canonical)
        self.assertEqual(self.canonical, original)
        by_id = {panel["id"]: panel for panel in original["panels"]}
        covered = []
        for group, expected_ids in (
            ("cpu-gpu", [1, 2, 3, 4]), ("memory-network", [5, 6])
        ):
            view = views[f"website-computer-status-{group}"]
            self.assertEqual([panel["id"] for panel in view["panels"]], expected_ids)
            self.assertEqual(
                {key: value for key, value in view.items()
                 if key not in ("id", "uid", "title", "panels")},
                {key: value for key, value in original.items()
                 if key not in ("id", "uid", "title", "panels")},
            )
            for index, panel in enumerate(view["panels"]):
                self.assertEqual(
                    {key: value for key, value in panel.items() if key != "gridPos"},
                    {key: value for key, value in by_id[panel["id"]].items()
                     if key != "gridPos"},
                )
                self.assertEqual(panel["gridPos"], {"h": 7, "w": 24, "x": 0, "y": index * 7})
                covered.append(panel["id"])
        self.assertEqual(covered, [1, 2, 3, 4, 5, 6])

    def test_rejects_missing_sql_and_ambiguous_or_expanded_source(self):
        for mutation in ("sql_missing", "sql_blank", "duplicate_id", "extra_panel", "wrong_uid"):
            with self.subTest(mutation=mutation):
                canonical = deepcopy(self.canonical)
                if mutation == "sql_missing":
                    del canonical["panels"][0]["targets"][0]["rawSql"]
                elif mutation == "sql_blank":
                    canonical["panels"][0]["targets"][0]["rawSql"] = " "
                elif mutation == "duplicate_id":
                    canonical["panels"][1]["id"] = 1
                elif mutation == "extra_panel":
                    canonical["panels"].append(deepcopy(canonical["panels"][0]))
                else:
                    canonical["uid"] = "another-dashboard"
                with self.assertRaises(DashboardContractError):
                    generate.derive_website_dashboards(canonical)

    def test_checked_in_outputs_are_current(self):
        self.assertEqual(generate.main(["--check"]), 0)

    def test_command_detects_drift_then_rebuilds_from_canonical_without_touching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            source = output_dir / CANONICAL_PATH.name
            canonical = deepcopy(self.canonical)
            canonical["panels"][0]["targets"][0]["rawSql"] += "\n-- maintenance edit"
            canonical["panels"][0]["fieldConfig"]["defaults"]["decimals"] = 3
            source.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")
            source_bytes = source.read_bytes()
            with mock.patch.object(generate, "DASHBOARD_DIR", output_dir):
                self.assertEqual(generate.main(["--check"]), 1)
                self.assertEqual(list(output_dir.glob("*.json")), [source])
                self.assertEqual(generate.main([]), 0)
                self.assertEqual(generate.main(["--check"]), 0)
                first_output = output_dir / "website-computer-status-cpu-gpu.json"
                view = json.loads(first_output.read_text(encoding="utf-8"))
                self.assertEqual(view["panels"][0]["targets"], canonical["panels"][0]["targets"])
                self.assertEqual(view["panels"][0]["fieldConfig"], canonical["panels"][0]["fieldConfig"])
                view["panels"][0]["targets"][0]["rawSql"] = "SELECT 1"
                first_output.write_text(json.dumps(view), encoding="utf-8")
                stale_bytes = first_output.read_bytes()
                self.assertEqual(generate.main(["--check"]), 1)
                self.assertEqual(first_output.read_bytes(), stale_bytes)
                self.assertEqual(generate.main([]), 0)
                self.assertEqual(generate.main(["--check"]), 0)
            self.assertEqual(source.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
