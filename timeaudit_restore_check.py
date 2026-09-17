"""Resumable isolated restore verification; completion survives a lost client."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import re
import time
import uuid
import functools
import contextlib
import os
from timeaudit_backup import NAME, atomic_json, command, docker_path, image_for, verify

STATE_PATH = Path(__file__).resolve().parent / "log" / "restore_check.json"


@contextlib.contextmanager
def journal_lock():
    # OS-released byte lock serializes this owner's start/finish transitions.
    # The harmless lock file can remain; no stale PID file is trusted.
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("restore_transition_busy") from None
            try:
                yield
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("restore_transition_busy") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def serialized(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
        with journal_lock():
            return function(*args, **kwargs)
    return invoke


def state():
    if not STATE_PATH.exists() or STATE_PATH.stat().st_size > 65536:
        raise RuntimeError("restore_state_unavailable")
    value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("restore_state_invalid")
    if value.get("schema") != "timeaudit.restore-check-state.v1" or not re.fullmatch(r"timeaudit-restorecheck-[a-f0-9]{16}", value.get("container", "")):
        raise RuntimeError("restore_state_invalid")
    return value


def save(value):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(STATE_PATH, value)


def assert_owned(value):
    raw = command([docker_path(), "inspect", "--format", '{{json .Config.Labels}}', value["container"]], timeout=5)
    labels = json.loads(raw)
    if not isinstance(labels, dict):
        raise RuntimeError("restore_container_identity_mismatch")
    if not isinstance(labels, dict) or labels.get("timeaudit.restore_id") != value["job_id"] or labels.get("timeaudit.purpose") != "isolated-restore-check":
        raise RuntimeError("restore_container_identity_mismatch")


def status():
    value = state()
    if value.get("cleanup_complete"):
        return {"status": value["status"], "scope": "full_isolated_restore", "cleanup_complete": True, "live_database_modified": False}
    found = command([docker_path(), "ps", "-aq", "--filter", "name=^/" + value["container"] + "$"], timeout=5).strip()
    if not found:
        # A racing read must not finalize a start whose Docker create is pending.
        return {"status":"starting" if value.get("status") == "starting" else "unavailable", "scope":"full_isolated_restore", "cleanup_complete":False, "reason":"isolated_container_not_observable", "live_database_modified":False}
    assert_owned(value)
    raw = command([docker_path(), "exec", value["container"], "sh", "-c", "if test -f /tmp/timeaudit-restore.exit; then cat /tmp/timeaudit-restore.exit; elif test -f /tmp/timeaudit-restore.started; then printf running; else printf starting; fi"], timeout=5).decode("ascii").strip()
    if raw not in {"starting", "running"} and not (raw.isdigit() and 0 <= int(raw) <= 255):
        raise RuntimeError("restore_completion_record_invalid")
    result = raw if raw in {"starting", "running"} else ("ready_to_verify" if raw == "0" else "failed")
    return {"status": result, "scope": "full_isolated_restore", "cleanup_complete": False, "requires_finish": raw not in {"starting", "running"}, "live_database_modified": False}


@serialized
def start(path, *, container="audit-postgres"):
    path = Path(path).resolve(strict=True)
    if not NAME.fullmatch(path.name):
        raise ValueError("restore_requires_completed_archive")
    if STATE_PATH.exists():
        previous = state()
        if not previous.get("cleanup_complete"):
            raise RuntimeError("previous_restore_requires_status_or_finish")
    manifest = verify(path, container=container)
    image = image_for(container)
    job_id = uuid.uuid4().hex[:16]
    name = "timeaudit-restorecheck-" + job_id
    value = {"schema": "timeaudit.restore-check-state.v1", "job_id": job_id, "container": name, "source_container": container, "archive": str(path), "sha256": manifest["sha256"], "bytes": manifest["bytes"], "mtime_ns": manifest["mtime_ns"], "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "starting", "cleanup_complete": False}
    save(value)
    created = False
    try:
        command([docker_path(), "run", "-d", "--pull=never", "--name", name, "--label", "timeaudit.purpose=isolated-restore-check", "--label", "timeaudit.restore_id="+job_id, "--network", "none", "--cpus", "2", "--memory", "2g", "--pids-limit", "128", "--mount", f"type=bind,source={path.parent},target=/backup,readonly", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", image], timeout=30)
        created = True
        ready = False
        for _ in range(30):
            try:
                command([docker_path(), "exec", name, "pg_isready", "-U", "postgres"], timeout=3)
                ready = True
                break
            except RuntimeError:
                time.sleep(1)
        if not ready:
            raise RuntimeError("isolated_database_not_ready")
        command([docker_path(), "exec", name, "createdb", "-U", "postgres", "restorecheck"], timeout=10)
        # Only the validated archive basename is interpolated into this fixed
        # container-local command. The exit record persists if the client dies.
        script = (f"touch /tmp/timeaudit-restore.started; timeout 3600 pg_restore --exit-on-error --no-owner --no-acl -U postgres -d restorecheck '/backup/{path.name}' >/tmp/timeaudit-restore.stdout 2>/tmp/timeaudit-restore.stderr; "
                  'result=$?; printf "%s" "$result" >/tmp/timeaudit-restore.exit')
        command([docker_path(), "exec", "-d", name, "sh", "-c", script], timeout=5)
        value["status"] = "running"
        save(value)
        return {"status": "running", "scope": "full_isolated_restore", "requires_finish": True, "completion_survives_client_disconnect": True, "live_database_modified": False}
    except Exception:
        if created:
            assert_owned(value)
            command([docker_path(), "rm", "-f", "-v", name], timeout=30)
        value.update(status="failed", cleanup_complete=created)
        save(value)
        raise


@serialized
def finish():
    value = state()
    progress = status()
    if progress["status"] in {"starting", "running", "unavailable"} or progress.get("cleanup_complete"):
        return progress
    assert_owned(value)
    passed = False
    try:
        if progress["status"] != "ready_to_verify":
            raise RuntimeError("restore_worker_failed")
        path = Path(value["archive"])
        metadata = path.stat()
        if (metadata.st_size, metadata.st_mtime_ns) != (value["bytes"], value["mtime_ns"]):
            raise RuntimeError("archive_changed_during_restore")
        sql = "SELECT json_build_object('hardware_has_rows',EXISTS(SELECT 1 FROM public.fact_system_hardware LIMIT 1),'activity_has_rows',EXISTS(SELECT 1 FROM public.app_usage_logs LIMIT 1),'registry_has_rows',EXISTS(SELECT 1 FROM public.dim_process_registry LIMIT 1),'public_tables',(SELECT count(*) FROM pg_tables WHERE schemaname='public'));"
        checks = json.loads(command([docker_path(), "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=8000", value["container"], "psql", "-X", "-U", "postgres", "-d", "restorecheck", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql], timeout=10))
        if set(checks) != {"hardware_has_rows", "activity_has_rows", "registry_has_rows", "public_tables"} or any(type(checks[k]) is not bool for k in checks if k.endswith("has_rows")) or type(checks["public_tables"]) is not int:
            raise RuntimeError("restore_check_output_invalid")
        manifest_path = path.with_suffix(path.suffix + ".json")
        if manifest_path.exists() and manifest_path.stat().st_size > 65536:
            raise RuntimeError("restore_manifest_too_large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else verify(path, container=value["source_container"])
        if not isinstance(manifest, dict):
            raise RuntimeError("restore_manifest_invalid")
        if manifest.get("sha256") != value["sha256"] or manifest.get("bytes") != value["bytes"]:
            raise RuntimeError("restore_manifest_mismatch")
        manifest.update(restore_check_verified=True, restore_checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(), restore_checks=checks, verification_scope="full_isolated_restore_and_sha256")
        atomic_json(manifest_path, manifest)
        passed = True
        value["checks"] = checks
    finally:
        command([docker_path(), "rm", "-f", "-v", value["container"]], timeout=30)
        value.update(status="pass" if passed else "failed", cleanup_complete=True, completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        save(value)
    return {"status": "pass", "scope": "full_isolated_restore", "checks": value["checks"], "cleanup_complete": True, "live_database_modified": False}
