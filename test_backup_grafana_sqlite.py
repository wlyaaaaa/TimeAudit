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

    def test_public_dashboard_snapshots_keep_semantic_matcher_ids(self):
        snapshot_dir = os.path.join(os.path.dirname(__file__), "grafana_dashboards")
        retired_datasource_uid = "bfoc1vymtgni8a"

        def assert_matchers(node, source, path="dashboard"):
            if isinstance(node, dict):
                matcher = node.get("matcher")
                if isinstance(matcher, dict):
                    self.assertIsInstance(
                        matcher.get("id"),
                        str,
                        f"{source}: {path}.matcher.id is missing",
                    )
                    self.assertTrue(
                        matcher["id"],
                        f"{source}: {path}.matcher.id is empty",
                    )
                for key, value in node.items():
                    assert_matchers(value, source, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_matchers(value, source, f"{path}[{index}]")

        for name in sorted(os.listdir(snapshot_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(snapshot_dir, name)
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            self.assertNotIn(
                retired_datasource_uid,
                json.dumps(document, ensure_ascii=False),
                f"{name}: retired Grafana datasource is still referenced",
            )
            assert_matchers(document, name)

    def test_older_live_dashboard_cannot_overwrite_newer_repository_snapshot(self):
        os.makedirs(backup.DASH_DIR)
        path = os.path.join(backup.DASH_DIR, "dash-1__Main Dashboard.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "uid": "dash-1",
                    "title": "Main Dashboard",
                    "version": 8,
                    "panels": [{"title": "repository fix"}],
                },
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")

        with self.assertRaisesRegex(backup.GitSyncError, "older than repository"):
            backup.write_dashboard_documents(
                [
                    (
                        "dash-1",
                        {
                            "uid": "dash-1",
                            "title": "Main Dashboard",
                            "version": 7,
                            "panels": [{"title": "old live copy"}],
                        },
                    )
                ],
                before_write=lambda: None,
            )

        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["panels"][0]["title"], "repository fix")

    def test_same_version_divergence_fails_closed_but_newer_live_version_exports(self):
        os.makedirs(backup.DASH_DIR)
        path = os.path.join(backup.DASH_DIR, "dash-1__Main Dashboard.json")
        repository = {
            "uid": "dash-1",
            "title": "Main Dashboard",
            "version": 8,
            "panels": [{"title": "repository fix"}],
        }
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(repository, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")

        divergent = dict(repository)
        divergent["panels"] = [{"title": "different live copy"}]
        with self.assertRaisesRegex(backup.GitSyncError, "same-version divergence"):
            backup.write_dashboard_documents(
                [("dash-1", divergent)],
                before_write=lambda: None,
            )

        newer = dict(divergent)
        newer["version"] = 9
        written = backup.write_dashboard_documents(
            [("dash-1", newer)],
            before_write=lambda: None,
        )
        self.assertEqual(written, {"dash-1__Main Dashboard.json"})
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["version"], 9)

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
                    "fieldConfig": {
                        "overrides": [
                            {
                                "matcher": {
                                    "id": "byName",
                                    "options": "GPU 温度",
                                }
                            }
                        ]
                    },
                },
                {"title": "CPU 频率 (9950X3D)"},
            ],
        }

        normalized = backup.normalize_dashboard_for_public_backup(dashboard)

        self.assertNotIn("id", normalized)
        self.assertNotIn("id", normalized["panels"][0])
        self.assertEqual(
            normalized["panels"][0]["fieldConfig"]["overrides"][0]["matcher"][
                "id"
            ],
            "byName",
        )
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
