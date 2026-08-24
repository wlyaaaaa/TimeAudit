"""Local PostgreSQL connection settings without repository credentials."""

from __future__ import annotations

import os
from urllib.parse import quote


DEFAULT_DB_HOST = "127.0.0.1"
DEFAULT_DB_HOST_PORT = 45432


def local_host_port() -> int:
    raw_value = os.environ.get(
        "TIMEAUDIT_DB_HOST_PORT",
        str(DEFAULT_DB_HOST_PORT),
    )
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TIMEAUDIT_DB_HOST_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("TIMEAUDIT_DB_HOST_PORT must be between 1 and 65535")
    return port


def local_dsn() -> str:
    password = os.environ.get("TIMEAUDIT_DB_PASSWORD")
    if not password:
        raise RuntimeError("TIMEAUDIT_DB_PASSWORD is required")
    encoded_password = quote(password, safe="")
    return (
        f"postgresql://leyang:{encoded_password}"
        f"@{DEFAULT_DB_HOST}:{local_host_port()}/time_audit"
    )
