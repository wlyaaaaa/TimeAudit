"""Backup failure paths must preserve originals and never label partial files complete."""
import hashlib
import json
import os
from pathlib import Path
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


def test_failed_export_preserves_good_archive_and_marks_incomplete(tmp_path):
    good=make_archive(tmp_path/"time_audit_20260101_120000.dump")
    original=good.read_bytes()
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError):backup.backup(tmp_path)
    assert good.read_bytes() == original
    assert list(tmp_path.glob("*.partial"))
    assert list(tmp_path.glob("*.dump")) == [good]


def test_verify_failure_cannot_publish_completed_archive(tmp_path):
    with patch.object(backup,"docker_path",return_value="docker"),patch.object(backup,"command",side_effect=fake_export), \
         patch.object(backup,"verify",side_effect=RuntimeError("bad catalog")):
        with pytest.raises(RuntimeError):backup.backup(tmp_path)
    assert not list(tmp_path.glob("*.dump"))
    assert list(tmp_path.glob("*.partial"))


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
    assert (tmp_path/(result["archive"]+".json")).exists()


def test_bad_parameters_are_rejected_before_export(tmp_path):
    for args in ({"retention_days":0},{"retention_days":-1},{"container":"--help"},{"db_user":"x; rm"}):
        with pytest.raises(ValueError):backup.backup(tmp_path,**args)
    assert not list(tmp_path.iterdir())