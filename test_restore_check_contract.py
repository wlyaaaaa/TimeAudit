"""Resumable restore state, process ownership and exit-code regressions."""
import json
from pathlib import Path
from unittest.mock import patch
import pytest
import timeaudit_restore_check as restore


def job():
    return dict(schema="timeaudit.restore-check-state.v1", container="timeaudit-restorecheck-"+"a"*16, job_id="a"*16, status="running", cleanup_complete=False)


def test_all_worker_exit_codes_remain_actionable():
    for code, expected in (("running","running"),("starting","starting"),("0","ready_to_verify"),("1","failed"),("137","failed"),("124","failed")):
        with patch.object(restore,"state",return_value=job()), patch.object(restore,"assert_owned"), patch.object(restore,"docker_path",return_value="docker"), patch.object(restore,"command",side_effect=[b"id",code.encode()]):
            assert restore.status()["status"] == expected


def test_missing_container_does_not_finalize_a_racing_start():
    with patch.object(restore,"state",return_value={**job(),"status":"starting"}), patch.object(restore,"docker_path",return_value="docker"), patch.object(restore,"command",return_value=b""), patch.object(restore,"save") as save:
        assert restore.status()["status"] == "starting"
        save.assert_not_called()


def test_foreign_container_labels_are_never_accepted():
    for labels in (None,[],{}, {"timeaudit.restore_id":"foreign","timeaudit.purpose":"isolated-restore-check"}):
        with patch.object(restore,"docker_path",return_value="docker"), patch.object(restore,"command",return_value=json.dumps(labels).encode()):
            with pytest.raises(RuntimeError,match="identity_mismatch"):
                restore.assert_owned(job())


def test_journal_rejects_unknown_container_and_non_object(tmp_path):
    path=tmp_path/"state.json"
    with patch.object(restore,"STATE_PATH",path):
        for payload in ([],None,{}, {**job(),"container":"audit-postgres"}):
            path.write_text(json.dumps(payload))
            with pytest.raises(RuntimeError,match="state_invalid"):
                restore.state()


def test_transition_lock_is_released_after_exception(tmp_path):
    with patch.object(restore,"STATE_PATH",tmp_path/"state.json"):
        with pytest.raises(ValueError):
            with restore.journal_lock():
                raise ValueError("fixture")
        with restore.journal_lock():
            pass


def test_finish_of_running_job_cannot_claim_pass_or_remove_container(tmp_path):
    with patch.object(restore,"STATE_PATH",tmp_path/"state.json"), patch.object(restore,"state",return_value=job()), patch.object(restore,"status",return_value={"status":"running"}), patch.object(restore,"command") as command:
        assert restore.finish()["status"] == "running"
        command.assert_not_called()
