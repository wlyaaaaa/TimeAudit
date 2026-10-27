import os
import unittest
from contextlib import ExitStack
from unittest import mock

import backup_grafana as backup
import grafana_auth as auth
import restore_grafana as restore
from test_restore_grafana import write_dashboard
import tempfile


class GrafanaAuthTests(unittest.TestCase):
    def test_native_delivery_and_endpoint_binding(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(auth, "_receive_header", return_value="Basic fixture") as receive:
            self.assertEqual(auth.authorization_header(auth.LOCAL_URL + "/"), "Basic fixture")
            for url in ("http://localhost:43000", "https://example.com", auth.LOCAL_URL + "/other"):
                with self.assertRaises(RuntimeError):
                    auth.authorization_header(url)
            receive.assert_called_once()

    def test_existing_private_environment_compatibility(self):
        with mock.patch.dict(os.environ, {"GRAFANA_USER": "fixture", "GRAFANA_PASSWORD": "value"}), mock.patch.object(auth, "_receive_header") as receive:
            self.assertEqual(auth.authorization_header(auth.LOCAL_URL), "Basic Zml4dHVyZTp2YWx1ZQ==")
            receive.assert_not_called()

    def test_restore_uses_broker_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_dashboard(directory)
            with mock.patch.object(restore, "authorization_header", side_effect=RuntimeError("unavailable")), mock.patch.object(restore, "api_get") as api:
                self.assertEqual(restore.main(["--file", str(path)]), 2)
                api.assert_not_called()
            with mock.patch.object(restore, "authorization_header", return_value="Basic fixture"), mock.patch.object(restore, "api_get", side_effect=[{"uid": restore.RECOVERY_DATASOURCE_UID, "type": "postgres"}, {"dashboard": {"uid": "dash-1"}}]), mock.patch.object(restore, "api_post", return_value={"status": "success"}) as post:
                self.assertEqual(restore.main(["--file", str(path)]), 0)
                self.assertEqual(post.call_args.args[2], "Basic fixture")

    def test_api_backup_uses_shared_authentication(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch("sys.argv", ["backup_grafana.py", "--source", "api", "--no-git"]))
            for name in ("initialize_windows_user_proxy", "grafana_backup_lock", "assert_dashboard_worktree_clean", "backup_grafana_db", "assert_dashboard_change_allowlist"):
                stack.enter_context(mock.patch.object(backup, name))
            stack.enter_context(mock.patch.object(backup, "authorization_header", return_value="Basic fixture"))
            export = stack.enter_context(mock.patch.object(backup, "export_dashboards"))
            self.assertEqual(backup.main(), 0)
            self.assertEqual(export.call_args.args, (auth.LOCAL_URL, "Basic fixture"))
