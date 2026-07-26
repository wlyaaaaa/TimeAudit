from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from .paths import DEFAULT_BACKUP_ROOT, runtime_paths


BACKUP_SCHEMA = "timeaudit.clipboard-backup.v1"
BACKUP_FILENAME = "clipboard_history.latest.sqlite3"
MANIFEST_FILENAME = "backup-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_database_facts(path: Path) -> dict[str, object]:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        events = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        blobs = connection.execute("SELECT count(*) FROM blobs").fetchone()[0]
        fts = connection.execute("SELECT count(*) FROM content_fts").fetchone()[0]
        return {
            "integrity": integrity,
            "schema_version": int(schema),
            "event_count": int(events),
            "blob_count": int(blobs),
            "fts_count": int(fts),
        }
    finally:
        connection.close()


def create_backup(source_root: Path, backup_root: Path) -> dict[str, object]:
    source = runtime_paths(source_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / BACKUP_FILENAME
    temporary = backup_root / f".{BACKUP_FILENAME}.{uuid.uuid4().hex}.tmp"
    source_uri = source.database.resolve().as_uri() + "?mode=ro"
    read_connection = sqlite3.connect(source_uri, uri=True, timeout=10)
    write_connection = sqlite3.connect(temporary)
    try:
        read_connection.backup(write_connection)
        write_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        write_connection.close()
        read_connection.close()
    facts = _safe_database_facts(temporary)
    if facts["integrity"] != "ok":
        temporary.unlink(missing_ok=True)
        raise RuntimeError("backup_integrity_failed")
    os.replace(temporary, destination)
    control_destination = backup_root / "control.json"
    if source.control.exists():
        control_temp = backup_root / f".control.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(source.control, control_temp)
        os.replace(control_temp, control_destination)
    manifest = {
        "schema": BACKUP_SCHEMA,
        "created_at_unix_ms": int(time.time() * 1000),
        "database_file": BACKUP_FILENAME,
        "database_sha256": _sha256(destination),
        "database_bytes": destination.stat().st_size,
        **facts,
    }
    manifest_temp = backup_root / f".manifest.{uuid.uuid4().hex}.tmp"
    manifest_temp.write_text(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(manifest_temp, backup_root / MANIFEST_FILENAME)
    return manifest


def verify_backup(backup_root: Path) -> dict[str, object]:
    database = backup_root / BACKUP_FILENAME
    manifest = json.loads(
        (backup_root / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    facts = _safe_database_facts(database)
    valid = (
        manifest.get("schema") == BACKUP_SCHEMA
        and manifest.get("database_sha256") == _sha256(database)
        and manifest.get("database_bytes") == database.stat().st_size
        and facts["integrity"] == "ok"
        and manifest.get("event_count") == facts["event_count"]
        and manifest.get("blob_count") == facts["blob_count"]
        and manifest.get("fts_count") == facts["fts_count"]
    )
    return {"valid": valid, **facts, "database_sha256": _sha256(database)}


def restore_backup(backup_root: Path, target_root: Path) -> dict[str, object]:
    if target_root.exists() and any(target_root.iterdir()):
        raise RuntimeError("restore_target_not_empty")
    verification = verify_backup(backup_root)
    if not verification["valid"]:
        raise RuntimeError("backup_verify_failed")
    target_root.mkdir(parents=True, exist_ok=True)
    target = runtime_paths(target_root)
    temporary = target_root / f".restore.{uuid.uuid4().hex}.tmp"
    shutil.copyfile(backup_root / BACKUP_FILENAME, temporary)
    if _sha256(temporary) != verification["database_sha256"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("restore_copy_mismatch")
    os.replace(temporary, target.database)
    control = backup_root / "control.json"
    if control.exists():
        shutil.copyfile(control, target.control)
    restored = _safe_database_facts(target.database)
    return {
        "valid": restored["integrity"] == "ok",
        **restored,
        "database_sha256": _sha256(target.database),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        required=DEFAULT_BACKUP_ROOT is None,
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--restore-target", type=Path)
    args = parser.parse_args()
    if args.restore_target:
        result = restore_backup(args.backup_root, args.restore_target)
    elif args.verify:
        result = verify_backup(args.backup_root)
    else:
        result = create_backup(runtime_paths(args.source_root).root, args.backup_root)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("valid", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
