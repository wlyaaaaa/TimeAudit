import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

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

    def test_literal_database_uri_paths_remain_read_only(self):
        backup.GRAFANA_DB = os.path.join(self.temp.name, "grafana#sample%25.db")
        self.create_unified_database()
        source = Path(backup.GRAFANA_DB)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        written = backup.export_dashboards_from_db(before_write=lambda: None)
        self.assertEqual({"dash-1__Main _ Dashboard.json"}, written)
        document = json.loads((Path(backup.DASH_DIR) / next(iter(written))).read_text(encoding="utf-8"))
        self.assertEqual({"uid": "dash-1", "title": "Main / Dashboard", "version": 7, "panels": []}, document)
        backup.backup_grafana_db(keep=2)
        snapshots = list(Path(backup.DB_BACKUP_DIR).glob("*.db"))
        self.assertEqual(1, len(snapshots))
        connection = sqlite3.connect(snapshots[0])
        try:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM resource").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertFalse(source.with_name("grafana").exists())

        absent = source.with_name("absent#sample%25.db")
        with self.assertRaises(sqlite3.OperationalError):
            backup.export_dashboards_from_db(str(absent), before_write=lambda: None)
        self.assertFalse(absent.exists())
        self.assertFalse(absent.with_name("absent").exists())

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

    def test_v2_backup_preserves_element_ids_and_reports_element_count(self):
        dashboard = {
            "uid": "v2", "version": 7, "id": 99, "title": "Hardware (RTX5080)",
            "elements": {"panel-1": {"kind": "Panel", "spec": {
                "id": 7, "title": "Panel",
                "data": {"kind": "QueryGroup", "spec": {"transformations": [
                    {"kind": "Transformation", "group": "organize", "spec": {"options": {}}}
                ]}},
                "fieldConfig": {"overrides": [{"matcher": {"id": "byName"},
                    "properties": [{"id": "unit", "value": "short"}]}]},
            }}}, "layout": {},
        }
        original = json.loads(json.dumps(dashboard))
        with mock.patch.object(backup, "log") as log:
            written = backup.write_dashboard_documents([("v2", dashboard)], before_write=lambda: None)
        document = json.loads((Path(backup.DASH_DIR) / next(iter(written))).read_text(encoding="utf-8"))
        self.assertEqual(document["elements"], dashboard["elements"])
        self.assertNotIn("id", document)
        self.assertEqual(document["title"], "Hardware")
        self.assertEqual(document["version"], 7)
        self.assertEqual(dashboard, original)
        self.assertIn("面板 1 个", log.call_args.args[0])

    def test_public_dashboard_snapshots_keep_semantic_matcher_ids(self):
        root = Path(__file__).resolve().parent
        retired_datasource_uid = "bfoc1vymtgni8a"

        def assert_matchers(node, source, path="dashboard"):
            if isinstance(node, dict):
                title = node.get("title")
                if isinstance(title, str):
                    self.assertIsNone(
                        backup.HARDWARE_SKU_IN_TITLE.search(title),
                        f"{source}: {path}.title contains a hardware model",
                    )
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

        for path in sorted([*root.glob("grafana_dashboards/*.json"), *root.glob("grafana_provisioning/dashboards/*.json")]):
            name = path.name
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

    def test_same_version_semantic_match_preserves_manual_formatting(self):
        os.makedirs(backup.DASH_DIR)
        path = os.path.join(backup.DASH_DIR, "dash-1__Main Dashboard.json")
        dashboard = {"uid": "dash-1", "title": "Main Dashboard", "version": 8, "panels": []}
        original = json.dumps(dashboard, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(original)
        changed = set()

        written = backup.write_dashboard_documents(
            [("dash-1", dashboard)], changed_paths=changed, before_write=lambda: None,
        )

        self.assertEqual(written, {"dash-1__Main Dashboard.json"})
        self.assertEqual(changed, set())
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

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
                                },
                                "properties": [
                                    {"id": "unit", "value": "celsius"},
                                    {"id": "thresholds", "value": {
                                        "mode": "absolute",
                                        "steps": [{"color": "red", "value": 80}],
                                    }},
                                ],
                            }
                        ]
                    },
                    "transformations": [
                        {
                            "id": "organize",
                            "options": {"excludeByName": {"duration": True}},
                        }
                    ],
                },
                {"title": "CPU 频率 (9950X3D)"},
                {"title": "整机显存 RTX 5080 监控"},
            ],
        }

        original = json.loads(json.dumps(dashboard))
        normalized = backup.normalize_dashboard_for_public_backup(dashboard)

        self.assertNotIn("id", normalized)
        self.assertNotIn("id", normalized["panels"][0])
        self.assertEqual(
            normalized["panels"][0]["fieldConfig"]["overrides"][0]["matcher"][
                "id"
            ],
            "byName",
        )
        self.assertEqual(
            normalized["panels"][0]["transformations"][0]["id"],
            "organize",
        )
        self.assertEqual(
            normalized["panels"][0]["fieldConfig"]["overrides"][0]["properties"],
            original["panels"][0]["fieldConfig"]["overrides"][0]["properties"],
        )
        self.assertEqual(dashboard, original)
        self.assertEqual(normalized["panels"][0]["title"], "GPU 温度与功率")
        self.assertEqual(normalized["panels"][1]["title"], "CPU 频率")
        self.assertEqual(normalized["panels"][2]["title"], "整机显存 监控")
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
