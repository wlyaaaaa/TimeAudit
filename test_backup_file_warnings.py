import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import backup_grafana as grafana
import timeaudit_backup as postgres
import backup_file_warnings as warnings
import sqlite3
import time
from backup_file_warnings import file_warning
from clipboard_history import backup
from clipboard_history.storage import ClipboardStore


def av(code):
    exc = OSError("synthetic antivirus block")
    exc.winerror = code
    return exc


class FileWarningTests(unittest.TestCase):
    def test_grafana_sqlite_target_evidence_keeps_git_branch_running(self):
        with tempfile.TemporaryDirectory() as name, \
             patch.object(grafana, "GRAFANA_DB", str(Path(name) / "source.db")), \
             patch.object(grafana.sys, "argv", ["backup"]), \
             patch.object(grafana, "initialize_windows_user_proxy", return_value=False), \
             patch.object(grafana, "grafana_backup_lock"), \
             patch.object(grafana, "assert_dashboard_worktree_clean"), \
             patch.object(grafana, "export_dashboards_from_db"), \
             patch.object(grafana, "assert_dashboard_change_allowlist", return_value=set()), \
             patch.object(grafana, "git_commit_and_push") as git:
            def target_failure(keep, candidates):
                candidates.append((Path(name) / "target.partial", "grafana.db"))
                raise sqlite3.OperationalError("generic target error")
            with patch.object(grafana, "_backup_grafana_db", side_effect=target_failure), \
                 patch.object(warnings, "defender_records", side_effect=lambda paths, started: [
                     {"path": str(paths[-1]), "success": True, "observed_unix": time.time()}]):
                self.assertEqual(grafana.main(), 0)
                git.assert_called_once()
            git.reset_mock()
            with patch.object(grafana, "_backup_grafana_db", side_effect=target_failure), \
                 patch.object(warnings, "defender_records", return_value=[]):
                self.assertEqual(grafana.main(), 1)
                git.assert_not_called()

    def test_sqlite_evidence_requires_current_exact_successful_path(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "database.sqlite3"; started = time.time() - 2
            good = {"path": str(path), "success": True, "observed_unix": started + 1}
            exc = sqlite3.OperationalError("generic error, no Windows code")
            for record in ({**good, "path": str(path.parent / "different.sqlite3")},
                           {**good, "observed_unix": started - 1}, {**good, "success": False}):
                self.assertIsNone(warnings.sqlite_warning(exc, [(path, path.name)], started, "test", records=[record]))
            result = warnings.sqlite_warning(exc, [(path, path.name)], started, "test", records=[good])
            self.assertEqual(result["reason"], "antivirus_removed")
            self.assertEqual(result["error_code"], 226)
            self.assertIsNone(warnings.sqlite_warning(exc, [(path.parent / "missing-root/db", "db")], started, "test", records=[good]))

    def test_clipboard_sqlite_av_keeps_database_but_updates_independent_control(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); source = root / "source"; target = root / "backup"
            source.mkdir(); target.mkdir()
            target.joinpath(backup.BACKUP_FILENAME).write_bytes(b"old synthetic database")
            source.joinpath("control.json").write_text("new synthetic control")
            def unavailable(source_root, backup_root, candidates):
                candidates.append((source_root / "clipboard_history.sqlite3", "clipboard_history.sqlite3"))
                raise sqlite3.OperationalError("generic missing source database")
            def records(paths, started):
                return [{"path": str(paths[0]), "success": True, "observed_unix": time.time()}]
            with patch.object(backup, "_create_backup", side_effect=unavailable), \
                 patch.object(warnings, "defender_records", side_effect=records):
                result = backup.create_backup(source, target)
            self.assertFalse(result["current_data_copied"])
            self.assertEqual(result["file_warnings"][0]["error_code"], 226)
            self.assertEqual(target.joinpath(backup.BACKUP_FILENAME).read_bytes(), b"old synthetic database")
            self.assertEqual(target.joinpath("control.json").read_text(), "new synthetic control")
            with patch.object(backup, "_create_backup", side_effect=unavailable), \
                 patch.object(warnings, "defender_records", return_value=[]):
                with self.assertRaises(sqlite3.OperationalError): backup.create_backup(source, target)

    def test_grafana_sqlite_source_without_evidence_fails(self):
        with tempfile.TemporaryDirectory() as name, \
             patch.object(grafana, "GRAFANA_DB", str(Path(name) / "missing.sqlite3")), \
             patch.object(grafana.sys, "argv", ["backup", "--no-git"]), \
             patch.object(grafana, "initialize_windows_user_proxy", return_value=False), \
             patch.object(grafana, "grafana_backup_lock"), \
             patch.object(grafana, "assert_dashboard_worktree_clean"), \
             patch.object(grafana, "export_dashboards_from_db", side_effect=sqlite3.OperationalError("generic")):
            with patch.object(warnings, "defender_records", return_value=[]):
                self.assertEqual(grafana.main(), 1)
            with patch.object(warnings, "defender_records", side_effect=lambda paths, started: [
                {"path": str(paths[0]), "success": True, "observed_unix": time.time()}]):
                self.assertEqual(grafana.main(), 0)

    def test_postgres_archive_av_warns_but_ordinary_io_fails(self):
        with tempfile.TemporaryDirectory() as name:
            exc = OSError(13, "synthetic", str(Path(name) / "candidate.dump.partial")); exc.winerror = 225
            exc.stage = "archive_export"
            with patch.object(postgres, "backup", side_effect=exc):
                self.assertEqual(postgres.main(["backup", "--backup-dir", name]), 0)
            with patch.object(postgres, "backup", side_effect=PermissionError(13, "full disk", str(Path(name) / "candidate.dump.partial"))):
                self.assertEqual(postgres.main(["backup", "--backup-dir", name]), 1)

    def test_classifier_does_not_hide_permission_or_unenumerated_missing(self):
        self.assertIsNone(file_warning(PermissionError(), "a", "copy"))
        self.assertIsNone(file_warning(FileNotFoundError(), "a", "copy"))
        for code in (225, 226):
            self.assertEqual(file_warning(av(code), "a", "copy")["error_code"], code)

    def test_control_av_preserves_old_control_and_verifiable_database(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); source = root / "source"; target = root / "backup"
            store = ClipboardStore(source / "clipboard_history.sqlite3")
            store.initialize(); store.close()
            source.joinpath("control.json").write_text("old")
            backup.create_backup(source, target)
            source.joinpath("control.json").write_text("new")
            for code in (225, 226):
                with patch.object(backup.shutil, "copyfile", side_effect=av(code)):
                    result = backup.create_backup(source, target)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["file_warnings"][0]["error_code"], code)
                self.assertEqual(target.joinpath("control.json").read_text(), "old")
                self.assertTrue(backup.verify_backup(target)["valid"])
            with patch.object(backup.shutil, "copyfile", side_effect=PermissionError("target unavailable")):
                with self.assertRaises(PermissionError): backup.create_backup(source, target)
            def disappears(*args):
                source.joinpath("control.json").rename(root / "moved-control.json")
                raise FileNotFoundError(2, "synthetic disappeared")
            with patch.object(backup.shutil, "copyfile", side_effect=disappears):
                result = backup.create_backup(source, target)
            self.assertEqual(result["file_warnings"][0]["reason"], "source_disappeared")
            self.assertEqual(target.joinpath("control.json").read_text(), "old")

    def test_grafana_real_target_error_fails_and_av_warns(self):
        with patch.object(grafana.sys, "argv", ["backup", "--no-git"]), \
             patch.object(grafana, "initialize_windows_user_proxy", return_value=False), \
             patch.object(grafana, "grafana_backup_lock"), \
             patch.object(grafana, "assert_dashboard_worktree_clean"), \
             patch.object(grafana, "export_dashboards_from_db"), \
             patch.object(grafana, "assert_dashboard_change_allowlist", return_value=set()):
            with patch.object(grafana, "backup_grafana_db", side_effect=PermissionError("full disk")):
                self.assertEqual(grafana.main(), 1)
            with patch.object(grafana, "backup_grafana_db", side_effect=av(225)):
                self.assertEqual(grafana.main(), 0)
