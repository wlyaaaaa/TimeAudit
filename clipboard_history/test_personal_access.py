"""Synthetic B2 state and widget doubles; never open a window or real history."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from concurrent.futures import Future
from unittest import mock

from clipboard_history.personal_access import BROKER, SharedPersonalAccess, permitted
import clipboard_history.personal_access as access_module


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# Use the same installed pure policy as the consumer, only with synthetic state.
if not BROKER.with_name("personal_environment.py").is_file():
    raise unittest.SkipTest("Installed PCConfig B2 policy is required for this integration test")
sys.path.insert(0, str(BROKER.parent))
try:
    policy = load_module("_test_b2_policy", BROKER.with_name("personal_environment.py"))
finally:
    sys.path.pop(0)
with mock.patch.dict(sys.modules, {"clipboard_history.win32_clipboard": types.SimpleNamespace(restore_text=mock.Mock())}):
    viewer_module = load_module("_test_history_viewer", Path(__file__).with_name("viewer.pyw"))


class Variable:
    def __init__(self, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class Widget:
    def __init__(self):
        self.items = {}
        self.text = ""
        self.options = {}
    def configure(self, **kwargs):
        self.options.update(kwargs)
    def get_children(self):
        return tuple(self.items)
    def delete(self, *args):
        self.items.clear()
        self.text = ""
    def insert(self, *args, **kwargs):
        if "iid" in kwargs:
            self.items[kwargs["iid"]] = kwargs["values"]
        else:
            self.text = args[-1]
    def selection(self):
        return tuple(self.items)[:1]


class ImmediateWorker:
    def submit(self, function, *args):
        result = Future()
        try:
            result.set_result(function(*args))
        except Exception as error:
            result.set_exception(error)
        return result


ROW = {"event_id": "synthetic-entry", "observed_at_utc": "2026-09-20T01:00:00Z",
       "payload_type": "text", "observation_kind": "observation", "preview": "synthetic preview"}


class SharedAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="timeaudit-access-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic-b2.json"
        self.clock = [1000, 100000]
        self.actions = []
        self.outcome = "success"
        self.state = policy.new_state()
        self.save()
        def projection(state, **kwargs):
            return policy.public_status(state, **kwargs, now_unix=self.clock[0], now_tick_ms=self.clock[1])
        self.access = SharedPersonalAccess(runner=self.run_broker, policy=types.SimpleNamespace(public_status=projection))
        self.access.initialize()

    def save(self):
        self.path.write_text(json.dumps(self.state), encoding="utf-8")

    def grant(self):
        self.state = policy.grant_verified_environment(self.state, root_task_id="synthetic-viewer",
            expected_generation=self.state["generation"], now_unix=self.clock[0],
            boot_unix=100, grant_minutes=30, now_tick_ms=self.clock[1])
        self.save()

    def run_broker(self, action):
        self.actions.append(action)
        if action == "VerifyPersonalEnvironment":
            if self.outcome == "success":
                self.grant()
                return {"status": "pass"}
            return {"status": "cancelled", "terminal": True}
        return {"schema": policy.STATUS_SCHEMA, "status": "pass", "state_path": str(self.path),
                "boot_time": {"available": True, "unix": 100}}

    def viewer(self):
        view = viewer_module.HistoryViewer.__new__(viewer_module.HistoryViewer)
        view.access = self.access
        view.access_open = False
        view.access_job = None
        view.paths = types.SimpleNamespace(database=Path(self.temp.name) / "never-open-real-history.sqlite3")
        view.history_job = None
        view.access_worker = ImmediateWorker()
        view.rows = {}
        view.offset = 0
        for name in ("query_var", "from_var", "to_var", "status_var", "access_var", "page_var"):
            setattr(view, name, Variable())
        view.type_var = Variable("全部")
        view.restore_var = Variable(True)
        for name in ("tree", "preview", "copy_button", "unlock_button"):
            setattr(view, name, Widget())
        view.after = mock.Mock()
        return view

    def store(self):
        store = mock.Mock()
        store.search.return_value = [dict(ROW)]
        store.get_event.return_value = {"event_id": ROW["event_id"], "text": "synthetic full text"}
        return store

    def test_locked_launch_never_opens_queries_or_fills_history(self):
        view = self.viewer()
        with mock.patch.object(viewer_module, "ReadOnlyClipboardStore") as factory:
            view.refresh()
            view._select()
            view._restore_selected()
            view._check_access()
            factory.assert_not_called()
        self.assertEqual(view.rows, {})
        self.assertEqual(view.preview.text, "")
        self.assertEqual(self.actions, ["StatusPersonalEnvironment"])

    def test_existing_shared_period_displays_without_verifying_or_renewing(self):
        self.grant()
        before = self.path.read_bytes()
        view, store = self.viewer(), self.store()
        with mock.patch.object(viewer_module, "ReadOnlyClipboardStore", return_value=store):
            view._check_access()
            view._select()
            view._finish_history()
            self.access.unlock()
        self.assertEqual(len(view.tree.items), 1)
        self.assertEqual(view.preview.text, "synthetic full text")
        self.assertEqual(self.actions, ["StatusPersonalEnvironment"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_four_choice_success_requires_fresh_shared_status(self):
        result = self.access.unlock()
        self.assertTrue(permitted(result["shared_status"]))
        self.assertEqual(self.actions, ["StatusPersonalEnvironment", "VerifyPersonalEnvironment", "StatusPersonalEnvironment"])
        self.assertEqual(result["shared_status"]["privacy_access"]["expires_at_unix"], self.state["grant"]["expires_unix"])

    def test_unlock_command_uses_canonical_selector_without_new_duration_or_factor(self):
        with mock.patch("clipboard_history.personal_access.subprocess.run",
                        return_value=types.SimpleNamespace(stdout='{"status":"cancelled"}')) as run:
            self.access._run("VerifyPersonalEnvironment")
        command = run.call_args.args[0]
        self.assertIn(str(BROKER), command)
        self.assertIn("VerifyPersonalEnvironment", command)
        self.assertIn("-PromptId", command)
        self.assertNotIn("-AuthorityFactor", command)
        self.assertNotIn("-GrantMinutes", command)
        self.assertNotIn("-PrivacyGrantHours", command)

    def test_cancel_does_not_lock_renew_or_retry(self):
        self.outcome = "cancel"
        before = self.path.read_bytes()
        result = self.access.unlock()
        self.assertFalse(permitted(result["shared_status"]))
        for _ in range(4):
            self.access.status()
        self.assertEqual(self.actions.count("VerifyPersonalEnvironment"), 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_expiry_and_explicit_lock_clear_every_view_and_close_store(self):
        for cause in ("expiry", "lock", "unknown", "closing"):
            with self.subTest(cause=cause):
                self.state = policy.new_state()
                self.grant()
                view, store = self.viewer(), self.store()
                with mock.patch.object(viewer_module, "ReadOnlyClipboardStore", return_value=store):
                    view.refresh()
                    view._finish_history()
                    view._select()
                    view._finish_history()
                view.query_var.set("synthetic private search")
                if cause == "expiry":
                    self.clock[0] = self.state["grant"]["expires_unix"]
                    self.clock[1] = self.state["grant"]["expires_tick_ms"]
                elif cause == "lock":
                    self.state = policy.new_state()
                    self.save()
                elif cause == "closing":
                    self.state["phase"] = "closing"
                    self.save()
                else:
                    self.path.write_text("invalid state", encoding="utf-8")
                view._check_access()
                self.assertFalse(view.tree.items)
                self.assertFalse(view.rows)
                self.assertEqual(view.preview.text, "")
                self.assertEqual(view.query_var.get(), "")
                self.assertIsNone(view.history_job)
                self.assertGreaterEqual(store.close.call_count, 1)
                self.assertEqual(view._copy_preview_guard(), "break")

    def test_late_search_result_cannot_reopen_after_lock(self):
        self.grant()
        view, store = self.viewer(), self.store()
        def late_rows(**kwargs):
            self.state = policy.new_state()
            self.save()
            return [dict(ROW)]
        store.search.side_effect = late_rows
        with mock.patch.object(viewer_module, "ReadOnlyClipboardStore", return_value=store):
            view.refresh()
            view._finish_history()
        self.assertFalse(view.tree.items)
        self.assertFalse(view.rows)


    def test_copy_rechecks_after_detail_read(self):
        self.grant()
        view, store = self.viewer(), self.store()
        with mock.patch.object(viewer_module, "ReadOnlyClipboardStore", return_value=store):
            view.refresh()
            view._finish_history()
        def late_detail(event_id):
            self.state = policy.new_state()
            self.save()
            return {"event_id": event_id, "text": "synthetic late result"}
        store.get_event.side_effect = late_detail
        with mock.patch.object(viewer_module, "restore_text") as copy:
            view._restore_selected()
            view._finish_history()
            copy.assert_not_called()

    def test_pending_query_cannot_delay_clear_or_refill_on_late_completion(self):
        self.grant()
        view = self.viewer()
        view._show_rows([dict(ROW)])
        view._set_preview("synthetic content already shown")
        future = Future()
        future.set_running_or_notify_cancel()
        view.history_job = ("search", view._lease_identity(), future)
        self.clock[0] = self.state["grant"]["expires_unix"]
        self.clock[1] = self.state["grant"]["expires_tick_ms"]
        view._check_access()
        self.assertFalse(view.tree.items)
        self.assertEqual(view.preview.text, "")
        future.set_result([dict(ROW)])
        view._finish_history()
        self.assertFalse(view.rows)


class PolicyReloadTests(unittest.TestCase):
    @staticmethod
    def policy_source(version):
        return (
            "def public_status(state, **kwargs):\n"
            f"    allowed = state.get('schema') == 'test-state-{version}' "
            "and state.get('phase') == 'unlocked'\n"
            "    return {'schema': 'pcconfig.personal-environment-status.v3', "
            "'status': 'pass', 'data_state': 'unlocked' if allowed else 'locked', "
            "'lock_generation': 1, 'privacy_access': "
            "{'status': 'pass' if allowed else 'blocked', "
            "'expires_at_unix': 4102444800 if allowed else 0}}\n"
        )

    def test_installed_policy_change_revalidates_new_state_and_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="timeaudit-policy-reload-") as temp:
            root = Path(temp)
            broker = root / "Invoke-SecretBroker.ps1"
            policy_file = broker.with_name("personal_environment.py")
            state_file = root / "state.json"
            policy_file.write_text(self.policy_source("v1"), encoding="utf-8")
            state_file.write_text(json.dumps({"schema": "test-state-v1", "phase": "unlocked"}),
                                  encoding="utf-8")

            def runner(action):
                self.assertEqual(action, "StatusPersonalEnvironment")
                return {"schema": "pcconfig.personal-environment-status.v3",
                        "status": "pass", "state_path": str(state_file),
                        "boot_time": {"available": True, "unix": 100}}

            with mock.patch.object(access_module, "BROKER", broker):
                access = SharedPersonalAccess(runner=runner)
                self.assertTrue(permitted(access.initialize()))
                state_file.write_text(json.dumps({"schema": "test-state-v2", "phase": "unlocked"}),
                                      encoding="utf-8")
                self.assertFalse(permitted(access.status()))
                old_stat = policy_file.stat()
                policy_file.write_text(self.policy_source("v2"), encoding="utf-8")
                self.assertEqual(policy_file.stat().st_size, old_stat.st_size)
                os.utime(policy_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
                self.assertTrue(permitted(access.status()))
                state_file.write_text(json.dumps({"schema": "test-state-v2", "phase": "locked"}),
                                      encoding="utf-8")
                self.assertFalse(permitted(access.status()))
                state_file.write_text(json.dumps({"schema": "test-state-v2", "phase": "unlocked"}),
                                      encoding="utf-8")
                policy_file.write_text("invalid python (", encoding="utf-8")
                self.assertFalse(permitted(access.status()))
                policy_file.unlink()
                self.assertFalse(permitted(access.status()))
                policy_file.write_text(self.policy_source("v2"), encoding="utf-8")
                self.assertTrue(permitted(access.status()))

    def test_injected_policy_and_runner_remain_supported(self):
        with tempfile.TemporaryDirectory(prefix="timeaudit-policy-injected-") as temp:
            root = Path(temp)
            state_file = root / "state.json"
            state_file.write_text("{}", encoding="utf-8")
            injected = types.SimpleNamespace(public_status=lambda state, **kwargs: {
                "schema": "pcconfig.personal-environment-status.v3", "status": "pass",
                "data_state": "unlocked", "lock_generation": 1,
                "privacy_access": {"status": "pass", "expires_at_unix": 4102444800},
            })
            runner = lambda action: {
                "schema": "pcconfig.personal-environment-status.v3", "status": "pass",
                "state_path": str(state_file), "boot_time": {"available": True, "unix": 100},
            }
            with mock.patch.object(access_module, "BROKER", root / "missing-broker.ps1"):
                access = SharedPersonalAccess(runner=runner, policy=injected)
                self.assertTrue(permitted(access.initialize()))

if __name__ == "__main__":
    unittest.main()
