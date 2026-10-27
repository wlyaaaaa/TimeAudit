"""Grafana API authentication via Password Center's existing native delivery."""
import base64
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

LOCAL_URL = "http://127.0.0.1:43000"
BROKER = Path(r"C:\ProgramData\PCConfig\AuthorityHost\tools\Invoke-PasswordCenterVendor.ps1")


def _receive_header():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPVOID]
    kernel.CreateNamedPipeW.restype = w.HANDLE
    kernel.ConnectNamedPipe.argtypes = [w.HANDLE, w.LPVOID]
    kernel.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    name = "passwordcenter-vendor-" + uuid.uuid4().hex
    handle = kernel.CreateNamedPipeW("\\\\.\\pipe\\" + name, 1, 1, 1, 65536, 65536, 0, None)
    if handle == w.HANDLE(-1).value:
        raise RuntimeError("密码中心 Grafana 接收管道创建失败。")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="timeaudit-grafana-auth-") as directory:
            request = Path(directory, "request.json")
            request.write_text(json.dumps({"schema": "passwordcenter.vendor-request.v1", "vendor": "grafana",
                "mode": "deliver", "endpoint": LOCAL_URL, "consumer_pid": os.getpid(), "pipe_name": name}), encoding="utf-8")
            process = subprocess.Popen(["pwsh", "-NoProfile", "-File", str(BROKER), "-Vendor", "grafana",
                "-RequestPath", str(request), "-NativeClient", "-Json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 40
            raw = bytearray()
            buffer, count = ctypes.create_string_buffer(65536), w.DWORD()
            while time.monotonic() < deadline:
                kernel.ConnectNamedPipe(handle, None)
                if kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None) and count.value:
                    raw.extend(buffer.raw[:count.value])
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            stdout, _ = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
            if kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None) and count.value:
                raw.extend(buffer.raw[:count.value])
            receipt = json.loads(stdout.decode("utf-8-sig"))
            header = raw.decode("utf-8")
            if process.returncode or receipt.get("status") != "pass" or not header.startswith("Basic ") or any(c in header for c in "\r\n"):
                raise ValueError("delivery failed")
            return header
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError("密码中心 Grafana 账号交付失败；请检查登记与正式安装。") from None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        kernel.CloseHandle(handle)


def authorization_header(base_url, user=None):
    password = os.environ.get("GRAFANA_PASSWORD")
    user = user or os.environ.get("GRAFANA_USER")
    if user and password:
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    if base_url.rstrip("/") != LOCAL_URL:
        raise RuntimeError("密码中心 Grafana 账号仅登记给 http://127.0.0.1:43000。")
    return _receive_header()
