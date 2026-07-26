from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from .control import write_control_state
from .model import CAPTURE_LIMIT_BYTES
from .paths import runtime_paths
from .win32_clipboard import (
    CLOUD_FORMAT,
    EXCLUDE_FORMAT,
    INCLUDE_HISTORY_FORMAT,
    restore_text,
    set_test_clipboard,
)


def _lookup(database: Path, digest: str):
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return connection.execute(
            """
            SELECT e.event_id,e.observation_kind,e.restored_from_event_id
            FROM events e JOIN blobs b ON b.blob_id=e.blob_id
            WHERE b.sha256=?
            ORDER BY e.observed_at_utc,e.event_id
            """,
            (digest,),
        ).fetchall()
    finally:
        connection.close()


def _reason_count(database: Path, reason: str) -> int:
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return int(
            connection.execute(
                "SELECT count(*) FROM events WHERE reason=?", (reason,)
            ).fetchone()[0]
        )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--verify-pause", action="store_true")
    parser.add_argument("--verify-policies", action="store_true")
    args = parser.parse_args()
    paths = runtime_paths(args.data_root)
    marker = f"PersonalOS-Smoke-{secrets.token_hex(16)}-中文🙂\nline2"
    digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
    restore_text("evt_nonexistent_smoke", marker)
    deadline = time.monotonic() + args.timeout
    rows = []
    while time.monotonic() < deadline:
        rows = _lookup(paths.database, digest)
        if rows:
            break
        time.sleep(0.1)
    if not rows:
        print(json.dumps({"status": "failed", "reason": "initial_capture_timeout"}))
        return 2
    original = rows[0][0]
    restore_text(original, marker)
    while time.monotonic() < deadline:
        rows = _lookup(paths.database, digest)
        if any(row[1] == "history_restore" and row[2] == original for row in rows):
            break
        time.sleep(0.1)
    passed = any(
        row[1] == "history_restore" and row[2] == original for row in rows
    )
    result = {
        "status": "passed" if passed else "failed",
        "marker_sha256": digest,
        "event_count": len(rows),
        "lineage_count": sum(1 for row in rows if row[2] == original),
    }
    if passed and args.verify_pause:
        write_control_state(paths.control, paused=True)
        time.sleep(2.5)
        paused_marker = f"PersonalOS-Pause-{secrets.token_hex(16)}"
        paused_digest = hashlib.sha256(paused_marker.encode("utf-8")).hexdigest()
        restore_text("evt_nonexistent_pause", paused_marker)
        time.sleep(1.0)
        paused_rows = _lookup(paths.database, paused_digest)
        write_control_state(paths.control, paused=False)
        time.sleep(2.5)
        resumed_marker = f"PersonalOS-Resume-{secrets.token_hex(16)}"
        resumed_digest = hashlib.sha256(resumed_marker.encode("utf-8")).hexdigest()
        restore_text("evt_nonexistent_resume", resumed_marker)
        resume_deadline = time.monotonic() + args.timeout
        resumed_rows = []
        while time.monotonic() < resume_deadline:
            resumed_rows = _lookup(paths.database, resumed_digest)
            if resumed_rows:
                break
            time.sleep(0.1)
        pause_passed = not paused_rows and bool(resumed_rows)
        result.update(
            {
                "pause_check": "passed" if pause_passed else "failed",
                "paused_capture_count": len(paused_rows),
                "resumed_capture_count": len(resumed_rows),
            }
        )
        passed = passed and pause_passed
        result["status"] = "passed" if passed else "failed"
    if passed and args.verify_policies:
        policy_results: dict[str, bool] = {}
        excluded = f"PersonalOS-Excluded-{secrets.token_hex(16)}"
        excluded_digest = hashlib.sha256(excluded.encode()).hexdigest()
        before = _reason_count(paths.database, "excluded_by_source")
        set_test_clipboard(excluded, dword_formats={EXCLUDE_FORMAT: 1})
        time.sleep(0.5)
        policy_results["exclude"] = (
            not _lookup(paths.database, excluded_digest)
            and _reason_count(paths.database, "excluded_by_source") > before
        )

        disallowed = f"PersonalOS-HistoryDisallowed-{secrets.token_hex(16)}"
        disallowed_digest = hashlib.sha256(disallowed.encode()).hexdigest()
        before = _reason_count(paths.database, "history_disallowed_by_source")
        set_test_clipboard(
            disallowed, dword_formats={INCLUDE_HISTORY_FORMAT: 0}
        )
        time.sleep(0.5)
        policy_results["history_disallowed"] = (
            not _lookup(paths.database, disallowed_digest)
            and _reason_count(paths.database, "history_disallowed_by_source") > before
        )

        local_only = f"PersonalOS-LocalOnly-{secrets.token_hex(16)}"
        local_only_digest = hashlib.sha256(local_only.encode()).hexdigest()
        set_test_clipboard(local_only, dword_formats={CLOUD_FORMAT: 0})
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not _lookup(
            paths.database, local_only_digest
        ):
            time.sleep(0.1)
        policy_results["cloud_zero_local_capture"] = bool(
            _lookup(paths.database, local_only_digest)
        )

        before = _reason_count(paths.database, "unsupported_format")
        set_test_clipboard(None, unsupported=True)
        time.sleep(0.5)
        policy_results["unsupported"] = (
            _reason_count(paths.database, "unsupported_format") > before
        )

        before = _reason_count(paths.database, "empty_text")
        set_test_clipboard("")
        time.sleep(0.5)
        policy_results["empty"] = _reason_count(paths.database, "empty_text") > before

        before = _reason_count(paths.database, "payload_too_large")
        set_test_clipboard("x" * (CAPTURE_LIMIT_BYTES // 2 + 1))
        time.sleep(0.5)
        policy_results["oversize"] = (
            _reason_count(paths.database, "payload_too_large") > before
        )
        policies_passed = all(policy_results.values())
        result["policy_check"] = "passed" if policies_passed else "failed"
        result["policy_cases"] = policy_results
        passed = passed and policies_passed
        result["status"] = "passed" if passed else "failed"
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
