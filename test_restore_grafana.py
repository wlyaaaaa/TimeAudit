import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import restore_grafana as restore
from grafana_dashboard_contract import RECOVERY_DATASOURCE_UID


def write_dashboard(directory, uid="dash-1"):
    path = Path(directory, f"{uid}.json")
    path.write_text(
        json.dumps(
            {
                "uid": uid,
                "title": "Dashboard",
                "panels": [
                    {
                        "datasource": {
                            "type": "grafana-postgresql-datasource",
                            "uid": RECOVERY_DATASOURCE_UID,
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class GrafanaRestoreTests(unittest.TestCase):
    def run_restore(self, path, api_get, api_post):
        with mock.patch.dict(
            os.environ,
            {"GRAFANA_USER": "test-user", "GRAFANA_PASSWORD": "test-password"},
        ), mock.patch.object(restore, "api_get", api_get), mock.patch.object(
            restore, "api_post", api_post
        ):
            return restore.main(["--file", str(path)])

    def test_restore_preflight_rejects_wrong_datasource_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_dashboard(directory)
            api_get = mock.Mock(
                return_value={"uid": RECOVERY_DATASOURCE_UID, "type": "mysql"}
            )
            api_post = mock.Mock()

            self.assertEqual(self.run_restore(path, api_get, api_post), 1)
            api_post.assert_not_called()

    def test_restore_posts_then_reads_back_exact_dashboard_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_dashboard(directory)
            api_get = mock.Mock(
                side_effect=[
                    {"uid": RECOVERY_DATASOURCE_UID, "type": "postgres"},
                    {"dashboard": {"uid": "dash-1"}},
                ]
            )
            api_post = mock.Mock(return_value={"status": "success", "version": 2})

            self.assertEqual(self.run_restore(path, api_get, api_post), 0)
            api_post.assert_called_once()
            self.assertEqual(
                [call.args[1] for call in api_get.call_args_list],
                [
                    f"/api/datasources/uid/{RECOVERY_DATASOURCE_UID}",
                    "/api/dashboards/uid/dash-1",
                ],
            )

    def test_restore_fails_when_post_success_readback_uid_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_dashboard(directory)
            api_get = mock.Mock(
                side_effect=[
                    {"uid": RECOVERY_DATASOURCE_UID, "type": "postgres"},
                    {"dashboard": {"uid": "different-dashboard"}},
                ]
            )
            api_post = mock.Mock(return_value={"status": "success", "version": 2})

            self.assertEqual(self.run_restore(path, api_get, api_post), 1)

    def test_dry_run_is_static_and_does_not_call_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_dashboard(directory)
            with mock.patch.object(restore, "api_get") as api_get, mock.patch.object(
                restore, "api_post"
            ) as api_post:
                self.assertEqual(
                    restore.main(["--dry-run", "--file", str(path)]), 0
                )
            api_get.assert_not_called()
            api_post.assert_not_called()

    def test_invalid_batch_is_rejected_before_credentials_or_api(self):
        with tempfile.TemporaryDirectory() as directory:
            write_dashboard(directory, "valid")
            invalid = write_dashboard(directory, "invalid")
            document = json.loads(invalid.read_text(encoding="utf-8"))
            document["panels"][0]["datasource"]["uid"] = "bfoc1vymtgni8a"
            invalid.write_text(json.dumps(document), encoding="utf-8")

            original = restore.DASH_DIR
            restore.DASH_DIR = directory
            try:
                with mock.patch.object(restore, "api_get") as api_get, mock.patch.object(
                    restore, "api_post"
                ) as api_post, mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(restore.main([]), 1)
                api_get.assert_not_called()
                api_post.assert_not_called()
            finally:
                restore.DASH_DIR = original


if __name__ == "__main__":
    unittest.main()
