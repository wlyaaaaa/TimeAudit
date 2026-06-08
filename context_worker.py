# -*- coding: utf-8 -*-
import ctypes
from ctypes import wintypes
import datetime
import asyncio
import psutil
import os
import re

from lifecycle_worker import check_process_elevation, check_file_signature

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

try:
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
except Exception:
    pass

def sanitize_command_line(process_name, cmdline):
    if not cmdline:
        return ""
    proc_lower = process_name.lower()
    
    if any(x in proc_lower for x in ["chrome", "msedge", "browser"]):
        cmdline = re.sub(r'--mojo-platform-channel-handle=\d+', '--mojo-platform-channel-handle=<handle>', cmdline)
        cmdline = re.sub(r'--renderer-client-id=\d+', '--renderer-client-id=<client-id>', cmdline)
        cmdline = re.sub(r'--field-trial-handle=[\d,a-zA-Z]+', '--field-trial-handle=<handle>', cmdline)
        cmdline = re.sub(r'--metrics-shmem-handle=[\d,a-zA-Z]+', '--metrics-shmem-handle=<handle>', cmdline)
        cmdline = re.sub(r'--pseudonymization-salt-handle=[\d,a-zA-Z]+', '--pseudonymization-salt-handle=<handle>', cmdline)
        cmdline = re.sub(r'--trace-process-track-uuid=\d+', '--trace-process-track-uuid=<uuid>', cmdline)
        cmdline = re.sub(r'--launch-time-ticks=\d+', '--launch-time-ticks=<ticks>', cmdline)
        cmdline = re.sub(r'--time-ticks-at-unix-epoch=-\d+', '--time-ticks-at-unix-epoch=<ticks>', cmdline)
    elif "multitip" in proc_lower or "360" in proc_lower:
        cmdline = re.sub(r'/package=[a-f0-9]{32}', '/package=<md5-package>', cmdline)
        cmdline = re.sub(r'/Message=[a-zA-Z0-9+/=]+', '/Message=<base64-message>', cmdline)
        cmdline = re.sub(r'/adpopid=[a-f0-9]{32}', '/adpopid=<md5-adpopid>', cmdline)
        
    return cmdline

class WindowStateTracker:
    def __init__(self):
        self.last_pid = None
        self.last_title = None
        self.last_start_time = None
        self.active_slice = None
        self.pending_inserts = []
        self.pending_updates = []
        self.last_pid_key_map = {}

    @property
    def last_process_key(self):
        if self.active_slice:
            return self.active_slice.get("process_key") or True
        return None

    def check_foreground_window_fast(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        os_pid = pid.value

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        window_title = buf.value

        window_mode = 2 if (user32.GetWindowLongW(hwnd, -16) & 0x01000000) else 3
        return {"hwnd": hwnd, "os_pid": os_pid, "window_title": window_title, "window_mode": window_mode}

    def harvest_process_metadata(self, os_pid):
        try:
            proc = psutil.Process(os_pid)
            process_name = proc.name()
            executable_path = proc.exe()
            command_line = " ".join(proc.cmdline()) if proc.cmdline() else ""
            command_line = sanitize_command_line(process_name, command_line)
            
            parent_process = None
            try:
                parent_proc = proc.parent()
                if parent_proc: parent_process = parent_proc.name()
            except Exception: pass

            service_name = None
            if process_name.lower() == 'svchost.exe':
                try:
                    services = proc.services()
                    if services: service_name = ",".join([s.name for s in services])[:100]
                except Exception:
                    cmd_parts = proc.cmdline()
                    if "-k" in cmd_parts:
                        idx = cmd_parts.index("-k")
                        if idx + 1 < len(cmd_parts): service_name = f"Group:{cmd_parts[idx+1]}"[:100]
            
            is_elevated = check_process_elevation(os_pid)
            signature_status = check_file_signature(executable_path)

            return {
                "process_name": process_name, "executable_path": executable_path,
                "parent_process": parent_process, "command_line": command_line,
                "service_name": service_name, "is_elevated": is_elevated, "signature_status": signature_status
            }
        except Exception: pass

        try:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os_pid)
            if handle:
                try:
                    size = ctypes.c_ulong(1024)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        full_path = buf.value
                        proc_name = os.path.basename(full_path)
                        sig_status = check_file_signature(full_path)
                        return {
                            "process_name": proc_name, "executable_path": full_path,
                            "parent_process": None, "command_line": "", "service_name": None,
                            "is_elevated": -1, "signature_status": sig_status
                        }
                finally:
                    kernel32.CloseHandle(handle)
        except Exception: pass

        return {
            "process_name": "Unknown_Protected_Process",
            "executable_path": "C:\\Windows\\System32\\Unknown.exe",
            "parent_process": None, "command_line": "", "service_name": None,
            "is_elevated": -1, "signature_status": 0
        }

    async def get_or_register_metadata_slow(self, conn, metadata):
        if not metadata:
            return None
        
        query = """
            INSERT INTO public.dim_process_registry 
            (process_name, executable_path, parent_process, command_line, service_name, is_elevated, signature_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (
                process_name, md5(executable_path), COALESCE(parent_process, ''::character varying), 
                md5(COALESCE(command_line, ''::text)), COALESCE(service_name, ''::character varying), 
                is_elevated, signature_status
            )
            DO UPDATE SET process_name = EXCLUDED.process_name
            RETURNING process_key;
        """
        return await conn.fetchval(
            query,
            metadata["process_name"],
            metadata["executable_path"],
            metadata["parent_process"],
            metadata["command_line"],
            metadata["service_name"],
            metadata.get("is_elevated", 0),
            metadata.get("signature_status", 0)
        )

    async def poll_heartbeat(self, pool, timestamp=None):
        fast_info = self.check_foreground_window_fast()
        if not fast_info: return

        if (fast_info["os_pid"] == self.last_pid and 
            fast_info["window_title"] == self.last_title and 
            not self.pending_inserts and 
            not self.pending_updates):
            return

        if fast_info["os_pid"] != self.last_pid or fast_info["window_title"] != self.last_title:
            now = timestamp if timestamp is not None else datetime.datetime.now(datetime.timezone.utc)
            
            if self.active_slice:
                prev = self.active_slice
                prev["end_timestamp"] = now
                prev["duration_ms"] = int((now - prev["timestamp"]).total_seconds() * 1000)
                self.pending_updates.append({
                    "timestamp": prev["timestamp"], "os_pid": prev["os_pid"],
                    "end_timestamp": prev["end_timestamp"], "duration_ms": prev["duration_ms"],
                    "metadata": prev["metadata"], "process_key": prev.get("process_key")
                })
                self.active_slice = None

            metadata = await asyncio.to_thread(self.harvest_process_metadata, fast_info["os_pid"])
            
            self.active_slice = {
                "timestamp": now, "os_pid": fast_info["os_pid"],
                "window_title": fast_info["window_title"], "window_mode": fast_info["window_mode"],
                "metadata": metadata, "process_key": self.last_pid_key_map.get(fast_info["os_pid"])
            }
            self.pending_inserts.append(self.active_slice)
            
            print(f"[{datetime.datetime.now().strftime('%X')} 🖥️ 状态机] 聚焦切至 -> [{fast_info['os_pid']}] {fast_info['window_title'][:25]}")
            
            self.last_pid = fast_info["os_pid"]
            self.last_title = fast_info["window_title"]
            self.last_start_time = now

        if self.pending_inserts or self.pending_updates:
            try:
                successful_inserts = []
                successful_updates = []
                
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for item in self.pending_inserts + self.pending_updates:
                            if item.get("process_key") is None and item.get("metadata"):
                                p_key = await self.get_or_register_metadata_slow(conn, item["metadata"])
                                if p_key:
                                    item["process_key"] = p_key
                                    self.last_pid_key_map[item["os_pid"]] = p_key
                                    if (self.active_slice and 
                                        self.active_slice["timestamp"] == item.get("timestamp") and 
                                        self.active_slice["os_pid"] == item.get("os_pid")):
                                        self.active_slice["process_key"] = p_key

                        for item in self.pending_inserts:
                            if item.get("process_key"):
                                # 【修复】：将 True 改回物理整型 1，解决 Smallint 字段类型不兼容引发的事务静默回滚
                                query = """
                                    INSERT INTO public.fact_process_context 
                                    ("timestamp", process_key, os_pid, is_foreground, window_title, window_mode)
                                    VALUES ($1, $2, $3, 1, $4, $5)
                                    ON CONFLICT DO NOTHING;
                                """
                                await conn.execute(query, item["timestamp"], item["process_key"], item["os_pid"], item["window_title"], item["window_mode"])
                                successful_inserts.append(item)

                        for item in self.pending_updates:
                            if item.get("process_key"):
                                query = """
                                    UPDATE public.fact_process_context 
                                    SET end_timestamp = $1, duration_ms = $2 
                                    WHERE process_key = $3 AND os_pid = $4 AND end_timestamp IS NULL;
                                """
                                await conn.execute(query, item["end_timestamp"], item["duration_ms"], item["process_key"], item["os_pid"])
                                successful_updates.append(item)
                
                for item in successful_inserts:
                    if item in self.pending_inserts:
                        self.pending_inserts.remove(item)
                for item in successful_updates:
                    if item in self.pending_updates:
                        self.pending_updates.remove(item)
                        
            except Exception as e:
                print(f"⚠️ [🖥️ 状态机] 数仓写入挂起: {e}")