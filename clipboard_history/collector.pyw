from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import sys
import time
import uuid
import winreg
from ctypes import wintypes
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipboard_history import SCHEMA_VERSION
from clipboard_history.control import read_control_state, write_status
from clipboard_history.paths import runtime_paths
from clipboard_history.storage import ClipboardStore
from clipboard_history.win32_clipboard import (
    read_clipboard,
    register_formats,
    sequence_number,
)


WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_TIMER = 0x0113
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_CLIPBOARDUPDATE = 0x031D
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_SESSION_LOCK = 0x0007
WTS_SESSION_UNLOCK = 0x0008
NOTIFY_FOR_THIS_SESSION = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = wintypes.LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL


def _session_id() -> str:
    value = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(value)):
        return "session_unknown"
    return f"session_{int(value.value)}"


def _boot_id() -> str:
    boot_unix_seconds = int(time.time() - kernel32.GetTickCount64() / 1000)
    return f"boot_{boot_unix_seconds:x}"


def _source_instance_id() -> str:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except OSError:
        machine_guid = os.environ.get("COMPUTERNAME", "unknown")
    digest = hashlib.sha256(machine_guid.encode("utf-8")).hexdigest()[:32]
    return f"windows:{digest}"


class Collector:
    def __init__(self, data_root: Path):
        self.paths = runtime_paths(data_root)
        self.store = ClipboardStore(self.paths.database)
        self.store.initialize()
        self.collector_instance_id = f"collector_{uuid.uuid4().hex}"
        self.boot_id = _boot_id()
        self.session_id = _session_id()
        self.source_instance_id = _source_instance_id()
        self.store.register_source_instance(self.source_instance_id)
        self.formats = register_formats()
        self.last_sequence = sequence_number()
        self.control = read_control_state(self.paths.control)
        self.capture_suspended = False
        self.hwnd = 0
        self._last_heartbeat = 0.0

    def event_identity(self) -> dict[str, object]:
        return {
            "collector_instance_id": self.collector_instance_id,
            "boot_id": self.boot_id,
            "session_id": self.session_id,
            "source_instance_id": self.source_instance_id,
        }

    def record_boundary(self, reason: str) -> None:
        self.store.record_boundary(
            **self.event_identity(),
            clipboard_sequence=self.last_sequence,
            reason=reason,
        )

    def baseline(self, reason: str) -> None:
        self.last_sequence = sequence_number()
        self.record_boundary(reason)
        self.heartbeat(reason=reason)

    def heartbeat(self, reason: str = "healthy") -> None:
        now = time.monotonic()
        if reason == "healthy" and now - self._last_heartbeat < 4:
            return
        common = {
            **self.event_identity(),
            "state": "paused" if self.control.paused else "running",
            "reason": reason,
            "last_sequence": self.last_sequence,
            "paused": self.control.paused,
            "pid": os.getpid(),
            "schema_version": SCHEMA_VERSION,
        }
        write_status(self.paths.heartbeat, **common)
        write_status(self.paths.collector_state, **common)
        self._last_heartbeat = now

    def refresh_control(self) -> None:
        latest = read_control_state(self.paths.control)
        if latest == self.control:
            self.heartbeat()
            return
        was_paused = self.control.paused
        self.control = latest
        if not was_paused and latest.paused:
            self.record_boundary("pause_started")
            self.last_sequence = sequence_number()
        elif was_paused and not latest.paused:
            self.baseline("pause_ended_baseline")
        self.heartbeat(reason="control_changed")

    def on_clipboard_update(self) -> None:
        notification_sequence = sequence_number()
        if self.control.paused or self.capture_suspended:
            self.last_sequence = notification_sequence
            self.heartbeat()
            return
        result = read_clipboard(self.hwnd, self.formats)
        if result.sequence != notification_sequence:
            self.store.record_gap(
                **self.event_identity(),
                clipboard_sequence=result.sequence,
                reason="clipboard_race_before_open",
                gap_count=None,
            )
        self.last_sequence = result.sequence
        decision = result.decision
        if decision.reason:
            self.store.record_skip(
                **self.event_identity(),
                clipboard_sequence=result.sequence,
                reason=decision.reason,
            )
        elif decision.text is not None and decision.payload_type is not None:
            restored_from = None
            request_id = None
            observation_kind = "copy"
            if result.marker and self.store.validate_restore(
                result.marker.event_id, decision.text
            ):
                restored_from = result.marker.event_id
                request_id = result.marker.request_id
                observation_kind = "history_restore"
            self.store.record_capture(
                **self.event_identity(),
                clipboard_sequence=result.sequence,
                payload_type=decision.payload_type,
                text=decision.text,
                observation_kind=observation_kind,
                restored_from_event_id=restored_from,
                restore_request_id=request_id,
            )
        self.heartbeat(reason="clipboard_observed")

    def close(self) -> None:
        try:
            self.heartbeat(reason="collector_stopping")
        finally:
            self.store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    collector = Collector(args.data_root)
    mutex_name = (
        "Local\\PersonalOSClipboardCollector_"
        + hashlib.sha256(str(collector.paths.root).lower().encode()).hexdigest()[:24]
    )
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    if not mutex or ctypes.get_last_error() == 183:
        collector.close()
        return 0

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_CLIPBOARDUPDATE:
            collector.on_clipboard_update()
            return 0
        if message == WM_TIMER:
            collector.refresh_control()
            return 0
        if message == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                collector.capture_suspended = True
                collector.record_boundary("system_suspend")
            elif wparam == PBT_APMRESUMEAUTOMATIC:
                collector.capture_suspended = False
                collector.baseline("system_resume_baseline")
            return 1
        if message == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                collector.capture_suspended = True
                collector.record_boundary("session_locked")
            elif wparam == WTS_SESSION_UNLOCK:
                collector.capture_suspended = False
                collector.baseline("session_unlock_baseline")
            return 0
        if message == WM_QUERYENDSESSION:
            collector.record_boundary("session_ending")
            return 1
        if message == WM_ENDSESSION and wparam:
            collector.record_boundary("session_ended")
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = f"PersonalOSClipboardCollector_{os.getpid()}"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        collector.close()
        return 2
    hwnd = user32.CreateWindowExW(
        0, class_name, class_name, 0, 0, 0, 0, 0, None, None, instance, None
    )
    if not hwnd:
        collector.close()
        return 3
    collector.hwnd = hwnd
    if not user32.AddClipboardFormatListener(hwnd):
        collector.close()
        return 4
    wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
    user32.SetTimer(hwnd, 1, 2000, None)
    collector.baseline("startup_baseline")
    message = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        user32.KillTimer(hwnd, 1)
        user32.RemoveClipboardFormatListener(hwnd)
        wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        collector.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        try:
            parsed_root = None
            if "--data-root" in sys.argv:
                parsed_root = Path(sys.argv[sys.argv.index("--data-root") + 1])
            write_status(
                runtime_paths(parsed_root).collector_state,
                state="failed",
                reason="collector_unhandled_error",
                pid=os.getpid(),
            )
        finally:
            raise SystemExit(10)
