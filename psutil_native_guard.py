"""Keep crash-prone psutil socket enumeration outside the main collector."""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections import defaultdict


def collect_inet_connections_once():
    """Return the existing per-PID network payload in a picklable shape."""
    import psutil

    grouped = defaultdict(list)
    for connection in psutil.net_connections(kind="inet"):
        if connection.pid:
            grouped[connection.pid].append(connection)

    snapshot = {}
    for pid, connections in grouped.items():
        remote_targets = []
        for connection in connections:
            if not connection.raddr:
                continue
            address = getattr(connection.raddr, "ip", connection.raddr[0])
            port = getattr(connection.raddr, "port", connection.raddr[1])
            remote_targets.append(f"{address}:{port}")
        snapshot[pid] = (
            len(connections),
            ",".join(remote_targets) if remote_targets else None,
        )
    return snapshot


def _connection_worker_main(send_connection, stop_receive, interval_seconds):
    try:
        while True:
            try:
                payload = {
                    "ok": True,
                    "snapshot": collect_inet_connections_once(),
                    "sampled_monotonic": time.monotonic(),
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "error": type(exc).__name__,
                    "sampled_monotonic": time.monotonic(),
                }
            try:
                send_connection.send(payload)
            except (BrokenPipeError, EOFError, OSError):
                return
            try:
                if stop_receive.poll(interval_seconds):
                    stop_receive.recv()
                    return
            except (BrokenPipeError, EOFError, OSError):
                return
    finally:
        try:
            send_connection.close()
        except OSError:
            pass
        try:
            stop_receive.close()
        except OSError:
            pass


class IsolatedConnectionSampler:
    """Restart a disposable psutil worker without risking the main engine."""

    def __init__(
        self,
        interval_seconds=2.0,
        stale_after_seconds=12.0,
        startup_timeout_seconds=8.0,
    ):
        self.interval_seconds = float(interval_seconds)
        self.stale_after_seconds = float(stale_after_seconds)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._process = None
        self._receive_connection = None
        self._stop_connection = None
        self._last_snapshot = {}
        self._last_success_monotonic = None
        self._worker_started_monotonic = None
        self._restart_count = 0
        self._last_exit_code = None
        self._last_error = None
        with self._lock:
            self._start_worker_locked()

    def _dispose_worker_locked(self, terminate):
        process = self._process
        receive_connection = self._receive_connection
        stop_connection = self._stop_connection

        if stop_connection is not None:
            try:
                stop_connection.send("stop")
            except (BrokenPipeError, EOFError, OSError):
                pass
        if process is not None:
            process.join(timeout=1.5)
            if terminate and process.is_alive():
                process.terminate()
                process.join(timeout=1.5)
            self._last_exit_code = process.exitcode
        if receive_connection is not None:
            try:
                receive_connection.close()
            except OSError:
                pass
        if stop_connection is not None:
            try:
                stop_connection.close()
            except OSError:
                pass

        self._process = None
        self._receive_connection = None
        self._stop_connection = None

    def _start_worker_locked(self):
        if self._process is not None:
            self._restart_count += 1
            self._dispose_worker_locked(terminate=True)

        receive_connection, send_connection = self._context.Pipe(duplex=False)
        stop_receive, stop_send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_connection_worker_main,
            args=(send_connection, stop_receive, self.interval_seconds),
            name="TimeAuditPsutilConnections",
            daemon=True,
        )
        process.start()
        send_connection.close()
        stop_receive.close()

        self._process = process
        self._receive_connection = receive_connection
        self._stop_connection = stop_send
        self._worker_started_monotonic = time.monotonic()
        self._last_success_monotonic = None

    def _drain_locked(self):
        connection = self._receive_connection
        if connection is None:
            return
        try:
            while connection.poll():
                payload = connection.recv()
                if payload.get("ok"):
                    self._last_snapshot = dict(payload.get("snapshot") or {})
                    self._last_success_monotonic = time.monotonic()
                    self._last_error = None
                else:
                    self._last_error = str(payload.get("error") or "unknown")
        except (EOFError, OSError):
            pass

    def snapshot(self):
        with self._lock:
            self._drain_locked()
            now = time.monotonic()
            process = self._process
            should_restart = process is None or not process.is_alive()
            if not should_restart and self._last_success_monotonic is None:
                should_restart = (
                    now - self._worker_started_monotonic
                    > self.startup_timeout_seconds
                )
            if not should_restart and self._last_success_monotonic is not None:
                should_restart = (
                    now - self._last_success_monotonic > self.stale_after_seconds
                )
            if should_restart:
                self._start_worker_locked()
            return dict(self._last_snapshot)

    def status(self):
        with self._lock:
            self._drain_locked()
            now = time.monotonic()
            return {
                "worker_pid": self._process.pid if self._process else None,
                "worker_alive": bool(self._process and self._process.is_alive()),
                "restart_count": self._restart_count,
                "last_exit_code": self._last_exit_code,
                "last_error": self._last_error,
                "last_success_age_seconds": (
                    None
                    if self._last_success_monotonic is None
                    else max(0.0, now - self._last_success_monotonic)
                ),
            }

    def close(self):
        with self._lock:
            self._dispose_worker_locked(terminate=True)
