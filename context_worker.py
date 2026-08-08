# -*- coding: utf-8 -*-
import ctypes
from ctypes import wintypes
import datetime
import asyncio
import psutil
import os
import re

from lifecycle_worker import check_process_elevation, check_file_signature, unknown_executable_path

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
        self.last_hwnd = None
        self.last_start_time = None
        self.active_slice = None
        self.pending_inserts = []
        self.pending_updates = []
        self.last_pid_key_map = {}
        # Keep the shared PID -> process_key cache shape intact for the
        # lifecycle worker.  This private companion cache is the proof that a
        # key belongs to the currently harvested process identity, rather than
        # merely to a PID that Windows may have reused.
        self._last_pid_identity_map = {}

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
            try:
                process_create_time = proc.create_time()
            except Exception:
                process_create_time = None
            
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
                "service_name": service_name, "is_elevated": is_elevated, "signature_status": signature_status,
                "process_create_time": process_create_time
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
                            "is_elevated": -1, "signature_status": sig_status,
                            "process_create_time": None
                        }
                finally:
                    kernel32.CloseHandle(handle)
        except Exception: pass

        return {
            "process_name": "Unknown_Protected_Process",
            "executable_path": unknown_executable_path("Unknown.exe"),
            "parent_process": None, "command_line": "", "service_name": None,
            "is_elevated": -1, "signature_status": 0,
            "process_create_time": None
        }

    @staticmethod
    def _metadata_identity(metadata):
        if not metadata:
            return None

        # create_time distinguishes two process instances that reused a PID;
        # the registry fields keep the fallback safe when create_time cannot be
        # read and mirror the database's process-key identity.
        return (
            metadata.get("process_create_time"),
            metadata.get("process_name"),
            metadata.get("executable_path"),
            metadata.get("parent_process"),
            metadata.get("command_line"),
            metadata.get("service_name"),
            metadata.get("is_elevated", 0),
            metadata.get("signature_status", 0),
        )

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

    def mark_sleep_boundary(self, boundary_ts):
        """【Bug3 修复】系统睡眠/休眠唤醒时由主控调用：把当前正聚焦的 slice 按"睡前最后清醒时刻"
        (boundary_ts) 截断关闭，使睡眠时长绝不被计入"前台聚焦时长"。否则用户合盖睡 8 小时后切窗，
        会把这 8 小时全算作"正在阅读该文档"，污染 TimeAudit 核心时长(已实证库内 max_duration≈24h)。
        关闭事件入队 pending_updates，由下一拍 poll_heartbeat 落库；同时清空 last_pid/last_title，
        令唤醒后开启一个全新的、不含睡眠的 slice。纯内存操作、无 DB 调用，可安全从事件循环线程调用。"""
        if self.active_slice:
            prev = self.active_slice
            end = boundary_ts
            if end < prev["timestamp"]:
                end = prev["timestamp"]   # 时钟异常兜底：duration 收敛为 0，绝不为负
            prev["end_timestamp"] = end
            prev["duration_ms"] = int((end - prev["timestamp"]).total_seconds() * 1000)
            self.pending_updates.append({
                "timestamp": prev["timestamp"], "os_pid": prev["os_pid"],
                "end_timestamp": prev["end_timestamp"], "duration_ms": prev["duration_ms"],
                "metadata": prev["metadata"], "process_key": prev.get("process_key")
            })
            self.active_slice = None
        self.last_pid = None
        self.last_title = None
        self.last_hwnd = None
        self.last_start_time = None

    async def poll_heartbeat(self, pool, timestamp=None):
        fast_info = self.check_foreground_window_fast()
        if not fast_info:
            return not self.pending_inserts and not self.pending_updates

        if (fast_info["os_pid"] == self.last_pid and
            fast_info["window_title"] == self.last_title and
            fast_info["hwnd"] == self.last_hwnd and
            not self.pending_inserts and 
            not self.pending_updates):
            return True

        if (
            fast_info["os_pid"] != self.last_pid
            or fast_info["window_title"] != self.last_title
            or fast_info["hwnd"] != self.last_hwnd
        ):
            now = timestamp if timestamp is not None else datetime.datetime.now(datetime.timezone.utc)
            
            if self.active_slice:
                prev = self.active_slice
                end_ts = now
                if end_ts < prev["timestamp"]:
                    end_ts = prev["timestamp"]
                prev["end_timestamp"] = end_ts
                prev["duration_ms"] = int((end_ts - prev["timestamp"]).total_seconds() * 1000)
                self.pending_updates.append({
                    "timestamp": prev["timestamp"], "os_pid": prev["os_pid"],
                    "end_timestamp": prev["end_timestamp"], "duration_ms": prev["duration_ms"],
                    "metadata": prev["metadata"], "process_key": prev.get("process_key")
                })
                self.active_slice = None

            metadata = await asyncio.to_thread(self.harvest_process_metadata, fast_info["os_pid"])
            process_identity = self._metadata_identity(metadata)
            process_key = None
            if (
                fast_info["os_pid"] == self.last_pid
                and process_identity is not None
                and self._last_pid_identity_map.get(fast_info["os_pid"]) == process_identity
            ):
                process_key = self.last_pid_key_map.get(fast_info["os_pid"])
            
            self.active_slice = {
                "timestamp": now, "os_pid": fast_info["os_pid"],
                "hwnd": fast_info["hwnd"],
                "window_title": fast_info["window_title"], "window_mode": fast_info["window_mode"],
                "metadata": metadata, "process_identity": process_identity,
                "process_key": process_key
            }
            self.pending_inserts.append(self.active_slice)
            
            print(f"[{datetime.datetime.now().strftime('%X')} 🖥️ 状态机] 聚焦切至 -> [{fast_info['os_pid']}] {fast_info['window_title'][:25]}")
            
            self.last_pid = fast_info["os_pid"]
            self.last_title = fast_info["window_title"]
            self.last_hwnd = fast_info["hwnd"]
            self.last_start_time = now

        if self.pending_inserts or self.pending_updates:
            staged_process_keys = []
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
                                    staged_process_keys.append(item)

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
                                # 【Bug6 修复】WHERE 必须带分区键 timestamp，命中主键 (timestamp, process_key, os_pid)
                                # 实现 O(1) 单分区定位。旧版缺 timestamp 会触发跨所有周/月分区的顺序扫描，且在
                                # Windows 复用 PID 时把几个月前同 (process_key, os_pid) 的幽灵 NULL 行误闭合，
                                # 产生"聚焦半年但 duration 仅几秒"的悖论脏数据。timestamp 即该 slice 开启时刻。
                                query = """
                                    UPDATE public.fact_process_context
                                    SET end_timestamp = $1, duration_ms = $2
                                    WHERE "timestamp" = $3 AND process_key = $4 AND os_pid = $5 AND end_timestamp IS NULL;
                                """
                                await conn.execute(query, item["end_timestamp"], item["duration_ms"], item["timestamp"], item["process_key"], item["os_pid"])
                                successful_updates.append(item)

                # A registry key created inside the transaction does not exist if
                # a later context insert rolls the transaction back. Publish keys
                # to in-memory caches only after the transaction commits.
                for item in staged_process_keys:
                    p_key = item["process_key"]
                    self.last_pid_key_map[item["os_pid"]] = p_key
                    self._last_pid_identity_map[item["os_pid"]] = item.get(
                        "process_identity",
                        self._metadata_identity(item.get("metadata")),
                    )
                    if self.active_slice is item:
                        self.active_slice["process_key"] = p_key

                for item in successful_inserts:
                    if item in self.pending_inserts:
                        self.pending_inserts.remove(item)
                for item in successful_updates:
                    if item in self.pending_updates:
                        self.pending_updates.remove(item)
                        
            except Exception:
                for item in staged_process_keys:
                    item["process_key"] = None
                # Keep the health signal payload-free: exception text from a DB
                # adapter can contain query arguments, including window metadata.
                print("⚠️ [🖥️ 状态机] 数仓写入挂起。")
                return False

        return not self.pending_inserts and not self.pending_updates
