from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024
FILE_LIST_LIMIT = 4096


@dataclass(frozen=True)
class CaptureDecision:
    payload_type: str | None
    text: str | None
    reason: str | None


def payload_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def classify_text(text: str) -> CaptureDecision:
    if not text:
        return CaptureDecision(None, None, "empty_text")
    if len(text.encode("utf-16-le", errors="strict")) > CAPTURE_LIMIT_BYTES:
        return CaptureDecision(None, None, "payload_too_large")
    parts = urlsplit(text.strip())
    payload_type = (
        "url"
        if parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)
        else "text"
    )
    return CaptureDecision(payload_type, text, None)


def classify_file_paths(paths: list[str]) -> CaptureDecision:
    if not paths:
        return CaptureDecision(None, None, "empty_file_list")
    if len(paths) > FILE_LIST_LIMIT:
        return CaptureDecision(None, None, "file_list_too_large")
    normalized = "\n".join(paths)
    if len(normalized.encode("utf-16-le", errors="strict")) > CAPTURE_LIMIT_BYTES:
        return CaptureDecision(None, None, "payload_too_large")
    return CaptureDecision("file_paths", normalized, None)


_SEARCH_TOKEN = re.compile(r"""[^\s"'():*+\-^{}\[\]]+""", flags=re.UNICODE)


def fts_literal_query(value: str) -> str | None:
    tokens = _SEARCH_TOKEN.findall(value)
    if not tokens:
        return None
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
