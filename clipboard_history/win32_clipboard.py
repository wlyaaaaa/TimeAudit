from __future__ import annotations

import ctypes
import json
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass

from .model import CAPTURE_LIMIT_BYTES, CaptureDecision, classify_file_paths, classify_text


if not hasattr(wintypes, "LRESULT"):
    wintypes.LRESULT = ctypes.c_ssize_t

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
INCLUDE_HISTORY_FORMAT = "CanIncludeInClipboardHistory"
CLOUD_FORMAT = "CanUploadToCloudClipboard"
RESTORE_FORMAT = "PersonalOS.ClipboardHistory.RestoreV1"

user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
shell32.DragQueryFileW.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPWSTR,
    wintypes.UINT,
]
shell32.DragQueryFileW.restype = wintypes.UINT


@dataclass(frozen=True)
class RestoreMarker:
    event_id: str
    request_id: str


@dataclass(frozen=True)
class ClipboardRead:
    sequence: int
    decision: CaptureDecision
    marker: RestoreMarker | None


def register_formats() -> dict[str, int]:
    return {
        "exclude": user32.RegisterClipboardFormatW(EXCLUDE_FORMAT),
        "include_history": user32.RegisterClipboardFormatW(INCLUDE_HISTORY_FORMAT),
        "cloud": user32.RegisterClipboardFormatW(CLOUD_FORMAT),
        "restore": user32.RegisterClipboardFormatW(RESTORE_FORMAT),
    }


def sequence_number() -> int:
    return int(user32.GetClipboardSequenceNumber())


def _copy_hglobal_bytes(fmt: int, limit: int = CAPTURE_LIMIT_BYTES + 2) -> bytes | None:
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = int(kernel32.GlobalSize(handle))
    if size <= 0 or size > limit:
        return None
    address = kernel32.GlobalLock(handle)
    if not address:
        return None
    try:
        return ctypes.string_at(address, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _read_dword(fmt: int) -> int | None:
    raw = _copy_hglobal_bytes(fmt, 16)
    if raw is None or len(raw) < 4:
        return None
    return int.from_bytes(raw[:4], "little")


def _read_unicode() -> CaptureDecision:
    raw = _copy_hglobal_bytes(CF_UNICODETEXT)
    if raw is None:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle and int(kernel32.GlobalSize(handle)) > CAPTURE_LIMIT_BYTES + 2:
            return CaptureDecision(None, None, "payload_too_large")
        return CaptureDecision(None, None, "clipboard_data_unavailable")
    if len(raw) % 2:
        return CaptureDecision(None, None, "unicode_payload_invalid")
    try:
        text = raw.decode("utf-16-le", errors="strict").split("\x00", 1)[0]
    except UnicodeError:
        return CaptureDecision(None, None, "unicode_payload_invalid")
    return classify_text(text)


def _read_file_paths() -> CaptureDecision:
    handle = user32.GetClipboardData(CF_HDROP)
    if not handle:
        return CaptureDecision(None, None, "clipboard_data_unavailable")
    count = int(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
    if count > 4096:
        return CaptureDecision(None, None, "file_list_too_large")
    paths: list[str] = []
    for index in range(count):
        length = int(shell32.DragQueryFileW(handle, index, None, 0))
        if length <= 0:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        shell32.DragQueryFileW(handle, index, buffer, length + 1)
        paths.append(buffer.value)
    return classify_file_paths(paths)


def _read_marker(fmt: int) -> RestoreMarker | None:
    if not user32.IsClipboardFormatAvailable(fmt):
        return None
    raw = _copy_hglobal_bytes(fmt, 4096)
    if raw is None:
        return None
    try:
        value = json.loads(raw.rstrip(b"\x00").decode("utf-8", errors="strict"))
        if (
            set(value) != {"schema", "event_id", "request_id"}
            or value["schema"] != "timeaudit.clipboard-restore.v1"
            or not isinstance(value["event_id"], str)
            or not value["event_id"].startswith("evt_")
            or not isinstance(value["request_id"], str)
            or not value["request_id"].startswith("req_")
        ):
            return None
        return RestoreMarker(value["event_id"], value["request_id"])
    except (UnicodeError, json.JSONDecodeError, TypeError):
        return None


def read_clipboard(hwnd: int, formats: dict[str, int]) -> ClipboardRead:
    opened = False
    for delay in (0.005, 0.010, 0.020, 0.040, 0.080):
        if user32.OpenClipboard(hwnd):
            opened = True
            break
        time.sleep(delay)
    current_sequence = sequence_number()
    if not opened:
        return ClipboardRead(
            current_sequence,
            CaptureDecision(None, None, "clipboard_locked"),
            None,
        )
    try:
        if user32.IsClipboardFormatAvailable(formats["exclude"]):
            return ClipboardRead(
                current_sequence,
                CaptureDecision(None, None, "excluded_by_source"),
                None,
            )
        if user32.IsClipboardFormatAvailable(formats["include_history"]):
            include = _read_dword(formats["include_history"])
            if include == 0:
                return ClipboardRead(
                    current_sequence,
                    CaptureDecision(None, None, "history_disallowed_by_source"),
                    None,
                )
        marker = _read_marker(formats["restore"])
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            decision = _read_file_paths()
        elif user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            decision = _read_unicode()
        else:
            decision = CaptureDecision(None, None, "unsupported_format")
        return ClipboardRead(current_sequence, decision, marker)
    finally:
        user32.CloseClipboard()


def _new_hglobal(data: bytes) -> int:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("global_alloc_failed")
    address = kernel32.GlobalLock(handle)
    if not address:
        kernel32.GlobalFree(handle)
        raise OSError("global_lock_failed")
    try:
        ctypes.memmove(address, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    return int(handle)


def restore_text(event_id: str, text: str) -> tuple[str, int]:
    formats = register_formats()
    request_id = f"req_{uuid.uuid4().hex}"
    marker = json.dumps(
        {
            "schema": "timeaudit.clipboard-restore.v1",
            "event_id": event_id,
            "request_id": request_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\x00"
    unicode_payload = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise OSError("clipboard_locked")
    unicode_handle = 0
    marker_handle = 0
    try:
        if not user32.EmptyClipboard():
            raise OSError("empty_clipboard_failed")
        unicode_handle = _new_hglobal(unicode_payload)
        if not user32.SetClipboardData(CF_UNICODETEXT, unicode_handle):
            raise OSError("set_unicode_failed")
        unicode_handle = 0
        marker_handle = _new_hglobal(marker)
        if not user32.SetClipboardData(formats["restore"], marker_handle):
            raise OSError("set_marker_failed")
        marker_handle = 0
    finally:
        if unicode_handle:
            kernel32.GlobalFree(unicode_handle)
        if marker_handle:
            kernel32.GlobalFree(marker_handle)
        user32.CloseClipboard()
    return request_id, sequence_number()


def set_test_clipboard(
    text: str | None,
    *,
    dword_formats: dict[str, int] | None = None,
    unsupported: bool = False,
) -> int:
    """Synthetic integration helper. Never use it to read existing clipboard data."""
    handles: list[int] = []
    transferred: set[int] = set()
    if not user32.OpenClipboard(None):
        raise OSError("clipboard_locked")
    try:
        if not user32.EmptyClipboard():
            raise OSError("empty_clipboard_failed")
        if text is not None:
            handle = _new_hglobal(text.encode("utf-16-le") + b"\x00\x00")
            handles.append(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise OSError("set_unicode_failed")
            transferred.add(handle)
        for name, value in (dword_formats or {}).items():
            fmt = user32.RegisterClipboardFormatW(name)
            handle = _new_hglobal(int(value).to_bytes(4, "little"))
            handles.append(handle)
            if not user32.SetClipboardData(fmt, handle):
                raise OSError("set_policy_failed")
            transferred.add(handle)
        if unsupported:
            fmt = user32.RegisterClipboardFormatW(
                "PersonalOS.ClipboardHistory.TestUnsupportedV1"
            )
            handle = _new_hglobal(b"test\x00")
            handles.append(handle)
            if not user32.SetClipboardData(fmt, handle):
                raise OSError("set_unsupported_failed")
            transferred.add(handle)
    finally:
        for handle in handles:
            if handle not in transferred:
                kernel32.GlobalFree(handle)
        user32.CloseClipboard()
    return sequence_number()
