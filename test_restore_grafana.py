import json
import os
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from pathlib import Path
from unittest import mock

import restore_grafana as restore
from grafana_dashboard_contract import RECOVERY_DATASOURCE_UID


def write_dashboard(directory, uid="dash-1", v2=False):
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
    if v2:
        document = {"uid": "dash/ 1?#", "version": 4, "id": 99, "title": "V2",
                    "elements": {"panel-1": {"kind": "Panel", "spec": {"id": 7}}}, "layout": {}}
        path.write_text(json.dumps(document), encoding="utf-8")
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
            for v2 in (False, True):
                path = write_dashboard(directory, v2=v2)
                with mock.patch.object(restore, "api_get") as api_get, mock.patch.object(
                    restore, "api_post"
                ) as api_post:
                    self.assertEqual(restore.main(["--dry-run", "--file", str(path)]), 0)
                api_get.assert_not_called()
                api_post.assert_not_called()

    def test_v2_restore_creation_updates_and_errors_keep_exact_uid(self):
        uid = "dash/ 1?#"
        collection = "/apis/dashboard.grafana.app/v2/namespaces/default/dashboards"
        exact = collection + "/dash%2F%201%3F%23"
        resource = {"metadata": {"name": uid, "resourceVersion": "rv-1", "generation": 10, "annotations": {"grafana.app/folder": "original-folder"}}}
        wrong = {"metadata": {"name": "wrong"}}
        cases = [
            ("create", HTTPError(exact, 404, "missing", {}, None), resource, resource, 0),
            ("update", resource, resource, resource, 0),
            ("update_without_version", {"metadata": {"name": uid}}, resource, resource, 0),
            ("denied", HTTPError(exact, 403, "denied", {}, None), None, None, 1),
            ("network", URLError("offline"), None, None, 1),
            ("conflict", resource, HTTPError(exact, 409, "conflict", {}, None), None, 1),
            ("response_uid", resource, wrong, resource, 1),
            ("readback_uid", resource, resource, wrong, 1),
            ("existing_uid", wrong, resource, resource, 1),
        ]
        for label, existing, response, readback, result in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                path = write_dashboard(directory, v2=True)
                original = json.loads(path.read_text(encoding="utf-8"))
                api_get = mock.Mock(side_effect=[
                    # Grafana 12+ reports the PostgreSQL plugin id instead of "postgres".
                    {"uid": RECOVERY_DATASOURCE_UID, "type": "grafana-postgresql-datasource"}, existing, readback])
                api_post = mock.Mock(side_effect=[response])
                with mock.patch.object(restore, "log") as log:
                    self.assertEqual(self.run_restore(path, api_get, api_post), result)
                self.assertEqual(api_get.call_args_list[1].args[1], exact)
                if label in {"denied", "network", "existing_uid"}:
                    api_post.assert_not_called()
                    continue
                api_post.assert_called_once()
                call = api_post.call_args
                self.assertEqual(call.args[1], collection if label == "create" else exact)
                self.assertEqual(call.kwargs["method"], "POST" if label == "create" else "PUT")
                body = call.args[3]
                self.assertEqual(body["apiVersion"], "dashboard.grafana.app/v2")
                self.assertEqual(body["kind"], "Dashboard")
                self.assertEqual(body["metadata"]["name"], uid)
                self.assertEqual(body["metadata"]["annotations"], {"grafana.app/folder": "" if label in {"create", "update_without_version"} else "original-folder"})
                self.assertEqual(body["spec"], {k: v for k, v in original.items() if k not in {"uid", "version", "id"}})
                self.assertEqual(body["metadata"].get("resourceVersion"), None if label in {"create", "update_without_version"} else "rv-1")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
                if result == 0:
                    self.assertEqual(api_get.call_args_list[-1].args[1], exact)
                    self.assertTrue(any("generation=10" in c.args[0] for c in log.call_args_list))

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
