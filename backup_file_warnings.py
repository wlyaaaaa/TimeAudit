"""Classify narrowly scoped file exceptions without hiding ordinary I/O failures."""
from pathlib import Path


def file_warning(exc, relative_path, stage, *, enumerated=False):
    code = getattr(exc, "winerror", None)
    if code is None:
        value = getattr(exc, "hresult", None)
        if value is not None and (value & 0xFFFF0000) == 0x80070000:
            code = value & 0xFFFF
    if code in (225, 226):
        reason = "antivirus_blocked" if code == 225 else "antivirus_removed"
    elif enumerated and isinstance(exc, FileNotFoundError):
        code, reason = getattr(exc, "errno", 2), "source_disappeared"
    else:
        return None
    return {"relative_path": str(relative_path).replace("\\", "/"),
            "reason": reason, "error_code": code, "stage": stage}
