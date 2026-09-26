"""Backup failure paths must preserve originals and never label partial files complete."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import pytest
import timeaudit_backup as backup


def make_archive(path):
    path.write_bytes(b"PGDMP" + b"fixture" * 200)
    return path


def fake_export(args, *, stdout, **kwargs):
    assert "pg_dump" in args and "-Fc" in args
    stdout.write(b"PGDMP" + b"fixture" * 200)
    return b""


def test_manifest_is_atomic_and_checks_hash(tmp_path):
    path=make_archive(tmp_path/"time_audit_20260101_120000.dump")
    with patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        result=backup.verify(path,record=True)
        assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert not result["restore_check_verified"]
        assert not list(tmp_path.glob("*.tmp"))
        path.write_bytes(path.read_bytes()+b"changed")
        with pytest.raises(RuntimeError,match="manifest_mismatch"):
            backup.verify(path)


def test_corrupt_or_unbounded_manifest_fails_closed(tmp_path):
    path=make_archive(tmp_path/"time_audit_20260101_120000.dump")
    meta=path.with_suffix(".dump.json")
    with patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        meta.write_text("[]")
        with pytest.raises(RuntimeError,match="manifest_invalid"):
            backup.verify(path)
        meta.write_bytes(b" "*65537)
        with pytest.raises(RuntimeError,match="manifest_too_large"):
            backup.verify(path)


def test_failed_export_preserves_good_archive_and_cleans_owned_candidate(tmp_path):
    good=make_archive(tmp_path/"time_audit_20260101_120000.dump")
    original=good.read_bytes()
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError):backup.backup(tmp_path)
    assert good.read_bytes() == original
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))
    assert list(tmp_path.glob("*.dump")) == [good]


def test_verify_failure_cannot_publish_completed_archive(tmp_path):
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"verify",side_effect=RuntimeError("bad catalog")):
        with pytest.raises(RuntimeError):backup.backup(tmp_path)
    assert not list(tmp_path.glob("*.dump"))
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))


def test_interrupted_publication_is_reconciled_without_touching_unmarked_history(tmp_path):
    orphan = make_archive(tmp_path/"time_audit_20260101_120000.dump")
    pending = make_archive(tmp_path/"time_audit_20260102_120000_abcdef.dump")
    marker = pending.with_suffix(".dump.transaction")
    os.link(pending, marker)
    partial = make_archive(tmp_path/"time_audit_20260103_120000_abcdef.dump.partial")
    partial_marker = tmp_path/"time_audit_20260103_120000_abcdef.dump.transaction"
    os.link(partial, partial_marker)
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        result = backup.backup(tmp_path)
    assert orphan.exists()
    assert not pending.exists() and not marker.exists()
    assert not partial.exists()
    assert not list(tmp_path.glob("*.transaction"))
    assert (tmp_path/(result["archive"]+".json")).exists()


def test_completed_archive_survives_interrupted_marker_cleanup(tmp_path):
    archive = make_archive(tmp_path/"time_audit_20260102_120000_abcdef.dump")
    marker = archive.with_suffix(".dump.transaction")
    os.link(archive, marker)
    with patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        backup.verify(archive, record=True)
        backup._reconcile_transactions(tmp_path, container="unused")
    assert archive.exists()
    assert archive.with_suffix(".dump.json").exists()
    assert not marker.exists()


def test_foreign_final_collision_preserves_foreign_bytes(tmp_path):
    foreign = {}

    def inject_collision(source, destination):
        Path(destination).write_bytes(b"another writer's archive")
        foreign["path"] = Path(destination)
        raise FileExistsError("synthetic collision")

    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7), \
         patch.object(backup.os,"rename",side_effect=inject_collision):
        with pytest.raises(RuntimeError, match="foreign_collision"):
            backup.backup(tmp_path)
    assert foreign["path"].read_bytes() == b"another writer's archive"
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))


def test_crash_recovery_never_removes_foreign_final(tmp_path):
    marker = make_archive(tmp_path/"time_audit_20260102_120000_abcdef.dump.transaction")
    partial = tmp_path/"time_audit_20260102_120000_abcdef.dump.partial"
    os.link(marker, partial)
    foreign_final = tmp_path/"time_audit_20260102_120000_abcdef.dump"
    foreign_final.write_bytes(b"foreign complete archive")
    with pytest.raises(RuntimeError, match="foreign_collision"):
        backup._reconcile_transactions(tmp_path, container="unused")
    assert foreign_final.read_bytes() == b"foreign complete archive"
    assert not marker.exists() and not partial.exists()


def test_crash_before_manifest_replace_cleans_only_owned_manifest_stage(tmp_path):
    marker = make_archive(tmp_path/"time_audit_20260102_120000_abcdef.dump.transaction")
    final = tmp_path/"time_audit_20260102_120000_abcdef.dump"
    os.link(marker, final)
    staged = tmp_path/(final.name+".json."+"a"*32+".tmp")
    staged.write_text('{"incomplete":true}', encoding="utf-8")
    unrelated = tmp_path/("time_audit_other.dump.json."+"b"*32+".tmp")
    unrelated.write_text("unrelated", encoding="utf-8")
    backup._reconcile_transactions(tmp_path, container="unused")
    assert not marker.exists() and not final.exists() and not staged.exists()
    assert unrelated.read_text(encoding="utf-8") == "unrelated"


def test_os_lock_blocks_second_process_without_touching_its_stage(tmp_path):
    code = (
        "import os,sys\nfrom pathlib import Path\nimport timeaudit_backup as b\n"
        "p=Path(sys.argv[1])\n"
        "with b._directory_lock(p):\n"
        " m=p/'time_audit_20260102_120000_abcdef.dump.transaction'\n"
        " m.write_bytes(b'PGDMP'+b'fixture'*200)\n"
        " os.link(m,p/'time_audit_20260102_120000_abcdef.dump.partial')\n"
        " print('ready',flush=True)\n sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(backup.__file__).parent,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="backup_already_running"):
            backup.backup(tmp_path)
        assert list(tmp_path.glob("*.partial"))
        assert list(tmp_path.glob("*.transaction"))
    finally:
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)
    assert child.returncode != 0
    with backup._directory_lock(tmp_path):
        backup._reconcile_transactions(tmp_path, container="unused")
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))


def test_cli_reports_busy_backup_without_claiming_completion(capsys):
    with patch.object(backup, "backup", side_effect=RuntimeError("backup_already_running")):
        assert backup.main(["backup"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "busy"
    assert result["reason"] == "backup_already_running"


def test_manifest_publication_failure_leaves_no_unowned_final(tmp_path):
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7), \
         patch.object(backup,"atomic_json",side_effect=OSError("synthetic write failure")):
        with pytest.raises(OSError):
            backup.backup(tmp_path)
    assert not list(tmp_path.glob("*.dump"))
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))


def test_success_publishes_archive_and_manifest_without_deleting_unverified_originals(tmp_path):
    for i in range(1,5):
        old=make_archive(tmp_path/f"time_audit_2026010{i}_120000.dump")
        os.utime(old,(1,1))
        old.with_suffix(".dump.json").write_text("[]")
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        result=backup.backup(tmp_path,retention_days=1)
    assert result["status"] == "pass" and result["removed_expired_verified_pairs"] == 0
    assert len(list(tmp_path.glob("*.dump"))) == 5
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.transaction"))
    assert (tmp_path/(result["archive"]+".json")).exists()


def test_unmarked_archives_do_not_consume_three_completed_retention_slots(tmp_path):
    valid = []
    with patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        for day in range(1,4):
            path = make_archive(tmp_path/f"time_audit_2026010{day}_120000.dump")
            os.utime(path, (day, day))
            backup.verify(path, record=True)
            valid.append(path)
    unmarked = [
        make_archive(tmp_path/f"time_audit_2026020{day}_120000.dump")
        for day in range(1,4)
    ]
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"image_for",return_value="image"),patch.object(backup,"archive_list",return_value=7):
        result = backup.backup(tmp_path, retention_days=1)
    assert result["removed_expired_verified_pairs"] == 1
    assert not valid[0].exists()
    assert all(path.exists() for path in valid[1:] + unmarked)
    assert (tmp_path/result["archive"]).exists()
    assert len([p for p in tmp_path.glob("*.dump") if backup._completed_manifest_matches(p)]) == 3


def test_bad_parameters_are_rejected_before_export(tmp_path):
    for args in ({"retention_days":0},{"retention_days":-1},{"container":"--help"},{"db_user":"x; rm"}):
        with pytest.raises(ValueError):backup.backup(tmp_path,**args)
    assert not list(tmp_path.iterdir())
