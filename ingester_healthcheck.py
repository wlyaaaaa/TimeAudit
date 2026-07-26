"""Payload-free Docker healthcheck for the TimeAudit screen-time ingester."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


HEARTBEAT_PATH = Path(
    os.environ.get("TIMEAUDIT_INGEST_HEARTBEAT", "/log/ingester_heartbeat.json")
)
MAX_AGE_SECONDS = int(os.environ.get("TIMEAUDIT_INGEST_MAX_AGE_SECONDS", "45"))


def heartbeat_status(
    path: Path = HEARTBEAT_PATH,
    *,
    now_ms: int | None = None,
    max_age_seconds: int = MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        if payload.get("schema") != "timeaudit.ingester-heartbeat.v1":
            return False, "schema"
        updated_ms = int(payload["updated_at_unix_ms"])
        age_ms = (now_ms if now_ms is not None else int(time.time() * 1000)) - updated_ms
        if age_ms < 0 or age_ms > max_age_seconds * 1000:
            return False, "stale"
        if payload.get("state") != "healthy":
            return False, "degraded"
        return True, "healthy"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False, "unreadable"


if __name__ == "__main__":
    healthy, _reason = heartbeat_status()
    raise SystemExit(0 if healthy else 1)
