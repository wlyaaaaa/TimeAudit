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
        self.gpu_total_vram_gb = 0.0   # RTX5080 整卡显存容量(GB)，每进程专用显存的物理上限
        self.num_cpus = psutil.cpu_count(logical=True) or 1   # 逻辑核数(9950X3D=32)，用于把每进程 CPU 归一化为整机占用%

        self.cpu_time_cache = {}
        self.affinity_cache = {}
        self.pid_key_cache = {}
        
        self.cache_lock = threading.Lock()
        self.net_conn_cache = {}
        self.shared_vram_cache = {}
        self.hung_pids_cache = set()   # 后台线程每拍刷新的“无响应/卡死”进程 PID 集合(IsHungAppWindow 同源)
        
        self.vram_map_cache = {}
        self.gpu_util_map_cache = {}
        # 【RTX5080 永久锁定】本机为三适配器拓扑：RTX5080 独显 + 9950X3D 的 AMD 核显 + 向日葵
        # 虚拟显示器(OrayIddDriver)。AMD 核显是 UMA 架构，会把大块系统内存误报成"专用显存"
        # (实测 dwm 在核显上"专用显存"高达 47GB，远超任何物理显存)，旧的"每轮取最大专用显存"
        # 启发式因此会被核显骗到，把游戏的显存/占用算到核显上、导致 RTX5080 进程数据恒为 ~0。
        # 现改为开机时从 DirectX 注册表按厂商 ID(NVIDIA=0x10DE)确定锁定 RTX5080 的 LUID 前缀，
        # 之后所有每进程 GPU 数据只认这张卡。LUID 每次开机重分配，故每次启动重读；来源是确定的
        # 硬件厂商 ID，绝非动态猜测，满足"不需要动态识别、永远是 RTX5080"的要求。
        self.nvidia_luid_prefixes = self._detect_nvidia_luid_prefixes()
        if self.nvidia_luid_prefixes:
            print(f"[🎮 GPU 进程遥测] 已按厂商 ID 锁定 NVIDIA 独显 LUID 前缀 {sorted(self.nvidia_luid_prefixes)}，核显/虚拟显示器一律隔离。")
        else:
            print("[⚠️ GPU 进程遥测] 未能从 DirectX 注册表识别 NVIDIA 独显，将周期性重试；在此之前不做适配器过滤。")

        self.last_net_bytes_sent = 0
        self.last_net_bytes_recv = 0
        self.last_net_time = time.monotonic()
        self.system_net_send_rate = 0.0
        self.system_net_recv_rate = 0.0
        
        self._init_nvml()
        try_enable_debug_privilege()
        
        self.bg_thread = threading.Thread(target=self._background_telemetry_loop, daemon=True)
        self.bg_thread.start()

    def reset_on_resume(self):
        """【Bug5 修复】系统睡眠唤醒后由主控调用：丢弃过期的每进程速率基线。否则唤醒后第一拍会把
        "睡眠期间几乎为零的字节增量"除以巨大的时间差(网络速率用墙钟 time.time()、CPU/IO 用可能在睡眠
        中暂停的 monotonic)，导致网络监控图表出现一次归零式断崖或 CPU/IO 虚高。清空增量缓存即令下一拍
        重新锚定基线(期间仅一拍速率为 0，符合"睡眠期间确无活动"的事实)。仅触碰主线程拥有的缓存。"""
        self.cpu_time_cache.clear()
        self.io_delta_cache.clear()
        try:
            net_io = psutil.net_io_counters()
            self.last_net_bytes_sent = net_io.bytes_sent
            self.last_net_bytes_recv = net_io.bytes_recv
            self.last_net_time = time.monotonic()
        except Exception:
            pass

    def _init_nvml(self):
        try:
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nvml_initialized = True
            try:
                # 整卡显存容量，用作每进程专用显存的物理上限。WDDM 下 PDH 的 Dedicated Usage 对
                # dwm 合成器等会聚合上报远超物理显存的虚高值(实测 dwm 达 68GB)，必须按此上限钳制。
                self.gpu_total_vram_gb = pynvml.nvmlDeviceGetMemoryInfo(self.gpu_handle).total / (1024 ** 3)
            except Exception:
                pass
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

    @staticmethod
    def _extract_adapter_key(name):
        # 从 PDH 实例名提取物理适配器段，如 luid_0x00000000_0x0001D7B8_phys_0
        # 三类实例名均含此段：GPU Adapter Memory / GPU Process Memory / GPU Engine
        m = re.search(r'luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+_phys_\d+', name)
        return m.group(0) if m else None

    @staticmethod
    def _luid_prefix(name):
        # 仅提取 LUID 前缀 luid_0xHIGH_0xLOW(不含 _phys_N)，统一小写以跨"适配器/进程/引擎"实例比对。
        m = re.search(r'luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+', name)
        return m.group(0).lower() if m else None

    @staticmethod
    def _detect_nvidia_luid_prefixes():
        """从 Windows DirectX 注册表读取所有 NVIDIA(VendorId=0x10DE) 适配器的 LUID 前缀。

        PDH 的 GPU 计数器实例名形如 pid_1234_luid_0xHIGH_0xLOW_phys_0_eng_0_engtype_3D，
        其中 luid 段唯一标识物理适配器。注册表 HKLM\\SOFTWARE\\Microsoft\\DirectX 下每个适配器
        子键含 VendorId 与 AdapterLuid(QWORD = (HighPart<<32)|LowPart)，据此即可把 NVIDIA 卡的
        LUID 转成与 PDH 同构的前缀字符串，实现厂商级精确锁定。返回小写前缀集合(可能含历史残留项，
        但只有当前在网的 LUID 会真正匹配到实例，故无害)。"""
        import winreg
        prefixes = set()
        try:
            base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX")
        except OSError:
            return prefixes
        try:
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(base, idx)
                    idx += 1
                except OSError:
                    break
                try:
                    sub = winreg.OpenKey(base, sub_name)
                    try:
                        vendor, _ = winreg.QueryValueEx(sub, "VendorId")
                        luid, _ = winreg.QueryValueEx(sub, "AdapterLuid")
                    finally:
                        sub.Close()
                    if int(vendor) == 0x10DE and luid is not None:
                        u = int(luid) & 0xFFFFFFFFFFFFFFFF
                        low = u & 0xFFFFFFFF
                        high = (u >> 32) & 0xFFFFFFFF
                        prefixes.add("luid_0x%08x_0x%08x" % (high, low))
                except OSError:
                    continue
        finally:
            base.Close()
        return prefixes

    @staticmethod
    def _scan_hung_pids():
        """枚举所有可见顶层窗口，用 IsHungAppWindow(任务管理器“未响应”判定同源 API：窗口消息泵 >5 秒
        无回应即视为卡死)标记其属主进程。返回卡死进程的 PID 集合；无窗口的服务/后台进程自然不在其中(=正常)。
        旧实现用 psutil.STATUS_STOPPED(Unix 概念，Windows 上几乎永不为真)，致 is_not_responding 形同虚设。"""
        hung = set()
        try:
            user32 = ctypes.windll.user32
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.IsHungAppWindow.argtypes = [wintypes.HWND]
            user32.IsHungAppWindow.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _cb(hwnd, _lparam):
                try:
                    if user32.IsWindowVisible(hwnd) and user32.IsHungAppWindow(hwnd):
                        pid = wintypes.DWORD(0)
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value:
                            hung.add(pid.value)
                except Exception:
                    pass
                return True

            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            pass
        return hung

    def _background_telemetry_loop(self):
        pdh = None
        pdh_query = wintypes.HANDLE()
        h_shared_mem = None
        h_dedicated_mem = None
        h_gpu_engine = None
        h_adapter_mem = None
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
                # 【显存精度修复】用 Local Usage(GPU 本地/专用显存段的常驻量) 而非 Dedicated Usage。
                # 实测 Dedicated Usage 含已提交/保留的地址空间，对 dwm 合成器爆表(72GB)；Local Usage
                # 才是常驻专用显存：dwm 仅 1.35GB，且全进程合计 ~4.9GB 与整卡实际已用 ~5.7GB 吻合。
                h_dedicated_mem = self._pdh_add_counter(pdh, pdh_query, "\\GPU Process Memory(*)\\Local Usage")
                h_gpu_engine = self._pdh_add_counter(pdh, pdh_query, "\\GPU Engine(*)\\Utilization Percentage")
                # 每适配器专用显存总量，用于在双 GPU(独显+核显+虚拟显示器)中识别独显 RTX5080
                h_adapter_mem = self._pdh_add_counter(pdh, pdh_query, "\\GPU Adapter Memory(*)\\Dedicated Usage")
                pdh_initialized = any([h_shared_mem, h_dedicated_mem, h_gpu_engine])
                print(f"[🎮 GPU 进程遥测] PDH 计数器并网: 共享显存={'✅' if h_shared_mem else '❌'} | 专用显存={'✅' if h_dedicated_mem else '❌'} | GPU引擎利用率={'✅' if h_gpu_engine else '❌'} | 适配器甄别={'✅' if h_adapter_mem else '❌'}")
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
                now_net_time = time.monotonic()
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
                        # 【RTX5080 厂商级锁定】只统计 NVIDIA 独显上的每进程显存/占用，彻底隔离
                        # AMD 核显(UMA 会把系统内存误报成巨量专用显存)与向日葵虚拟显示器。
                        nv = self.nvidia_luid_prefixes
                        if not nv:
                            # 开机时注册表读取失败的兜底：周期性重试，一旦识别成功即永久锁定。
                            nv = self.nvidia_luid_prefixes = self._detect_nvidia_luid_prefixes()
                            if nv:
                                print(f"[🎮 GPU 进程遥测] 已补充锁定 NVIDIA 独显 LUID 前缀 {sorted(nv)}。")

                        def _is_rtx5080(instance_name):
                            # 仅保留 NVIDIA 独显的实例；nv 为空(极端兜底)时不过滤，退化为全卡合计。
                            if not nv:
                                return True
                            p = ProcessActivityWorker._luid_prefix(instance_name)
                            return p is not None and p in nv

                        for name, val in self._read_pdh_pid_array(pdh, h_shared_mem):
                            if not _is_rtx5080(name):
                                continue
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                temp_shared_vram[pid] = temp_shared_vram.get(pid, 0) + int(val / (1024 * 1024))

                        # 每进程专用显存物理上限：取"整卡容量"与"整卡当前已用总量"的较小者。
                        # 单进程常驻专用显存不可能超过整卡当前已驻留总量，据此把 dwm 等聚合虚高值
                        # 收紧到现实量级(实测整卡仅用 ~5.7GB，而 PDH 给 dwm 报 68GB)。
                        cap_gb = self.gpu_total_vram_gb
                        try:
                            if self.nvml_initialized:
                                used_gb = pynvml.nvmlDeviceGetMemoryInfo(self.gpu_handle).used / (1024 ** 3)
                                if used_gb > 0:
                                    cap_gb = min(cap_gb, used_gb) if cap_gb > 0 else used_gb
                        except Exception:
                            pass
                        for name, val in self._read_pdh_pid_array(pdh, h_dedicated_mem):
                            if not _is_rtx5080(name):
                                continue
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                val_gb = val / (1024 ** 3)
                                # 物理上限保护：单进程常驻专用显存不可能超过整卡容量(dwm 合成器虚高规避)
                                if cap_gb > 0 and val_gb > cap_gb:
                                    val_gb = cap_gb
                                temp_pdh_dedicated_gb[pid] = temp_pdh_dedicated_gb.get(pid, 0.0) + val_gb

                        for name, val in self._read_pdh_pid_array(pdh, h_gpu_engine):
                            if not _is_rtx5080(name):
                                continue
                            match = re.search(r'pid_(\d+)', name, re.IGNORECASE)
                            if match:
                                pid = int(match.group(1))
                                # 任务管理器同口径：取该进程在独显上所有引擎中最繁忙引擎的利用率
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

            temp_hung_pids = self._scan_hung_pids()

            with self.cache_lock:
                self.net_conn_cache = temp_net_conn
                self.shared_vram_cache = temp_shared_vram
                self.vram_map_cache = temp_vram_map
                self.gpu_util_map_cache = temp_gpu_util_map
                self.hung_pids_cache = temp_hung_pids

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
        now_ts = time.monotonic()

        with self.cache_lock:
            local_net_cache = dict(self.net_conn_cache)
            local_vram_cache = dict(self.shared_vram_cache)
            local_vram_map = dict(self.vram_map_cache)
            local_gpu_util_map = dict(self.gpu_util_map_cache)
            local_hung_pids = set(self.hung_pids_cache)
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
                        "ppid": 0,
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

        # 【性能优化】用 NtQuerySystemInformation 同一快照的 pid→name 映射解析父进程名，取代
        # psutil.Process.parent()——后者在 Windows 上每次都全量重建 ppid_map(枚举所有进程)，
        # cProfile 实测占 collect_active_processes 总耗时的 ~86%(单拍 ~1s)。同一快照内 ppid
        # 自洽且零额外系统调用，把 collect 从 ~1s 压到 ~0.1s。
        pid_to_name = {p["pid"]: p["name"] for p in proc_list}

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
            cpu = 0.0       # 整机归一化占用% (0-100, 与任务管理器口径一致)
            cpu_raw = 0.0   # 多核合计原始占用% (可超 100, 仅用于活跃度过滤的灵敏度保持)
            if cache_key in self.cpu_time_cache:
                last_cpu_time, last_t = self.cpu_time_cache[cache_key]
                dt = max(0.001, now_ts - last_t)
                if dt >= 0.1:
                    cpu_raw = max(0.0, ((cpu_time - last_cpu_time) / dt) * 100.0)
                    # 【CPU 占用 bug 修复】按全部逻辑核(32)归一化并上限 100%。
                    # 旧实现是"单核占用%"，多线程进程会累加到 2556% 等虚高值；现统一为整机口径 0-100。
                    cpu = min(100.0, cpu_raw / self.num_cpus)
                    self.cpu_time_cache[cache_key] = (cpu_time, now_ts)
                else:
                    self.cpu_time_cache[cache_key] = (last_cpu_time, last_t)
            else:
                self.cpu_time_cache[cache_key] = (cpu_time, now_ts)

            if cpu_raw <= 0.1 and r_rate_mb <= 0.01 and w_rate_mb <= 0.01 and other_rate_kb <= 0.5:
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
                    
                    # 父进程名改用快照内 pid→name 映射(零系统调用)；不再调 proc.parent()
                    # (Windows 上每次触发 ppid_map 全进程枚举，是 collect 的头号耗时点)。
                    ppid = p_info.get("ppid", 0)
                    if ppid:
                        parent_name = pid_to_name.get(ppid)
                    
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

            # 【无响应检测修复】改用与任务管理器同源的 IsHungAppWindow：后台线程每拍枚举可见顶层窗口、
            # 标记消息泵卡死(>5s)的属主 PID，此处仅 O(1) 集合查表。旧的 psutil.STATUS_STOPPED 是 Unix 概念，
            # Windows 上几乎永不为真，导致该字段与 addrd7x「卡死进程时间线」面板形同虚设。
            is_dead = pid in local_hung_pids

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