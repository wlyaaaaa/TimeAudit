# -*- coding: utf-8 -*-
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

    def tearDown(self):
        backup.ROOT = self.original_root
        self.temp.cleanup()

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
        backup.ROOT = local
        commit_file(local, "unrelated.txt", "must remain local-only\n")
        # Undo only the unrelated commit while retaining its staged content.
        run_git(local, "reset", "--soft", "HEAD~1")
        dashboards = os.path.join(local, "grafana_dashboards")
        os.makedirs(dashboards)
        with open(os.path.join(dashboards, "main.json"), "w", encoding="utf-8") as handle:
            handle.write('{"title": "main"}\n')

        backup.git_commit_and_push(do_push=True)

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
        backup.ROOT = local

        backup.git_commit_and_push(do_push=True)

        self.assertEqual(
            run_git(local, "rev-parse", "HEAD").stdout.strip(),
            run_git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        )

    def test_behind_and_diverged_are_blocked(self):
        _, local, peer = self.bare_topology("behind")
        commit_file(peer, "remote.txt", "remote\n")
        run_git(peer, "push", "--quiet", "origin", "main")
        backup.ROOT = local
        with self.assertRaisesRegex(backup.GitSyncError, r"\bbehind\b"):
            backup.git_commit_and_push(do_push=True)

        _, local, peer = self.bare_topology("diverged")
        commit_file(local, "local.txt", "local\n")
        commit_file(peer, "remote.txt", "remote\n")
        run_git(peer, "push", "--quiet", "origin", "main")
        backup.ROOT = local
        with self.assertRaisesRegex(backup.GitSyncError, r"\bdiverged\b"):
            backup.git_commit_and_push(do_push=True)

    def test_push_failure_propagates(self):
        _, local = self.non_bare_topology("push-failure")
        commit_file(local, "grafana_dashboards/a.json", "{}\n")
        backup.ROOT = local
        with self.assertRaisesRegex(backup.GitSyncError, r"git push .* failed with exit code"):
            backup.git_commit_and_push(do_push=True)

    def test_remote_oid_mismatch_fails(self):
        _, local, _ = self.bare_topology("mismatch")
        commit_file(local, "grafana_dashboards/a.json", "{}\n")
        backup.ROOT = local
        with self.assertRaisesRegex(backup.GitSyncError, "remote OID mismatch"):
            backup.assert_fresh_remote_oid("origin", "main")

    def test_main_returns_nonzero_when_git_cloud_backup_fails(self):
        with (
            mock.patch.object(backup, "export_dashboards", return_value=set()),
            mock.patch.object(backup, "backup_grafana_db", return_value=None),
            mock.patch.object(
                backup,
                "git_commit_and_push",
                side_effect=backup.GitSyncError("push rejected"),
            ),
            mock.patch("sys.argv", ["backup_grafana.py"]),
        ):
            self.assertEqual(backup.main(), 1)

    def test_git_disables_hidden_interactive_credentials(self):
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )
        with mock.patch("backup_grafana.subprocess.run", return_value=completed) as run:
            backup.git(["status"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "Never")


if __name__ == "__main__":
    unittest.main()
