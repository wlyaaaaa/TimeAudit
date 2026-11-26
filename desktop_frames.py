"""Count desktop image updates without reading any pixels."""
import ctypes as c
from ctypes import wintypes as w
from collections import deque
from contextlib import ExitStack, contextmanager
import threading
import time
from uuid import UUID


P, U = c.c_void_p, c.c_uint
OUT = c.POINTER(P)
NOT_FOUND, WAIT_TIMEOUT = 0x887A0002, 0x887A0027


class OutputDesc(c.Structure):
    _fields_ = [("name", c.c_wchar * 32), ("rect", w.RECT), ("attached", w.BOOL),
                ("rotation", U), ("monitor", P)]


class FrameInfo(c.Structure):
    _fields_ = [("present", c.c_int64), ("mouse", c.c_int64), ("count", U),
                ("coalesced", w.BOOL), ("masked", w.BOOL), ("position", w.POINT),
                ("visible", w.BOOL), ("metadata_size", U), ("pointer_size", U)]


def method(obj, slot, result, *args):
    table = c.cast(obj, c.POINTER(c.POINTER(P))).contents
    return c.WINFUNCTYPE(result, P, *args)(table[slot])


def checked(result):
    if result < 0:
        raise OSError(f"DXGI 0x{result & 0xffffffff:08x}")


def release(obj):
    method(obj, 2, U)(obj)


def foreground_monitor():
    user = c.windll.user32
    user.GetForegroundWindow.restype = w.HWND
    user.MonitorFromWindow.argtypes = (w.HWND, w.DWORD)
    user.MonitorFromWindow.restype = P
    hwnd = user.GetForegroundWindow()
    return user.MonitorFromWindow(hwnd, 2) if hwnd else None


@contextmanager
def duplication(target):
    factory, device, output1, dup = P(), P(), P(), P()
    with ExitStack() as scope:
        factory_id = UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le
        create = c.windll.dxgi.CreateDXGIFactory1
        create.argtypes = (c.c_char_p, OUT)
        checked(create(factory_id, c.byref(factory)))
        scope.callback(release, factory)
        for i in range(32):
            adapter = P()
            code = method(factory, 12, c.c_long, U, OUT)(factory, i, c.byref(adapter))
            if code & 0xffffffff == NOT_FOUND:
                break
            checked(code)
            scope.callback(release, adapter)
            for j in range(32):
                output = P()
                code = method(adapter, 7, c.c_long, U, OUT)(adapter, j, c.byref(output))
                if code & 0xffffffff == NOT_FOUND:
                    break
                checked(code)
                scope.callback(release, output)
                desc = OutputDesc()
                checked(method(output, 7, c.c_long, c.POINTER(OutputDesc))(output, c.byref(desc)))
                if desc.attached and desc.monitor == target:
                    output_id = UUID("00cddea8-939b-4b83-a340-a685226666cc").bytes_le
                    checked(method(output, 0, c.c_long, c.c_char_p, OUT)(output, output_id, c.byref(output1)))
                    scope.callback(release, output1)
                    create_device = c.windll.d3d11.D3D11CreateDevice
                    create_device.argtypes = (P, U, P, U, P, U, U, OUT, P, P)
                    checked(create_device(adapter, 0, None, 0, None, 0, 7, c.byref(device), None, None))
                    scope.callback(release, device)
                    checked(method(output1, 22, c.c_long, P, OUT)(output1, device, c.byref(dup)))
                    scope.callback(release, dup)
                    yield dup
                    return
        raise OSError("Desktop output unavailable")


class DesktopFrames:
    """One idle-gated capture. A sample consumes only the next observed interval."""

    def __init__(self):
        self._changed = threading.Condition()
        self._target = None
        self._epoch = 0
        self._active_until = 0
        self._thread = None
        self._closed = False
        self._events = deque()
        self._reset()

    def _reset(self):
        self._opened = self._observed = self._consumed = None
        self._events.clear()

    def sample(self, active, discard=False):
        target = foreground_monitor() if active else None
        with self._changed:
            if self._closed:
                return None
            now = time.perf_counter()
            if target != self._target or (target and now >= self._active_until):
                self._target = target
                self._epoch += 1
                self._reset()
                self._changed.notify()
            if not target:
                return None
            # Stop capturing if the main collector no longer confirms activity.
            self._active_until = now + 3
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="DesktopFrames", daemon=True)
                self._thread.start()
            if self._observed is None or now - self._observed > 0.6:
                return None
            # Leave one acquisition interval in memory for events not yet delivered.
            end = self._observed - 0.15
            start = self._consumed if self._consumed is not None else self._opened
            if end <= start or (end - start < 0.5 and not discard):
                return None
            frames = 0
            while self._events and self._events[0][0] <= end:
                when, count = self._events.popleft()
                if when > start:
                    frames += count
            self._consumed = end
            return None if discard else frames / (end - start)

    def close(self):
        with self._changed:
            self._closed = True
            self._changed.notify()
        if self._thread:
            self._thread.join(timeout=2)

    def _running(self, epoch):
        return not self._closed and epoch == self._epoch and time.perf_counter() < self._active_until

    def _run(self):
        freq = c.c_int64()
        c.windll.kernel32.QueryPerformanceFrequency(c.byref(freq))
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closed or self._target)
                if self._closed:
                    return
                target, epoch = self._target, self._epoch
                if not self._running(epoch):
                    self._target = None
                    self._reset()
                    continue
            try:
                with duplication(target) as dup:
                    self._capture(dup, epoch, freq.value)
            except OSError:
                # Secure desktops, display sleep and mode changes invalidate the handle.
                with self._changed:
                    if self._running(epoch):
                        self._reset()
                        self._changed.wait(timeout=1)

    def _capture(self, dup, epoch, frequency):
        take = method(dup, 8, c.c_long, U, c.POINTER(FrameInfo), OUT)
        give = method(dup, 14, c.c_long)
        info, resource = FrameInfo(), P()
        held, last = False, 0
        with self._changed:
            if not self._running(epoch):
                return
            self._opened = time.perf_counter()
        try:
            while self._running(epoch):
                if held:
                    if resource:
                        release(resource)
                    resource.value = None
                    held = False
                    checked(give(dup))
                code = take(dup, 100, c.byref(info), c.byref(resource))
                if (code & 0xffffffff) != WAIT_TIMEOUT:
                    checked(code)
                    held = True
                now = time.perf_counter()
                with self._changed:
                    if not self._running(epoch):
                        break
                    if now - (self._observed or self._opened) > 2.5:
                        self._events.clear()
                        self._opened, self._consumed = now, None
                    self._observed = now
                    while self._events and self._events[0][0] < now - 4:
                        self._opened = self._events.popleft()[0]
                        self._consumed = max(self._opened, self._consumed or 0)
                    if held and info.count and info.present > last:
                        when = info.present / frequency
                        if when > self._opened:
                            self._events.append((when, info.count))
                        last = info.present
        finally:
            if held:
                if resource:
                    release(resource)
                give(dup)
