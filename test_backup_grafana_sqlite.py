import json
import os
import sqlite3
import tempfile
import unittest

import backup_grafana as backup


class GrafanaSqliteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="timeaudit-grafana-backup-")
        self.original_dash_dir = backup.DASH_DIR
        self.original_database = backup.GRAFANA_DB
        self.original_backup_dir = backup.DB_BACKUP_DIR
        backup.DASH_DIR = os.path.join(self.temp.name, "dashboards")
        backup.GRAFANA_DB = os.path.join(self.temp.name, "grafana.db")
        backup.DB_BACKUP_DIR = os.path.join(self.temp.name, "backups")

    def tearDown(self):
        backup.DASH_DIR = self.original_dash_dir
        backup.GRAFANA_DB = self.original_database
        backup.DB_BACKUP_DIR = self.original_backup_dir
        self.temp.cleanup()

    def create_unified_database(self):
        connection = sqlite3.connect(backup.GRAFANA_DB)
        connection.execute(
            """
            CREATE TABLE resource (
                guid TEXT,
                resource_version INTEGER,
                "group" TEXT,
                resource TEXT,
                namespace TEXT,
                name TEXT,
                value BLOB,
                action INTEGER,
                label_set TEXT,
                previous_resource_version INTEGER,
                folder TEXT
            )
            """
        )
        resource = {
            "metadata": {"name": "dash-1", "generation": 7},
            "spec": {"title": "Main / Dashboard", "panels": [], "id": 99},
        }
        connection.execute(
            'INSERT INTO resource ("group", resource, name, value) VALUES (?, ?, ?, ?)',
            (
                "dashboard.grafana.app",
                "dashboards",
                "dash-1",
                json.dumps(resource),
            ),
        )
        connection.commit()
        connection.close()

    def test_unified_storage_exports_normalized_dashboard_without_credentials(self):
        self.create_unified_database()

        written = backup.export_dashboards_from_db()

        self.assertEqual(written, {"dash-1__Main _ Dashboard.json"})
        path = os.path.join(backup.DASH_DIR, next(iter(written)))
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["uid"], "dash-1")
        self.assertEqual(document["version"], 7)
        self.assertNotIn("id", document)

    def test_consistent_database_backup_passes_quick_check(self):
        self.create_unified_database()

        backup.backup_grafana_db(keep=2)

        snapshots = os.listdir(backup.DB_BACKUP_DIR)
        self.assertEqual(len(snapshots), 1)
        snapshot = os.path.join(backup.DB_BACKUP_DIR, snapshots[0])
        connection = sqlite3.connect(snapshot)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute(
                    'SELECT COUNT(*) FROM resource WHERE "group"=? AND resource=?',
                    ("dashboard.grafana.app", "dashboards"),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_proxy_parser_supports_single_and_split_windows_formats(self):
        self.assertEqual(
            backup.resolve_proxy_settings("127.0.0.1:7892"),
            {
                "http": "http://127.0.0.1:7892",
                "https": "http://127.0.0.1:7892",
            },
        )

    def test_public_snapshot_removes_hardware_skus_from_titles_only(self):
        dashboard = {
            "id": 12,
            "title": "Hardware",
            "panels": [
                {
                    "id": 13,
                    "title": "GPU 温度与功率 (RTX5080)",
                    "description": "RTX5080 remains in non-title content",
                },
                {"title": "CPU 频率 (9950X3D)"},
            ],
        }

        normalized = backup.normalize_dashboard_for_public_backup(dashboard)

        self.assertNotIn("id", normalized)
        self.assertNotIn("id", normalized["panels"][0])
        self.assertEqual(normalized["panels"][0]["title"], "GPU 温度与功率")
        self.assertEqual(normalized["panels"][1]["title"], "CPU 频率")
        self.assertIn("RTX5080", normalized["panels"][0]["description"])
        self.assertEqual(
            backup.resolve_proxy_settings(
                "http=127.0.0.1:8080;https=https://127.0.0.1:8443"
            ),
            {
                "http": "http://127.0.0.1:8080",
                "https": "https://127.0.0.1:8443",
            },
        )


if __name__ == "__main__":
    unittest.main()
