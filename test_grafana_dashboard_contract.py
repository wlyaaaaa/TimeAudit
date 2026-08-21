import json
import os
import tempfile
import unittest
from pathlib import Path

import backup_grafana as backup
from grafana_dashboard_contract import (
    DashboardContractError,
    RECOVERY_DATASOURCE_UID,
    discover_dashboard_files,
    validate_dashboard_document,
)


ROOT = Path(__file__).resolve().parent


def valid_dashboard(uid="dash-1"):
    return {
        "uid": uid,
        "title": "Dashboard",
        "panels": [
            {
                "datasource": {
                    "type": "grafana-postgresql-datasource",
                    "uid": RECOVERY_DATASOURCE_UID,
                },
                "fieldConfig": {
                    "overrides": [
                        {"matcher": {"id": "byName", "options": "value"}}
                    ]
                },
            }
        ],
    }


class GrafanaDashboardContractTests(unittest.TestCase):
    def test_discovery_excludes_editor_backup_and_requested_backup_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory, "active.json")
            legacy = Path(directory, "legacy.json.bak")
            active.write_text("{}", encoding="utf-8")
            legacy.write_text("{}", encoding="utf-8")

            self.assertEqual(discover_dashboard_files(directory), [str(active)])
            with self.assertRaisesRegex(DashboardContractError, "exact .json"):
                discover_dashboard_files(directory, str(legacy))

    def test_contract_rejects_retired_or_unknown_postgres_datasource(self):
        retired = valid_dashboard()
        retired["panels"][0]["datasource"]["uid"] = "bfoc1vymtgni8a"
        with self.assertRaisesRegex(DashboardContractError, "retired datasource"):
            validate_dashboard_document(retired)

        unknown = valid_dashboard()
        unknown["panels"][0]["datasource"]["uid"] = "unknown-postgres"
        with self.assertRaisesRegex(DashboardContractError, "recovery uid"):
            validate_dashboard_document(unknown)

    def test_contract_rejects_missing_matcher_id(self):
        dashboard = valid_dashboard()
        del dashboard["panels"][0]["fieldConfig"]["overrides"][0]["matcher"]["id"]
        with self.assertRaisesRegex(DashboardContractError, "matcher.id"):
            validate_dashboard_document(dashboard)

    def test_backup_validates_all_documents_before_writing(self):
        dashboard = valid_dashboard()
        dashboard["panels"][0]["datasource"]["uid"] = "bfoc1vymtgni8a"
        with tempfile.TemporaryDirectory() as directory:
            original = backup.DASH_DIR
            backup.DASH_DIR = os.path.join(directory, "dashboards")
            try:
                with self.assertRaises(DashboardContractError):
                    backup.write_dashboard_documents(
                        [(dashboard["uid"], dashboard)], before_write=lambda: None
                    )
                self.assertFalse(os.path.exists(backup.DASH_DIR))
            finally:
                backup.DASH_DIR = original

    def test_repository_snapshots_and_recovery_datasource_share_contract(self):
        snapshots = sorted((ROOT / "grafana_dashboards").glob("*.json"))
        self.assertEqual(len(snapshots), 6)
        for path in snapshots:
            with path.open(encoding="utf-8") as handle:
                validate_dashboard_document(json.load(handle), source=path.name)

        datasource = (
            ROOT / "grafana_provisioning" / "datasources" / "datasource.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(f"uid: {RECOVERY_DATASOURCE_UID}", datasource)

        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("./grafana_dashboards:", compose)

    def test_tracked_historical_backup_is_not_recoverable(self):
        backup_path = ROOT / "grafana_dashboards" / "adfkm96__屏幕使用时间.json.bak"
        self.assertTrue(backup_path.is_file())
        with backup_path.open(encoding="utf-8") as handle:
            dashboard = json.load(handle)
        with self.assertRaisesRegex(DashboardContractError, "retired datasource"):
            validate_dashboard_document(dashboard, source=backup_path.name)


if __name__ == "__main__":
    unittest.main()
