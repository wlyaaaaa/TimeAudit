# -*- coding: utf-8 -*-
import contextlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import backup_grafana as backup


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def configure_identity(repo):
    run_git(repo, "config", "user.name", "Backup Test")
    run_git(repo, "config", "user.email", "backup-test@example.invalid")


def commit_file(repo, relative_path, content):
    path = os.path.join(repo, relative_path)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    run_git(repo, "add", "--", relative_path)
    run_git(repo, "commit", "-m", f"test {relative_path}")


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="timeaudit-git-sync-")
        self.original_root = backup.ROOT
        self.original_dash_dir = backup.DASH_DIR

    def tearDown(self):
        backup.ROOT = self.original_root
        backup.DASH_DIR = self.original_dash_dir
        self.temp.cleanup()

    def use_backup_checkout(self, repo):
        backup.ROOT = repo
        backup.DASH_DIR = os.path.join(repo, "grafana_dashboards")

    def bare_topology(self, name):
        base = os.path.join(self.temp.name, name)
        seed = os.path.join(base, "seed")
        remote = os.path.join(base, "remote.git")
        local = os.path.join(base, "local")
        peer = os.path.join(base, "peer")
        os.makedirs(seed)
        run_git(seed, "init", "--quiet")
        configure_identity(seed)
        commit_file(seed, "seed.txt", "seed\n")
        run_git(seed, "branch", "-M", "main")
        run_git(seed, "init", "--bare", "--quiet", remote)
        run_git(seed, "remote", "add", "origin", remote)
        run_git(seed, "push", "--quiet", "-u", "origin", "main")
        run_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        run_git(base, "clone", "--quiet", remote, local)
        run_git(base, "clone", "--quiet", remote, peer)
        configure_identity(local)
        configure_identity(peer)
        return remote, local, peer

    def non_bare_topology(self, name):
        base = os.path.join(self.temp.name, name)
        remote = os.path.join(base, "remote")
        local = os.path.join(base, "local")
        os.makedirs(remote)
        run_git(remote, "init", "--quiet")
        configure_identity(remote)
        commit_file(remote, "seed.txt", "seed\n")
        run_git(remote, "branch", "-M", "main")
        run_git(remote, "config", "receive.denyCurrentBranch", "refuse")
        run_git(base, "clone", "--quiet", remote, local)
        configure_identity(local)
        return remote, local

    def test_dashboard_allowlist_commit_push_and_remote_verification(self):
        remote, local, _ = self.bare_topology("allowlist")
        self.use_backup_checkout(local)
        commit_file(local, "unrelated.txt", "must remain local-only\n")
        # Undo only the unrelated commit while retaining its staged content.
        run_git(local, "reset", "--soft", "HEAD~1")
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        with open(os.path.join(dashboards, "main.json"), "w", encoding="utf-8") as handle:
            handle.write('{"title": "main"}\n')

        backup.git_commit_and_push(
            do_push=True,
            dashboard_paths=["grafana_dashboards/main.json"],
        )

        local_oid = run_git(local, "rev-parse", "HEAD").stdout.strip()
        remote_oid = run_git(remote, "rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(local_oid, remote_oid)
        staged = run_git(local, "diff", "--cached", "--name-only").stdout.splitlines()
        self.assertEqual(staged, ["unrelated.txt"])
        remote_files = run_git(remote, "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
        self.assertIn("grafana_dashboards/main.json", remote_files)
        self.assertNotIn("unrelated.txt", remote_files)

    def test_clean_worktree_still_pushes_prior_ahead_commit(self):
        remote, local, _ = self.bare_topology("ahead")
        commit_file(local, "grafana_dashboards/a.json", "{}\n")
        self.use_backup_checkout(local)

        backup.git_commit_and_push(do_push=True, dashboard_paths=[])

        self.assertEqual(
            run_git(local, "rev-parse", "HEAD").stdout.strip(),
            run_git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        )

    def test_behind_and_diverged_are_blocked(self):
        _, local, peer = self.bare_topology("behind")
        commit_file(peer, "remote.txt", "remote\n")
        run_git(peer, "push", "--quiet", "origin", "main")
        self.use_backup_checkout(local)
        with self.assertRaisesRegex(backup.GitSyncError, r"\bbehind\b"):
            backup.git_commit_and_push(do_push=True, dashboard_paths=[])

        _, local, peer = self.bare_topology("diverged")
        commit_file(local, "local.txt", "local\n")
        commit_file(peer, "remote.txt", "remote\n")
        run_git(peer, "push", "--quiet", "origin", "main")
        self.use_backup_checkout(local)
        with self.assertRaisesRegex(backup.GitSyncError, r"\bdiverged\b"):
            backup.git_commit_and_push(do_push=True, dashboard_paths=[])

    def test_push_failure_propagates(self):
        _, local = self.non_bare_topology("push-failure")
        commit_file(local, "grafana_dashboards/a.json", "{}\n")
        self.use_backup_checkout(local)
        with self.assertRaisesRegex(backup.GitSyncError, r"git push .* failed with exit code"):
            backup.git_commit_and_push(do_push=True, dashboard_paths=[])

    def test_remote_oid_mismatch_fails(self):
        _, local, _ = self.bare_topology("mismatch")
        commit_file(local, "grafana_dashboards/a.json", "{}\n")
        self.use_backup_checkout(local)
        with self.assertRaisesRegex(backup.GitSyncError, "remote OID mismatch"):
            backup.assert_fresh_remote_oid("origin", "main")

    def test_main_returns_nonzero_when_git_cloud_backup_fails(self):
        with (
            mock.patch.object(
                backup,
                "grafana_backup_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(backup, "assert_dashboard_worktree_clean"),
            mock.patch.object(backup, "dashboard_json_paths", return_value=set()),
            mock.patch.object(backup, "export_dashboards_from_db", return_value=set()),
            mock.patch.object(backup, "backup_grafana_db", return_value=None),
            mock.patch.object(
                backup,
                "git_commit_and_push",
                side_effect=backup.GitSyncError("push rejected"),
            ),
            mock.patch("sys.argv", ["backup_grafana.py"]),
        ):
            self.assertEqual(backup.main(), 1)

    def test_main_refuses_dirty_dashboard_tree_before_any_export(self):
        with (
            mock.patch.object(
                backup,
                "grafana_backup_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                backup,
                "assert_dashboard_worktree_clean",
                side_effect=backup.GitSyncError("dashboard tree is dirty"),
            ),
            mock.patch.object(backup, "export_dashboards_from_db") as export,
            mock.patch.object(backup, "backup_grafana_db") as binary_backup,
            mock.patch.object(backup, "git_commit_and_push") as sync,
            mock.patch("sys.argv", ["backup_grafana.py"]),
        ):
            self.assertEqual(backup.main(), 1)
        export.assert_not_called()
        binary_backup.assert_not_called()
        sync.assert_not_called()

    def test_main_holds_the_backup_lock_during_export(self):
        with (
            mock.patch.object(
                backup,
                "grafana_backup_lock",
                return_value=contextlib.nullcontext(),
            ) as lock,
            mock.patch.object(backup, "assert_dashboard_worktree_clean"),
            mock.patch.object(backup, "dashboard_json_paths", return_value=set()),
            mock.patch.object(backup, "export_dashboards_from_db", return_value=set()) as export,
            mock.patch.object(backup, "backup_grafana_db"),
            mock.patch("sys.argv", ["backup_grafana.py", "--no-git"]),
        ):
            self.assertEqual(backup.main(), 0)

        lock.assert_called_once_with()
        export.assert_called_once()

    def test_dirty_tracked_untracked_deleted_and_non_json_paths_fail_closed(self):
        _, local, _ = self.bare_topology("dirty-dashboard-tree")
        self.use_backup_checkout(local)
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        commit_file(local, "grafana_dashboards/tracked.json", "{}\n")
        commit_file(local, ".gitignore", "grafana_dashboards/ignored.json\n")

        def write_dashboard_file(name, content):
            with open(os.path.join(dashboards, name), "w", encoding="utf-8") as handle:
                handle.write(content)

        cases = {
            "modified": lambda: write_dashboard_file("tracked.json", '{"changed": true}\n'),
            "staged": lambda: (
                write_dashboard_file("tracked.json", '{"changed": true}\n'),
                run_git(local, "add", "--", "grafana_dashboards/tracked.json"),
            ),
            "untracked-json": lambda: write_dashboard_file("new.json", "{}\n"),
            "untracked-non-json": lambda: write_dashboard_file("notes.txt", "do not publish\n"),
            "ignored-json": lambda: write_dashboard_file("ignored.json", "{}\n"),
            "deleted": lambda: os.remove(os.path.join(dashboards, "tracked.json")),
        }

        for label, mutate in cases.items():
            with self.subTest(label=label):
                run_git(local, "reset", "--hard", "HEAD")
                for name in ("new.json", "notes.txt", "ignored.json"):
                    path = os.path.join(dashboards, name)
                    if os.path.exists(path):
                        os.remove(path)
                mutate()
                with self.assertRaisesRegex(
                    backup.GitSyncError,
                    "uncommitted dashboard changes",
                ):
                    backup.assert_dashboard_worktree_clean()

    def test_writer_rechecks_clean_dashboard_tree_before_deletion(self):
        _, local, _ = self.bare_topology("writer-recheck")
        self.use_backup_checkout(local)
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        commit_file(local, "grafana_dashboards/old.json", "{}\n")
        with open(os.path.join(dashboards, "old.json"), "w", encoding="utf-8") as handle:
            handle.write('{"manual": true}\n')

        with self.assertRaisesRegex(
            backup.GitSyncError,
            "uncommitted dashboard changes",
        ):
            backup.write_dashboard_documents([])

        self.assertTrue(os.path.exists(os.path.join(dashboards, "old.json")))

    def test_post_export_changes_must_match_the_writer_allowlist(self):
        _, local, _ = self.bare_topology("post-export-allowlist")
        self.use_backup_checkout(local)
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        commit_file(local, "grafana_dashboards/old.json", "{}\n")
        changed_paths = set()

        backup.write_dashboard_documents(
            [("new", {"uid": "new", "title": "New dashboard", "panels": []})],
            changed_paths=changed_paths,
        )

        self.assertEqual(
            changed_paths,
            {
                "grafana_dashboards/new__New dashboard.json",
                "grafana_dashboards/old.json",
            },
        )
        self.assertEqual(
            backup.assert_dashboard_change_allowlist(changed_paths),
            changed_paths,
        )

        with open(os.path.join(dashboards, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("manual notes\n")
        with self.assertRaisesRegex(backup.GitSyncError, "only grafana_dashboards/\\*.json"):
            backup.assert_dashboard_change_allowlist(changed_paths)

    def test_git_sync_stages_only_explicit_dashboard_paths(self):
        _, local, _ = self.bare_topology("precise-dashboard-stage")
        self.use_backup_checkout(local)
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        commit_file(local, "grafana_dashboards/old.json", "{}\n")

        os.remove(os.path.join(dashboards, "old.json"))
        with open(os.path.join(dashboards, "new.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(dashboards, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("must remain untracked\n")

        backup.git_commit_and_push(
            do_push=False,
            dashboard_paths=[
                "grafana_dashboards/old.json",
                "grafana_dashboards/new.json",
            ],
        )

        committed = run_git(
            local,
            "show",
            "--no-renames",
            "--name-only",
            "--format=",
        ).stdout.splitlines()
        self.assertEqual(
            sorted(path for path in committed if path),
            ["grafana_dashboards/new.json", "grafana_dashboards/old.json"],
        )
        status = run_git(local, "status", "--short").stdout.splitlines()
        self.assertEqual(status, ["?? grafana_dashboards/notes.txt"])

    def test_git_disables_hidden_interactive_credentials(self):
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("backup_grafana.subprocess.run", return_value=completed) as run:
            backup.git(["status"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "Never")

    def test_network_retry_is_bounded_and_transport_only(self):
        failed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="TLS connect error"
        )
        passed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )
        with (
            mock.patch("backup_grafana.git", side_effect=[failed, passed]) as run,
            mock.patch("backup_grafana.time.sleep") as sleep,
        ):
            result = backup.git_network(["fetch", "origin"], delays=(1,))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)

        rejected = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout="",
            stderr="non-fast-forward update rejected",
        )
        with (
            mock.patch("backup_grafana.git", return_value=rejected) as run,
            mock.patch("backup_grafana.time.sleep") as sleep,
            self.assertRaises(backup.GitSyncError),
        ):
            backup.git_network(["push", "origin"], delays=(1, 2))
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
