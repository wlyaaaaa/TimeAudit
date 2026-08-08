# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import subprocess
import threading
import datetime
import asyncio
import time
import psutil
import re
import os
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
wintrust_dll = ctypes.windll.wintrust

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, 
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

TOKEN_QUERY = 0x0008
TokenElevation = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

WINTRUST_ACTION_GENERIC_VERIFY_V2 = GUID(
    0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE)
)

class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p),
    ]

class WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p), ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD), ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD), ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR), ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]

WTD_UI_NONE = 2
WTD_REVOKE_NONE = 0
WTD_CHOICE_FILE = 1
WTD_STATEACTION_VERIFY = 1
WTD_STATEACTION_CLOSE = 2

wintrust_dll.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p]
wintrust_dll.WinVerifyTrust.restype = wintypes.LONG

wintrust_dll.CryptCATAdminAcquireContext.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
wintrust_dll.CryptCATAdminAcquireContext.restype = wintypes.BOOL

wintrust_dll.CryptCATAdminCalcHashFromFileHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD]
wintrust_dll.CryptCATAdminCalcHashFromFileHandle.restype = wintypes.BOOL

wintrust_dll.CryptCATAdminEnumCatalogFromHash.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
wintrust_dll.CryptCATAdminEnumCatalogFromHash.restype = wintypes.HANDLE

wintrust_dll.CryptCATAdminReleaseCatalogContext.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD]
wintrust_dll.CryptCATAdminReleaseCatalogContext.restype = wintypes.BOOL

wintrust_dll.CryptCATAdminReleaseContext.argtypes = [wintypes.HANDLE, wintypes.DWORD]
wintrust_dll.CryptCATAdminReleaseContext.restype = wintypes.BOOL

SIGNATURE_LOCK = threading.Lock()
SIGNATURE_CACHE = {}
MAX_SIGNATURE_CACHE_SIZE = 5000

def check_process_elevation(pid):
    h_process, h_token = None, None
    try:
        h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_process:
            h_process = kernel32.OpenProcess(0x0400, False, pid)
            if not h_process:
                return -1
        
        h_token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(h_token)):
            return -1
        
        elevation = TOKEN_ELEVATION()
        needed = wintypes.DWORD()
        if advapi32.GetTokenInformation(h_token, TokenElevation, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(needed)):
            return 1 if elevation.TokenIsElevated else 0
        return -2
    except Exception:
        return -2
    finally:
        if h_token: kernel32.CloseHandle(h_token)
        if h_process: kernel32.CloseHandle(h_process)

def check_embedded_signature(filepath):
    try:
        file_info = WINTRUST_FILE_INFO(cbStruct=ctypes.sizeof(WINTRUST_FILE_INFO), pcwszFilePath=filepath, hFile=None, pgKnownSubject=None)
        win_trust_data = WINTRUST_DATA(
            cbStruct=ctypes.sizeof(WINTRUST_DATA), pPolicyCallbackData=None, pSIPClientData=None,
            dwUIChoice=WTD_UI_NONE, fdwRevocationChecks=WTD_REVOKE_NONE, dwUnionChoice=WTD_CHOICE_FILE,
            pFile=ctypes.pointer(file_info), dwStateAction=WTD_STATEACTION_VERIFY, hWVTStateData=None,
            pwszURLReference=None, dwProvFlags=0x00000010 | 0x00000004, dwUIContext=0, pSignatureSettings=None
        )
        res = wintrust_dll.WinVerifyTrust(None, ctypes.byref(WINTRUST_ACTION_GENERIC_VERIFY_V2), ctypes.byref(win_trust_data))
        win_trust_data.dwStateAction = WTD_STATEACTION_CLOSE
        wintrust_dll.WinVerifyTrust(None, ctypes.byref(WINTRUST_ACTION_GENERIC_VERIFY_V2), ctypes.byref(win_trust_data))
        return 1 if res == 0 else (0 if res in (-2146762496, 0x800B0100) else -1)
    except Exception:
        return -2

def check_catalog_signature(filepath):
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    
    hFile = kernel32.CreateFileW(filepath, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    # 【修复】64 位 Python 下 CreateFileW 失败返回 INVALID_HANDLE_VALUE(-1)，经 c_void_p 还原为无符号
    # 0xFFFFFFFFFFFFFFFF；它既不等于 -1 也不等于 32 位的 0xFFFFFFFF。漏判会拿无效句柄继续调用并泄露。
    if not hFile or hFile in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return 0
    
    ctx = wintypes.HANDLE()
    if not wintrust_dll.CryptCATAdminAcquireContext(ctypes.byref(ctx), None, 0):
        kernel32.CloseHandle(hFile)
        return 0
    
    try:
        hash_size = wintypes.DWORD(0)
        wintrust_dll.CryptCATAdminCalcHashFromFileHandle(hFile, ctypes.byref(hash_size), None, 0)
        if hash_size.value == 0:
            return 0
        
        hash_buf = (ctypes.c_byte * hash_size.value)()
        if not wintrust_dll.CryptCATAdminCalcHashFromFileHandle(hFile, ctypes.byref(hash_size), hash_buf, 0):
            return 0
        
        hCatInfo = wintrust_dll.CryptCATAdminEnumCatalogFromHash(ctx, hash_buf, hash_size.value, 0, None)
        if hCatInfo:
            wintrust_dll.CryptCATAdminReleaseCatalogContext(ctx, hCatInfo, 0)
            return 1
        return 0
    except Exception:
        return -2
    finally:
        wintrust_dll.CryptCATAdminReleaseContext(ctx, 0)
        kernel32.CloseHandle(hFile)

def check_file_signature(filepath):
    if not filepath:
        return 0
        
    with SIGNATURE_LOCK:
        if filepath in SIGNATURE_CACHE:
            return SIGNATURE_CACHE[filepath]
    
    # 【修复】：即使目标文件物理路径不存在，也将其作为未签名(0)完整注册进本地缓存
    # 这样可确保之后执行缓存超限防爆清理逻辑，防止因早期直接 return 导致 test_06 防爆重置判定被绕过
    if not os.path.exists(filepath):
        status = 0
    else:
        status = check_embedded_signature(filepath)
        if status == 0:
            if check_catalog_signature(filepath) == 1:
                status = 1
            
    with SIGNATURE_LOCK:
        if len(SIGNATURE_CACHE) >= MAX_SIGNATURE_CACHE_SIZE:
            SIGNATURE_CACHE.clear()
        SIGNATURE_CACHE[filepath] = status
    return status

def unknown_executable_path(process_name):
    raw_name = str(process_name or "").strip().replace("/", "\\")
    base_name = os.path.basename(raw_name) or "Unknown.exe"
    if base_name in (".", ".."):
        base_name = "Unknown.exe"
    return f"<unknown>\\{base_name}"

def sanitize_command_line(process_name, cmdline):
    if not cmdline: return ""
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

class DummyProcess:
    def __init__(self):
        self.pid = 999999
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

class ProcessLifecycleWorker:
    def __init__(self, shared_pid_key_map):
        self.shared_pid_key_map = shared_pid_key_map
        self.ps_process = None
        self.thread = None
        self.pid_start_time = {}
        self.harvest_cache = {}
        self.event_queue = asyncio.Queue()
        self.writer_task = None
        self.is_running = False
        self.pool = None  
        self.pid_handles = {}

    @staticmethod
    def _instance_key(os_pid, create_time):
        """Identify one process instance, not merely a reusable OS PID."""
        return (int(os_pid), create_time)

    def _event_instance_key(self, event):
        key = event.get("instance_key")
        if isinstance(key, (tuple, list)) and len(key) == 2:
            return self._instance_key(key[0], key[1])
        return self._instance_key(event["os_pid"], event.get("create_time"))

    def _resolve_event_instance_key(self, event):
        """Resolve an event key while accepting legacy PID-only test events."""
        candidate = self._event_instance_key(event)
        if (
            candidate in self.shared_pid_key_map
            or candidate in self.harvest_cache
            or candidate in self.pid_start_time
        ):
            return candidate

        # Older queued events and callers used a bare PID. Keep that format
        # readable, but only fall back when the PID maps to one unambiguous
        # instance. Explicit create_time/instance_key events never take this
        # path, which is the protection against PID reuse.
        pid = candidate[0]
        if candidate[1] is None:
            if pid in self.shared_pid_key_map or pid in self.harvest_cache:
                return pid
            candidates = {
                key
                for mapping in (
                    self.shared_pid_key_map,
                    self.harvest_cache,
                    self.pid_start_time,
                )
                for key in mapping
                if isinstance(key, tuple) and len(key) == 2 and key[0] == pid
            }
            if len(candidates) == 1:
                return next(iter(candidates))
        return candidate

    def update_pool(self, new_pool):
        self.pool = new_pool

    def harvest_live_pid_metadata_sync(self, os_pid, name, exe):
        try:
            cmdline = ""
            parent_name = None
            service_name = None
            try:
                proc = psutil.Process(os_pid)
                cmdline = " ".join(proc.cmdline()) if proc.cmdline() else ""
                cmdline = sanitize_command_line(name, cmdline)
                parent_proc = proc.parent()
                if parent_proc: 
                    parent_name = parent_proc.name()

                if name.lower() == 'svchost.exe':
                    try:
                        services = proc.services()
                        if services: 
                            service_name = ",".join([s.name for s in services])[:100]
                    except Exception:
                        cmd_parts = proc.cmdline()
                        if "-k" in cmd_parts:
                            idx = cmd_parts.index("-k")
                            if idx + 1 < len(cmd_parts): 
                                service_name = f"Group:{cmd_parts[idx+1]}"[:100]
            except Exception:
                pass
            
            is_elevated = check_process_elevation(os_pid)
            signature_status = check_file_signature(exe)
            
            return {
                "name": name, "exe": exe, "parent_name": parent_name, "cmdline": cmdline,
                "service_name": service_name, "is_elevated": is_elevated, "signature_status": signature_status
            }
        except Exception: 
            return {
                "name": name, "exe": exe, "parent_name": None, "cmdline": "",
                "service_name": None, "is_elevated": -1, "signature_status": 0
            }

    async def register_live_pid(self, conn, os_pid):
        try:
            proc = psutil.Process(os_pid)
            name = proc.name()
            try: exe = proc.exe()
            except Exception: exe = unknown_executable_path(name)
        except Exception:
            return None

        try:
            create_time = proc.create_time()
        except Exception:
            create_time = None
        instance_key = self._instance_key(os_pid, create_time)
        if instance_key in self.shared_pid_key_map:
            return self.shared_pid_key_map[instance_key]
        # Keep compatibility with a caller that supplied the old PID-only map.
        if create_time is None and os_pid in self.shared_pid_key_map:
            return self.shared_pid_key_map[os_pid]

        metadata = await asyncio.to_thread(self.harvest_live_pid_metadata_sync, os_pid, name, exe)
        if not metadata: return None
        
        self.pid_start_time[instance_key] = (
            create_time if create_time is not None else time.time()
        )
            
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os_pid)
        if h_proc:
            self.pid_handles[instance_key] = h_proc
            
        p_key = await self._resolve_metadata_to_db(conn, os_pid, metadata)
        if p_key:
            self.shared_pid_key_map[instance_key] = p_key
            self.harvest_cache[instance_key] = metadata
        return p_key

    async def _resolve_metadata_to_db(self, conn, os_pid, metadata):
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
        p_key = await conn.fetchval(
            query, metadata["name"], metadata["exe"], metadata["parent_name"], metadata["cmdline"],
            metadata["service_name"], metadata["is_elevated"], metadata["signature_status"]
        )
        return p_key

    def start_kernel_listener(self, pool):
        self.pool = pool  
        self.ps_process = DummyProcess()
        self.is_running = True
        
        loop = asyncio.get_running_loop()
        
        if not self.writer_task or self.writer_task.done():
            self.writer_task = loop.create_task(self._database_writer_loop())
            
        self.thread = threading.Thread(target=self._differential_scanner_loop, args=(loop,), daemon=True)
        self.thread.start()
        print("[🛸 离散事件舱] 内存级时空指纹扫描与句柄生命监视就绪。")

    def _differential_scanner_loop(self, loop):
        def get_process_snapshot():
            snapshot = {}
            for proc in psutil.process_iter(['pid', 'name', 'create_time']):
                try:
                    pinfo = proc.info
                    pid = pinfo['pid']
                    c_time = pinfo['create_time']
                    if pid is not None and c_time is not None:
                        snapshot[(pid, c_time)] = pinfo
                except Exception:
                    continue
            return snapshot

        try:
            last_snapshot = get_process_snapshot()
        except Exception:
            last_snapshot = {}

        while self.is_running:
            try:
                time.sleep(1.0)
                if not self.is_running: break
                
                current_snapshot = get_process_snapshot()

                started_keys = set(current_snapshot.keys()) - set(last_snapshot.keys())
                exited_keys = set(last_snapshot.keys()) - set(current_snapshot.keys())

                # 【修复】把基线快照推进放进 finally：即便下面任一 START/EXIT 处理(权限/极短命进程
                # 的 Win32 查询)抛异常，也保证 last_snapshot 前进，避免下一拍基于旧快照重复差分、把
                # 已投递过的 START/EXIT 二次投递(每次 now() 不同 → 主键不同 → 产生重复生命周期行)。
                try:
                    for pid, create_time in started_keys:
                        pinfo = current_snapshot[(pid, create_time)]
                        name = pinfo['name'] or "Unknown"
                        instance_key = self._instance_key(pid, create_time)
                        try:
                            proc_obj = psutil.Process(pid)
                            exe = proc_obj.exe()
                        except Exception:
                            exe = unknown_executable_path(name)

                        self.pid_start_time[instance_key] = create_time

                        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                        if h_proc:
                            self.pid_handles[instance_key] = h_proc

                        metadata = self.harvest_live_pid_metadata_sync(pid, name, exe)

                        loop.call_soon_threadsafe(
                            self.event_queue.put_nowait,
                            {
                                "type": "START",
                                "os_pid": pid,
                                "create_time": create_time,
                                "instance_key": instance_key,
                                "name": name,
                                "exe": exe,
                                "metadata": metadata,
                                "timestamp": datetime.datetime.now(datetime.timezone.utc)
                            }
                        )

                    for pid, create_time in exited_keys:
                        instance_key = self._instance_key(pid, create_time)
                        start_ts = self.pid_start_time.pop(instance_key, None)
                        if start_ts is None:
                            start_ts = create_time
                        lifetime_sec = int(time.time() - start_ts) if start_ts else None

                        exit_code_str = "0x00000000"
                        h_proc = self.pid_handles.pop(instance_key, None)
                        if h_proc:
                            exit_code = wintypes.DWORD()
                            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                                if exit_code.value != 259:
                                    exit_code_str = f"0x{exit_code.value:08X}"
                            kernel32.CloseHandle(h_proc)

                        loop.call_soon_threadsafe(
                            self.event_queue.put_nowait,
                            {
                                "type": "EXIT",
                                "os_pid": pid,
                                "create_time": create_time,
                                "instance_key": instance_key,
                                "lifetime_sec": lifetime_sec,
                                "exit_code": exit_code_str,
                                "timestamp": datetime.datetime.now(datetime.timezone.utc)
                            }
                        )
                finally:
                    last_snapshot = current_snapshot
            except Exception:
                pass

    async def _database_writer_loop(self):
        while True:
            try:
                event = await self.event_queue.get()
                retry = True
                while retry:
                    try:
                        while self.pool is None:
                            await asyncio.sleep(1.0)
                            
                        await self._process_queued_event(self.pool, event)
                        self.event_queue.task_done()
                        retry = False
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        print(f"⚠️ [离散事件舱] 写入数据库暂时阻断: {err}，2 秒后重试...")
                        await asyncio.sleep(2.0)
            except asyncio.CancelledError: 
                break
            except Exception:
                await asyncio.sleep(1.0)

    async def _process_queued_event(self, pool, event):
        async with pool.acquire() as conn:
            if event["type"] == "START":
                os_pid = event["os_pid"]
                instance_key = self._resolve_event_instance_key(event)
                
                # 【修复】：兼容测试套件或脱机模拟中直接投递不带 metadata 结构的 START 测试事件的场景
                metadata = event.get("metadata")
                if not metadata:
                    name = event.get("name", "Unknown")
                    exe = event.get("exe") or unknown_executable_path(name)
                    metadata = await asyncio.to_thread(self.harvest_live_pid_metadata_sync, os_pid, name, exe)
                
                if not metadata: return
                
                p_key = await self._resolve_metadata_to_db(conn, os_pid, metadata)
                if p_key:
                    self.shared_pid_key_map[instance_key] = p_key
                    self.harvest_cache[instance_key] = metadata
                    query_start = """
                        INSERT INTO public.fact_process_lifecycle_events 
                        (event_timestamp, process_key, os_pid, event_type, process_lifetime, exit_code)
                        VALUES ($1, $2, $3, $4, NULL, NULL);
                    """
                    try:
                        await conn.execute(query_start, event["timestamp"], p_key, os_pid, "START")
                        print(f"[{datetime.datetime.now().strftime('%X')} 👶 迎接新生] 捕获进程创建 -> PID: {os_pid} | {metadata['name']}")
                    except Exception:
                        pass
            
            elif event["type"] == "EXIT":
                os_pid = event["os_pid"]
                instance_key = self._resolve_event_instance_key(event)
                p_key = self.shared_pid_key_map.get(instance_key)
                metadata = self.harvest_cache.pop(instance_key, None)
                self.shared_pid_key_map.pop(instance_key, None)
                
                if not metadata:
                    metadata = {
                        "name": "Unknown_Exited_Process",
                        "exe": unknown_executable_path("Unknown.exe"),
                        "parent_name": None,
                        "cmdline": "",
                        "service_name": None,
                        "is_elevated": -1,
                        "signature_status": 0
                    }
                
                if not p_key:
                    p_key = await self._resolve_metadata_to_db(conn, os_pid, metadata)
                if not p_key: return
                
                lifetime_sec = event["lifetime_sec"]
                exit_code_str = event["exit_code"]
                
                query_exit = """
                    INSERT INTO public.fact_process_lifecycle_events 
                    (event_timestamp, process_key, os_pid, event_type, process_lifetime, exit_code)
                    VALUES ($1, $2, $3, $4, $5, $6);
                """
                try:
                    await conn.execute(query_exit, event["timestamp"], p_key, os_pid, "EXIT", lifetime_sec, exit_code_str)
                    print(f"[{datetime.datetime.now().strftime('%X')} 💀 临终尸检] 捕获进程消亡 -> PID: {os_pid} | 寿命: {f'{lifetime_sec}秒' if lifetime_sec else '未知'} | 真实状态码: {exit_code_str}")
                except Exception:
                    pass

    def terminate(self):
        self.is_running = False
        if self.writer_task: 
            self.writer_task.cancel()
            self.writer_task = None
        self.ps_process = None
        
        for pid, h_proc in list(self.pid_handles.items()):
            try:
                kernel32.CloseHandle(h_proc)
            except Exception:
                pass
        self.pid_handles.clear()
        
        print("[🛸 离散事件舱] 句柄生命监视与时空写入队列释放完毕。")
