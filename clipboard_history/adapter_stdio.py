from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    CONTRACT_VERSION,
    EXPORT_ERROR_SCHEMA,
    EXPORT_REQUEST_SCHEMA,
    EXPORT_RESPONSE_SCHEMA,
    SOURCE_PROFILE_KEY,
)
from .paths import runtime_paths
from .storage import ReadOnlyClipboardStore


MAX_REQUEST_BYTES = 16 * 1024
ALLOWED_REQUEST_FIELDS = {"schema", "action", "checkpoint", "limit", "include_payload"}


class RequestError(ValueError):
    pass


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("invalid_request_size")
    if sys.stdin.buffer.read(1):
        raise RequestError("multiple_requests_not_supported")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise RequestError("invalid_json") from None
    if not isinstance(value, dict) or set(value) - ALLOWED_REQUEST_FIELDS:
        raise RequestError("invalid_request_fields")
    if value.get("schema") != EXPORT_REQUEST_SCHEMA or value.get("action") != "export":
        raise RequestError("unsupported_request")
    if type(value.get("limit", 100)) is not int or not 1 <= value.get("limit", 100) <= 500:
        raise RequestError("invalid_limit")
    if type(value.get("include_payload", True)) is not bool:
        raise RequestError("invalid_include_payload")
    checkpoint = value.get("checkpoint")
    if checkpoint is not None:
        if (
            not isinstance(checkpoint, dict)
            or set(checkpoint) != {"observed_at_utc", "event_id"}
            or not isinstance(checkpoint["observed_at_utc"], str)
            or not isinstance(checkpoint["event_id"], str)
            or not checkpoint["event_id"].startswith("evt_")
        ):
            raise RequestError("invalid_checkpoint")
    return value


def _event_envelope(row: dict[str, Any], include_payload: bool) -> dict[str, Any]:
    event = {
        "event_id": row["event_id"],
        "observed_at_utc": row["observed_at_utc"],
        "source_profile_key": SOURCE_PROFILE_KEY,
        "source_instance_id": row["source_instance_id"],
        "collector_instance_id": row["collector_instance_id"],
        "boot_id": row["boot_id"],
        "session_id": row["session_id"],
        "clipboard_sequence": row["clipboard_sequence"],
        "event_kind": row["event_kind"],
        "observation_kind": row["observation_kind"],
        "payload_type": row["payload_type"],
        "reason": row["reason"],
        "gap_count": row["gap_count"],
        "restored_from_event_id": row["restored_from_event_id"],
        "restore_request_id": row["restore_request_id"],
    }
    if include_payload and row["payload_text"] is not None:
        event["payload"] = {
            "sha256": row["payload_sha256"],
            "text": row["payload_text"],
            "utf8_bytes": row["payload_utf8_bytes"],
        }
    return event


def _write(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(
        (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    )
    sys.stdout.buffer.flush()


def run(data_root: Path | None) -> int:
    request = _read_request()
    checkpoint = request.get("checkpoint")
    store = ReadOnlyClipboardStore(runtime_paths(data_root).database)
    try:
        rows, has_more = store.export_after(
            checkpoint_observed_at_utc=(
                checkpoint["observed_at_utc"] if checkpoint else None
            ),
            checkpoint_event_id=checkpoint["event_id"] if checkpoint else None,
            limit=request.get("limit", 100),
        )
        events = [
            _event_envelope(row, request.get("include_payload", True)) for row in rows
        ]
        next_checkpoint = (
            {
                "observed_at_utc": rows[-1]["observed_at_utc"],
                "event_id": rows[-1]["event_id"],
            }
            if rows
            else checkpoint
        )
        _write(
            {
                "schema": EXPORT_RESPONSE_SCHEMA,
                "status": "ok",
                "source_profile_key": SOURCE_PROFILE_KEY,
                "source_contract_version": CONTRACT_VERSION,
                "source_instance_id": store.source_instance_id(),
                "events": events,
                "next_checkpoint": next_checkpoint,
                "has_more": has_more,
            }
        )
        return 0
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    try:
        return run(args.data_root)
    except RequestError as error:
        _write(
            {
                "schema": EXPORT_ERROR_SCHEMA,
                "status": "error",
                "code": str(error),
            }
        )
        return 2
    except (OSError, RuntimeError, ValueError):
        _write(
            {
                "schema": EXPORT_ERROR_SCHEMA,
                "status": "error",
                "code": "adapter_unavailable",
            }
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
