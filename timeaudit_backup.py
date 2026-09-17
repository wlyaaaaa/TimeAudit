"""Atomic PostgreSQL archives, integrity manifests and isolated restore checks."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

DEFAULT_BACKUP_DIR = Path(r"G:\80_Backup\TimeAudit\postgresql")
NAME = re.compile(r"^time_audit_\d{8}_\d{6}(?:_[a-f0-9]{6})?\.dump$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def command(args, *, timeout=30, stdout=subprocess.PIPE):
    try:
        result = subprocess.run(args, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("backup_command_unavailable_or_timed_out") from None
    if result.returncode:
        raise RuntimeError("backup_command_failed")
    return result.stdout or b""


def docker_path():
    path = shutil.which("docker.exe") or shutil.which("docker")
    if not path:
        raise RuntimeError("docker_unavailable")
    return path


def image_for(container):
    if not IDENTIFIER.fullmatch(container):
        raise ValueError("invalid_container")
    image = command([docker_path(), "inspect", "--format", "{{.Image}}", container], timeout=5).decode("ascii").strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise RuntimeError("container_image_identity_invalid")
    return image


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive_list(path, *, image):
    # Mount an immutable archive into a short-lived existing-image container.
    # No network, ports, credentials, live database writes or image pulls.
    args = [docker_path(), "run", "--rm", "--pull=never", "--network", "none", "--cpus", "0.5", "--memory", "256m", "--pids-limit", "64", "--mount", f"type=bind,source={path.parent},target=/backup,readonly", "--entrypoint", "pg_restore", image, "--list", f"/backup/{path.name}"]
    raw = command(args, timeout=30)
    if len(raw) > 8388608:
        raise RuntimeError("archive_catalog_too_large")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    entries = [line for line in lines if re.match(r"^\d+;", line)]
    if not entries or not any(" TABLE public fact_system_hardware " in line for line in entries):
        raise RuntimeError("archive_missing_required_table")
    return len(entries)


def verify(path: Path, *, container="audit-postgres", record=False):
    path = path.resolve(strict=True)
    before = path.stat()
    if before.st_size < 1024:
        raise RuntimeError("archive_too_small")
    with path.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise RuntimeError("archive_header_invalid")
    image = image_for(container)
    entries = archive_list(path, image=image)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8388608):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("archive_changed_during_verification")
    manifest_path = path.with_suffix(path.suffix + ".json")
    previous = None
    if manifest_path.exists():
        if manifest_path.stat().st_size > 65536:
            raise RuntimeError("archive_manifest_too_large")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise RuntimeError("archive_manifest_invalid")
        if previous.get("schema") != "timeaudit.backup-manifest.v1" or previous.get("sha256") != digest.hexdigest() or previous.get("bytes") != after.st_size:
            raise RuntimeError("archive_manifest_mismatch")
    value = {"schema": "timeaudit.backup-manifest.v1", "bytes": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest(), "archive_list_verified": True, "catalog_entries": entries, "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "restore_check_verified": bool(previous and previous.get("restore_check_verified")), "verification_scope": "archive_catalog_and_sha256"}
    if previous and previous.get("restore_check_verified"):
        for key in ("restore_checked_at_utc", "restore_counts", "restore_checks"):
            if key in previous:
                value[key] = previous[key]
    if record:
        if not NAME.fullmatch(path.name):
            raise ValueError("record_requires_completed_owned_archive")
        atomic_json(manifest_path, value)
    return value


def backup(directory: Path, *, container="audit-postgres", db_user="leyang", db_name="time_audit", retention_days=14):
    if not all(IDENTIFIER.fullmatch(v) for v in (container, db_user, db_name)) or not 1 <= retention_days <= 3650:
        raise ValueError("invalid_backup_parameters")
    directory.mkdir(parents=True, exist_ok=True)
    filename = "time_audit_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6] + ".dump"
    final = directory / filename
    partial = final.with_suffix(final.suffix + ".partial")
    # A failed/aborted export is visibly incomplete and never replaces a good archive.
    with partial.open("xb") as stream:
        command([docker_path(), "exec", container, "pg_dump", "-U", db_user, "-d", db_name, "-Fc"], timeout=3600, stdout=stream)
        stream.flush()
        os.fsync(stream.fileno())
    result = verify(partial, container=container)
    os.rename(partial, final)
    atomic_json(final.with_suffix(final.suffix + ".json"), result)
    cutoff = time.time() - retention_days * 86400
    completed = sorted((p for p in directory.glob("time_audit_*.dump") if NAME.fullmatch(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    # Preserve at least three completed archives. Only this tool's old, verified
    # pairs are eligible; historical unverified originals are not silently deleted.
    for path in completed[3:]:
        manifest = path.with_suffix(path.suffix + ".json")
        if path.stat().st_mtime < cutoff and manifest.exists():
            try:
                if manifest.stat().st_size > 65536:
                    continue
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("schema") == "timeaudit.backup-manifest.v1" and meta.get("bytes") == path.stat().st_size and meta.get("archive_list_verified") is True:
                path.unlink()
                manifest.unlink()
                removed += 1
    return {"status": "pass", "archive": final.name, "bytes": result["bytes"], "archive_list_verified": True, "sha256_recorded": True, "removed_expired_verified_pairs": removed}


def restore_check(path: Path, *, container="audit-postgres"):
    from timeaudit_restore_check import start
    return start(path, container=container)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "verify", "restore-check", "restore-status", "finish-restore"))
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--container", default="audit-postgres")
    parser.add_argument("--db-user", default="leyang")
    parser.add_argument("--db-name", default="time_audit")
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mode == "backup":
            value = backup(args.backup_dir, container=args.container, db_user=args.db_user, db_name=args.db_name, retention_days=args.retention_days)
        elif args.mode in {"restore-status", "finish-restore"}:
            from timeaudit_restore_check import status, finish
            value = status() if args.mode == "restore-status" else finish()
        else:
            path = args.archive
            if path is None:
                paths = [p for p in args.backup_dir.glob("time_audit_*.dump") if NAME.fullmatch(p.name)]
                if not paths:
                    raise RuntimeError("no_completed_archive")
                path = max(paths, key=lambda p: p.stat().st_mtime)
            if args.mode == "verify":
                verified = verify(path, container=args.container, record=args.record)
                value = {"status": "pass", "archive_list_verified": True, "sha256_verified": True, "bytes": verified["bytes"], "catalog_entries": verified["catalog_entries"], "manifest_recorded": args.record, "full_restore_verified": verified["restore_check_verified"]}
            else:
                value = restore_check(path, container=args.container)
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        return 1 if value.get("status") == "failed" else 0
    except (RuntimeError, ValueError, OSError):
        # Never forward database stderr or archive/catalog contents into logs.
        print(json.dumps({"status": "failed", "mode": args.mode, "reason": "backup_or_verification_failed", "completed_archives_preserved": True}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
