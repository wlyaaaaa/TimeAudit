from __future__ import annotations

import hashlib
import base64
import json
import math
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


_SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret(?:[_-]?key)?|"
    r"password|passwd|client[_-]?secret)\b\s*[:=]\s*['\"]?([^\s'\";,}]{8,256})"
)
_KNOWN_PREFIX = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{24,}|"
    r"github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{24,}|"
    r"AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})$"
)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_URI = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")
_MAILTO = re.compile(r"^mailto:[^\s@?]+@[^\s@?]+(?:\?\S*)?$", re.IGNORECASE)
_RECOVERY_KEY = re.compile(r"^(?:[0-9]{6}-){7}[0-9]{6}$")
# These structured identifiers often have high entropy without being secrets.
# Restrict filenames to familiar extensions and identifier punctuation; a dot
# alone (or a password containing !, @, etc.) is not a filename exemption.
_FILENAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*\."
    r"(?:txt|md|pdf|docx?|xlsx?|pptx?|csv|tsv|json|ya?ml|xml|html?|"
    r"png|jpe?g|gif|webp|svg|mp[34]|wav|zip|7z|rar|tar|gz|exe|msi|py)",
    re.IGNORECASE,
)
_VERSION_IDENTIFIER = re.compile(
    r"(?:[A-Za-z]+[-_])*[vV]?[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-_][A-Za-z]+(?:[.-]?[0-9]+)?)?"
)
_MODEL_IDENTIFIER = re.compile(
    r"(?:[A-Za-z]+[0-9]+(?:\.[0-9]+)*-[0-9]+(?:\.[0-9]+)?[Bb]"
    r"(?:-[A-Za-z]+)*|[a-z]+(?:-[a-z0-9]+)*-[0-9]{4}-[0-9]{2}-[0-9]{2})"
)
_PLACEHOLDER = re.compile(
    r"(?i)(?:example|sample|placeholder|changeme|replace[_-]?me|your[_-]?|dummy|"
    r"not[_-]?a[_-]?real|<[^>]+>|\$\{[^}]+\})"
)


def _entropy(value: str) -> float:
    counts = {character: value.count(character) for character in set(value)}
    size = len(value)
    return -sum((n / size) * math.log2(n / size) for n in counts.values())


def _tokenish(value: str) -> bool:
    if not 8 <= len(value) <= 256 or any(c.isspace() for c in value) or _PLACEHOLDER.search(value):
        return False
    if _UUID.fullmatch(value) or re.fullmatch(r"[0-9a-fA-F]{32,128}", value):
        return False
    if _URI.fullmatch(value) or "/" in value or "\\" in value:
        return False
    if "@" in value and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return False
    if any(pattern.fullmatch(value) for pattern in (_FILENAME, _VERSION_IDENTIFIER, _MODEL_IDENTIFIER)):
        return False
    if value.isalnum() and (len(value) < 24 or sum(c.isdigit() for c in value) < 4):
        return False
    classes = sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))
    if len(value) < 16:
        # Short hints require all four character classes, not merely English
        # text plus a number. This is a local hint, never credential validation.
        return classes == 4 and _entropy(value) >= 2.8
    words = re.split(r"[_-]|(?<=[a-z])(?=[A-Z])|[0-9]+", value)
    if len([word for word in words if len(word) >= 3]) >= 3 and not re.search(r"[^A-Za-z0-9_-]", value):
        return False
    return classes >= 3 and _entropy(value) >= 3.4


def _jwt_like(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3 or min(map(len, parts)) < 12:
        return False
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
        return isinstance(header, dict) and isinstance(header.get("alg"), str)
    except (ValueError, UnicodeError):
        return False


def looks_like_secret(text: str) -> bool:
    """Local format hint, never a claim that a credential is valid."""
    value = text.strip()
    if not value:
        return False
    # The product's secret-like filter is for key-shaped Latin/ASCII text.
    # An assignment embedded in a Chinese note is still a note, not a key row.
    if re.search(r"[\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]", value):
        return False
    if _KNOWN_PREFIX.fullmatch(value) or _jwt_like(value) or _RECOVERY_KEY.fullmatch(value):
        return True
    if len(value) <= 256 and _tokenish(value):
        return True
    for match in _SECRET_ASSIGNMENT.finditer(value):
        candidate = match.group(1)
        if _KNOWN_PREFIX.fullmatch(candidate) or _jwt_like(candidate) or _tokenish(candidate):
            return True
    bearer = re.search(r"(?i)\bBearer\s+([A-Za-z0-9._-]{16,256})\b", value)
    return bool(bearer and (_jwt_like(bearer.group(1)) or _tokenish(bearer.group(1))))


def looks_like_link(text: str) -> bool:
    value = text.strip()
    if _MAILTO.fullmatch(value):
        return True
    if not _URI.fullmatch(value):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and parts.netloc)
    except ValueError:
        return False


def content_kind(text: str, payload_type: str) -> str:
    if payload_type == "file_paths":
        return "file_paths"
    if looks_like_secret(text):
        return "secret_like"
    if looks_like_link(text):
        return "link"
    return "text"
