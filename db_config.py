"""Local PostgreSQL connection settings without repository credentials."""

from __future__ import annotations

import os
from urllib.parse import quote


def local_dsn() -> str:
    password = os.environ.get("TIMEAUDIT_DB_PASSWORD")
    if not password:
        raise RuntimeError("TIMEAUDIT_DB_PASSWORD is required")
    encoded_password = quote(password, safe="")
    return (
        f"postgresql://leyang:{encoded_password}"
        "@127.0.0.1:55432/time_audit"
    )
