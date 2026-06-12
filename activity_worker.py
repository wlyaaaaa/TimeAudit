# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="pynvml")
warnings.filterwarnings("ignore", category=FutureWarning)

import datetime
import asyncio
import sys
import ctypes
from ctypes import wintypes
import psutil
import pynvml
import re
import threading
import time

from lifecycle_worker import check_process_elevation, check_file_signature

class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p), 
    ]

class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]

try:
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtQuerySystemInformation.argtypes = [
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
    ]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    HAS_NTDLL = True
except Exception:
    HAS_NTDLL = False

def fetch_system_processes():
    if not HAS_NTDLL or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
        
    SystemProcessInformation = 5
    buffer_size = 512 * 1024
    
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        return_len = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(
            SystemProcessInformation, buffer, buffer_size, ctypes.byref(return_len)
        )
        if status == 0:
            break
        elif status == -1073741820 or status == 0xC0000004 or (return_len.value > buffer_size):
            buffer_size = max(return_len.value + 65536, buffer_size * 2)
            if buffer_size > 16 * 1024 * 1024: 
                return None
            continue
        else:
            return None
        
    processes = []
    offset = 0
    try:
        while True:
            spi_ptr = ctypes.cast(ctypes.addressof(buffer) + offset, ctypes.POINTER(SYSTEM_PROCESS_INFORMATION))
            spi = spi_ptr.contents
            
            name = "Idle"
            if spi.ImageName.Length > 0 and spi.ImageName.Buffer:
                try:
                    name = ctypes.string_at(spi.ImageName.Buffer, spi.ImageName.Length).decode('utf-16le', errors='ignore')
                except Exception:
                    pass
                    
            pid = int(spi.UniqueProcessId) if spi.UniqueProcessId else 0
            ppid = int(spi.InheritedFromUniqueProcessId) if spi.InheritedFromUniqueProcessId else 0
            
            user_sec = spi.UserTime / 10000000.0
            kernel_sec = spi.KernelTime / 10000000.0
            cpu_time = user_sec + kernel_sec
            
            create_time = 0.0
            if spi.CreateTime > 0:
                create_time = (spi.CreateTime - 116444736000000000) / 10000000.0
                
            processes.append({
                "pid": pid,
                "ppid": ppid,
                "name": name,
                "cpu_time": cpu_time,
                "r_bytes": spi.ReadTransferCount,
                "w_bytes": spi.WriteTransferCount,
                "other_bytes": spi.OtherTransferCount,
                "r_ops": spi.ReadOperationCount,
                "w_ops": spi.WriteOperationCount,
                "ram_mb": int(spi.WorkingSetSize / (1024 * 1024)),
                "threads": spi.NumberOfThreads,
                "create_time": create_time,
                "is_fallback": False
            })
            
            if spi.NextEntryOffset == 0:
                break
            offset += spi.NextEntryOffset
    except Exception:
        return None
        
    return processes

class PDH_FMT_COUNTERVALUE_DOUBLE(ctypes.Structure):
    _fields_ = [
        ("CStatus", ctypes.c_ulong),
        ("doubleValue", ctypes.c_double)
    ]
    
class PDH_FMT_COUNTERVALUE_ITEM_DOUBLE(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPCWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE_DOUBLE)
    ]

def try_enable_debug_privilege():
    try:
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        SE_PRIVILEGE_ENABLED = 0x00000002
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]
        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]
        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]
        hToken = wintypes.HANDLE()
        if advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(hToken)):
            luid = LUID()
            if advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
                tp = TOKEN_PRIVILEGES()
                tp.PrivilegeCount = 1
                tp.Privileges[0].Luid = luid
                tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
                advapi32.AdjustTokenPrivileges(hToken, False, ctypes.byref(tp), 0, None, None)
            kernel32.CloseHandle(hToken)
    except: 
        pass

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

class ProcessActivityWorker:
    def __init__(self):
        self.key_cache = {}      
        self.io_delta_cache = {} 
        self.path_elevation_cache = {} 
        self.nvml_initialized = False
        self.gpu_handle = None
        
        self.cpu_time_cache = {}
        self.affinity_cache = {}
        self.pid_key_cache = {}
        
        self.cache_lock = threading.Lock()
        self.net_conn_cache = {}      
        self.shared_vram_cache = {}    
        
        self.vram_map_cache = {}
        self.gpu_util_map_cache = {}
        
        self.last_net_bytes_sent = 0
        self.last_net_bytes_recv = 0
        self.last_net_time = time.time()
        self.system_net_send_rate = 0.0
        self.system_net_recv_rate = 0.0
        
        self._init_nvml()
        try_enable_debug_privilege()
        
        self.bg_thread = threading.Thread(target=self._background_telemetry_loop, daemon=True)
        self.bg_thread.start()

    def _init_nvml(self):
        try:
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nvml_initialized = True
        except:
            self.nvml_initialized = False

    @staticmethod
    def _pdh_add_counter(pdh, query, path):
        # 优先使用英文计数器接口，免疫系统显示语言本地化导致的路径失配
        try:
            h = wintypes.HANDLE()
            if pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(h)) == 0:
                return h
        except Exception:
            pass
        try:
            h = wintypes.HANDLE()
            if pdh.PdhAddCounterW(query, path, None, ctypes.byref(h)) == 0:
                return h
        except Exception:
            pass
        return None

    @staticmethod
    def _read_pdh_pid_array(pdh, h_counter):
        results = []
        if not h_counter:
            return results
        try:
            buffer_size = wintypes.DWORD(0)
            item_count = wintypes.DWORD(0)
            PDH_FMT_DOUBLE = 0x00000200
            res = pdh.PdhGetFormattedCounterArrayW(
                h_counter, PDH_FMT_DOUBLE, ctypes.byref(buffer_size), ctypes.byref(item_count), None
            )
            if (res == 0x800007D2 or res == 0) and buffer_size.value > 0:
                buffer = ctypes.create_string_buffer(buffer_size.value)
                res = pdh.PdhGetFormattedCounterArrayW(
                    h_counter, PDH_FMT_DOUBLE, ctypes.byref(buffer_size), ctypes.byref(item_count), buffer
                )
                if res == 0 and item_count.value > 0:
                    array_type = PDH_FMT_COUNTERVALUE_ITEM_DOUBLE * item_count.value
                    items = array_type.from_buffer(buffer)
                    for i in range(item_count.value):
                        name = items[i].szName
                        status = items[i].FmtValue.CStatus
                        val = items[i].FmtValue.doubleValue
                        if name and status in (0, 1) and val > 0:
                            results.append((name, val))
        except Exception:
            pass
        return results

    def _background_telemetry_loop(self):
        pdh = None
        pdh_query = wintypes.HANDLE()
        h_shared_mem = None
        h_dedicated_mem = None
        h_gpu_engine = None
        pdh_initialized = False

        try:
            pdh = ctypes.windll.pdh
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhOpenQueryW.restype = wintypes.DWORD

            pdh.PdhAddCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhAddCounterW.restype = wintypes.DWORD

            try:
                pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
                pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
            except Exception:
                pass

            pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            pdh.PdhCollectQueryData.restype = wintypes.DWORD

            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
            ]
            pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD

            res = pdh.PdhOpenQueryW(None, 0, ctypes.byref(pdh_query))
            if res == 0:
                # 【核心修复】：GeForce 在 WDDM 驱动模式下，NVML 既拿不到每进程显存
                # (usedGpuMemory 恒为 None)，也拿不到每进程利用率 (接口不支持)。
                # 必须改用 Windows 图形内核 (dxgkrnl) 的 PDH 性能计数器 —— 即任务管理器同源数据。
                h_shared_mem = self._pdh_add_counter(pdh, pdh_query, "\\GPU Process Memory(*)\\Shared Usage")
                h_dedicated_mem = self._pdh_add_counter(pdh, pdh_query, "\\GPU Process Memory(*)\\Dedicated Usage")
                h_gpu_engine = self._pdh_add_counter(pdh, pdh_query, "\\GPU Engine(*)\\Utilization Percentage")
                pdh_initialized = any([h_shared_mem, h_dedicated_mem, h_gpu_engine])
                print(f"[🎮 GPU 进程遥测] PDH 计数器并网: 共享显存={'✅' if h_shared_mem else '❌'} | 专用显存={'✅' if h_dedicated_mem else '❌'} | GPU引擎利用率={'✅' if h_gpu_engine else '❌'}")
        except Exception:
            pdh_initialized = False

        try:
            net_io = psutil.net_io_counters()
            self.last_net_bytes_sent = net_io.bytes_sent
            self.last_net_bytes_recv = net_io.bytes_recv
        except:
            pass

        while True:
            temp_net_conn = {}
            try:
                from collections import defaultdict
                grouped = defaultdict(list)
                for conn in psutil.net_connections(kind='inet'):
                    if conn.pid: 
                        grouped[conn.pid].append(conn)
                
                for pid, conns in grouped.items():
                    net_conn_count = len(conns)
                    remote_targets = [f"{c.raddr.ip}:{c.raddr.port}" for c in conns if c.raddr]
                    net_remote = ",".join(remote_targets) if remote_targets else None
                    temp_net_conn[pid] = (net_conn_count, net_remote)
            except Exception:
                pass

            try:
                now_net_time = time.time()
                net_io = psutil.net_io_counters()
                dt_net = max(0.001, now_net_time - self.last_net_time)
                
                self.system_net_send_rate = max(0.0, (net_io.bytes_sent - self.last_net_bytes_sent) / 1024.0 / dt_net)
                self.system_net_recv_rate = max(0.0, (net_io.bytes_recv - self.last_net_bytes_recv) / 1024.0 / dt_net)
                
                self.last_net_bytes_sent = net_io.bytes_sent
                self.last_net_bytes_recv = net_io.bytes_recv
                self.last_net_time = now_net_time
            except Exception:
                pass

            temp_shared_vram = {}
            temp_pdh_dedicated_gb = {}
            temp_pdh_gpu_util = {}
            if pdh_initialized:
                try:
                    res = pdh.PdhCollectQueryData(pdh_query)
                    if res == 0:
                        for name, val in self._read_pdh_pid_array(pdh, h_shared_mem):
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                temp_shared_vram[pid] = temp_shared_vram.get(pid, 0) + int(val / (1024 * 1024))

                        for name, val in self._read_pdh_pid_array(pdh, h_dedicated_mem):
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                temp_pdh_dedicated_gb[pid] = temp_pdh_dedicated_gb.get(pid, 0.0) + val / (1024 ** 3)

                        for name, val in self._read_pdh_pid_array(pdh, h_gpu_engine):
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                # 任务管理器同口径：取该进程所有 GPU 引擎中最繁忙引擎的利用率
                                util = min(float(val), 100.0)
                                if util > temp_pdh_gpu_util.get(pid, 0.0):
                                    temp_pdh_gpu_util[pid] = util
                except Exception:
                    pass

            temp_vram_map = {}
            temp_gpu_util_map = {}
            if self.nvml_initialized:
                # 【核心修复】：WDDM 模式下 usedGpuMemory 恒为 None，原代码对 None 做除法
                # 抛出 TypeError 后整轮采集被吞掉，导致显存/GPU 利用率永远为 0。
                # 此处 NVML 仅作为兜底数据源（TCC/MIG 模式下才有值），并对两个接口分别做异常隔离。
                try:
                    for nv_proc in pynvml.nvmlDeviceGetGraphicsRunningProcesses(self.gpu_handle):
                        if nv_proc.usedGpuMemory:
                            temp_vram_map[nv_proc.pid] = nv_proc.usedGpuMemory / (1024 ** 3)
                except Exception:
                    pass
                try:
                    util_samples = pynvml.nvmlDeviceGetProcessUtilization(self.gpu_handle, 0)
                    for sample in util_samples:
                        util = getattr(sample, 'gpuUtil', None)
                        if util is not None:
                            temp_gpu_util_map[sample.pid] = float(util)
                except Exception:
                    pass
            else:
                self._init_nvml()

            # PDH (任务管理器同源) 数据优先覆盖 NVML —— GeForce WDDM 下唯一可靠的每进程 GPU 数据源
            temp_vram_map.update(temp_pdh_dedicated_gb)
            temp_gpu_util_map.update(temp_pdh_gpu_util)

            with self.cache_lock:
                self.net_conn_cache = temp_net_conn
                self.shared_vram_cache = temp_shared_vram
                self.vram_map_cache = temp_vram_map
                self.gpu_util_map_cache = temp_gpu_util_map

            time.sleep(2.0)

    async def get_or_register_cached(self, conn, proc_info):
        exe_path = proc_info["exe"]
        is_elevated = self.path_elevation_cache.get(exe_path)
        if is_elevated is None:
            is_elevated = await asyncio.to_thread(check_process_elevation, proc_info["os_pid"])
            if is_elevated < 0:
                is_elevated = await conn.fetchval(
                    "SELECT is_elevated FROM public.dim_process_registry WHERE executable_path = $1 ORDER BY created_at DESC LIMIT 1",
                    exe_path
                )
                if is_elevated is None:
                    is_elevated = 0
            self.path_elevation_cache[exe_path] = is_elevated

        from lifecycle_worker import SIGNATURE_CACHE, check_file_signature
        signature_status = SIGNATURE_CACHE.get(exe_path)
        if signature_status is None:
            signature_status = await asyncio.to_thread(check_file_signature, exe_path)
        
        cache_tuple = (
            proc_info["name"], 
            exe_path, 
            proc_info["parent_name"], 
            proc_info["cmdline"], 
            proc_info["service_name"],
            is_elevated,
            signature_status
        )
        if cache_tuple in self.key_cache: 
            return self.key_cache[cache_tuple]
        
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
            query, 
            proc_info["name"], 
            exe_path, 
            proc_info["parent_name"], 
            proc_info["cmdline"], 
            proc_info["service_name"],
            is_elevated,
            signature_status
        )
        if p_key: 
            self.key_cache[cache_tuple] = p_key
        return p_key

    def collect_active_processes(self):
        active_list = []
        next_io_cache = {}
        now_ts = asyncio.get_event_loop().time()

        with self.cache_lock:
            local_net_cache = dict(self.net_conn_cache)
            local_vram_cache = dict(self.shared_vram_cache)
            local_vram_map = dict(self.vram_map_cache)
            local_gpu_util_map = dict(self.gpu_util_map_cache)
            system_send_rate = self.system_net_send_rate
            system_recv_rate = self.system_net_recv_rate

        if not local_vram_map and self.nvml_initialized:
            try:
                for nv_proc in pynvml.nvmlDeviceGetGraphicsRunningProcesses(self.gpu_handle):
                    if nv_proc.usedGpuMemory:
                        local_vram_map[nv_proc.pid] = nv_proc.usedGpuMemory / (1024 ** 3)
            except Exception:
                pass
            try:
                util_samples = pynvml.nvmlDeviceGetProcessUtilization(self.gpu_handle, 0)
                for sample in util_samples:
                    util = getattr(sample, 'gpuUtil', None)
                    if util is not None:
                        local_gpu_util_map[sample.pid] = float(util)
            except Exception:
                pass

        proc_list = fetch_system_processes()
        
        if proc_list is None:
            proc_list = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_times', 'io_counters', 'create_time']):
                try:
                    info = proc.info
                    c_time = info['create_time']
                    if c_time is None: continue
                    cpu_times = info.get('cpu_times')
                    cpu_time = (cpu_times.user + cpu_times.system) if cpu_times else 0.0
                    io = info['io_counters']
                    proc_list.append({
                        "pid": info['pid'],
                        "name": info['name'],
                        "cpu_time": cpu_time,
                        "r_bytes": io.read_bytes if io else 0,
                        "w_bytes": io.write_bytes if io else 0,
                        "other_bytes": io.other_bytes if io else 0,
                        "r_ops": io.read_count if io else 0,
                        "w_ops": io.write_count if io else 0,
                        "ram_mb": 0,
                        "threads": 1,
                        "create_time": c_time,
                        "is_fallback": True
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        total_active_connections = sum([val[0] for val in local_net_cache.values() if val[0] > 0])

        for p_info in proc_list:
            pid = p_info["pid"]
            name = p_info["name"]
            create_time = p_info["create_time"]
            cache_key = (pid, create_time)
            
            r_bytes = p_info["r_bytes"]
            w_bytes = p_info["w_bytes"]
            other_bytes = p_info["other_bytes"]
            r_ops = p_info["r_ops"]
            w_ops = p_info["w_ops"]
            
            r_rate_mb = w_rate_mb = iops_rate = other_rate_kb = 0.0
            
            if cache_key in self.io_delta_cache:
                last_r, last_w, last_ro, last_wo, last_other, last_t = self.io_delta_cache[cache_key]
                dt = max(0.001, now_ts - last_t)
                
                if dt >= 0.1:
                    r_rate_mb = max(0.0, (r_bytes - last_r) / (1024 * 1024) / dt)
                    w_rate_mb = max(0.0, (w_bytes - last_w) / (1024 * 1024) / dt)
                    iops_rate = max(0.0, ((r_ops + w_ops) - (last_ro + last_wo)) / dt)
                    other_rate_kb = max(0.0, (other_bytes - last_other) / 1024.0 / dt)
                    next_io_cache[cache_key] = (r_bytes, w_bytes, r_ops, w_ops, other_bytes, now_ts)
                else:
                    next_io_cache[cache_key] = (last_r, last_w, last_ro, last_wo, last_other, last_t)
            else:
                next_io_cache[cache_key] = (r_bytes, w_bytes, r_ops, w_ops, other_bytes, now_ts)
            
            cpu_time = p_info["cpu_time"]
            cpu = 0.0
            if cache_key in self.cpu_time_cache:
                last_cpu_time, last_t = self.cpu_time_cache[cache_key]
                dt = max(0.001, now_ts - last_t)
                if dt >= 0.1:
                    cpu = max(0.0, ((cpu_time - last_cpu_time) / dt) * 100.0)
                    self.cpu_time_cache[cache_key] = (cpu_time, now_ts)
                else:
                    self.cpu_time_cache[cache_key] = (last_cpu_time, last_t)
            else:
                self.cpu_time_cache[cache_key] = (cpu_time, now_ts)

            if cpu <= 0.1 and r_rate_mb <= 0.01 and w_rate_mb <= 0.01 and other_rate_kb <= 0.5: 
                continue

            p_key = self.pid_key_cache.get(cache_key)
            
            exe_path = None
            cmdline_str = ""
            parent_name = None
            service_name = None
            
            if p_key is None:
                try:
                    proc = psutil.Process(pid)
                    exe_path = proc.exe()
                    if not exe_path: continue
                    cmdline_str = " ".join(proc.cmdline()) if proc.cmdline() else ""
                    cmdline_str = sanitize_command_line(name, cmdline_str)
                    
                    try:
                        parent_proc = proc.parent()
                        if parent_proc: parent_name = parent_proc.name()
                    except: pass
                    
                    if name.lower() == 'svchost.exe':
                        try:
                            services = proc.services()
                            if services: service_name = ",".join([s.name for s in services])[:100]
                        except:
                            cmd_parts = proc.cmdline()
                            if "-k" in cmd_parts:
                                idx = cmd_parts.index("-k")
                                if idx + 1 < len(cmd_parts): service_name = f"Group:{cmd_parts[idx+1]}"[:100]
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue

            if not p_info.get("is_fallback"):
                ram_mb = p_info["ram_mb"]
                threads = p_info["threads"]
            else:
                try:
                    proc = psutil.Process(pid)
                    p_mem = proc.memory_info()
                    ram_mb = int(p_mem.rss / (1024 * 1024)) if p_mem else 0
                    threads = int(proc.num_threads())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue

            affinity_mask = self.affinity_cache.get(cache_key)
            if affinity_mask is None:
                affinity_mask = 0
                try:
                    proc = psutil.Process(pid)
                    for cpu_id in proc.cpu_affinity():
                        if cpu_id < 32: affinity_mask |= (1 << cpu_id)
                    if affinity_mask & 0x80000000: affinity_mask -= 0x100000000
                except: 
                    affinity_mask = -2 
                self.affinity_cache[cache_key] = affinity_mask

            p_vram = float(local_vram_map.get(pid, 0.0))
            p_gpu_util = float(local_gpu_util_map.get(pid, 0.0))
            shared_vram_mb = int(local_vram_cache.get(pid, 0))

            is_dead = False
            try:
                proc = psutil.Process(pid)
                is_dead = True if proc.status() == psutil.STATUS_STOPPED else False
            except: pass

            net_conn_count, net_remote = local_net_cache.get(pid, (0, None))
            if net_conn_count > 0 and total_active_connections > 0:
                conn_ratio = float(net_conn_count) / float(total_active_connections)
                proc_net_send_kb = float(system_send_rate * conn_ratio)
                proc_net_recv_kb = float(system_recv_rate * conn_ratio)
            else:
                proc_net_send_kb = 0.0
                proc_net_recv_kb = 0.0

            active_list.append({
                "os_pid": pid, "create_time": create_time, "name": name,
                "exe": exe_path, "cmdline": cmdline_str, "parent_name": parent_name, "service_name": service_name, 
                "cpu": float(cpu), "gpu": p_gpu_util,
                "ram_mb": ram_mb, "vram_gb": p_vram, "vram_shared_mb": shared_vram_mb,
                "r_rate": r_rate_mb, "w_rate": w_rate_mb, "iops": int(iops_rate), 
                "net_send_kb": proc_net_send_kb, "net_recv_kb": proc_net_recv_kb,
                "affinity": affinity_mask, "threads": threads, "is_not_responding": is_dead,
                "net_conn_count": int(net_conn_count), "net_remote": net_remote,
                "process_key": p_key 
            })
                
        self.io_delta_cache = next_io_cache
        
        active_keys = set(next_io_cache.keys())
        self.pid_key_cache = {k: v for k, v in self.pid_key_cache.items() if k in active_keys}
        self.cpu_time_cache = {k: v for k, v in self.cpu_time_cache.items() if k in active_keys}
        self.affinity_cache = {k: v for k, v in self.affinity_cache.items() if k in active_keys}
        
        return active_list

    async def write_to_db_by_worker(self, pool, active_processes, timestamp=None):
        await self.write_batch_to_db(pool, active_processes, timestamp)

    async def write_batch_to_db(self, pool, active_processes, timestamp=None):
        if not active_processes: return
        now = timestamp if timestamp is not None else datetime.datetime.now(datetime.timezone.utc)
        
        uncached_procs = []
        for proc in active_processes:
            if proc.get("process_key") is None:
                uncached_procs.append(proc)
        
        async with pool.acquire() as conn:
            if uncached_procs:
                for p in uncached_procs:
                    p_key = await self.get_or_register_cached(conn, p)
                    if p_key:
                        p["process_key"] = p_key
                        self.pid_key_cache[(p["os_pid"], p["create_time"])] = p_key

            query = """
                INSERT INTO public.fact_process_activity 
                ("timestamp", process_key, os_pid, proc_cpu_usage, proc_gpu_usage, proc_ram_mb, 
                 proc_vram_used_gb, proc_vram_shared_mb, proc_disk_read_rate_mb, proc_disk_write_rate_mb, proc_disk_iops, 
                 proc_network_send_kb, proc_network_recv_kb, proc_active_connections, proc_remote_ip_port, 
                 proc_cpu_affinity, proc_thread_count, is_not_responding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18);
            """

            async with conn.transaction():
                batch_args = []
                for proc in active_processes:
                    p_key = proc.get("process_key")
                    if not p_key: 
                        continue
                    
                    # 【修复】：将 boolean 类型写回兼容性最强的物理 smallint 整型 1 或 0
                    batch_args.append((
                        now, p_key, proc["os_pid"], proc["cpu"], proc["gpu"], proc["ram_mb"],
                        proc["vram_gb"], proc["vram_shared_mb"], proc["r_rate"], proc["w_rate"], proc["iops"],
                        proc["net_send_kb"], proc["net_recv_kb"],
                        proc["net_conn_count"], proc["net_remote"], proc["affinity"], proc["threads"], 
                        1 if proc["is_not_responding"] else 0
                    ))
                if batch_args: 
                    await conn.executemany(query, batch_args)