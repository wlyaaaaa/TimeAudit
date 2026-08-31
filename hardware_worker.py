# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="pynvml")
warnings.filterwarnings("ignore", category=FutureWarning)

import datetime
import asyncio
import subprocess
import threading
import logging
import re
import math
import time
import socket
import ctypes
from ctypes import wintypes  # 【修复】网络探针线程使用 wintypes.HANDLE/BOOL/WORD/DWORD 等，缺此导入会让
                            # ICMP 初始化必抛 NameError → has_icmp=False → 永远退化为 TCP connect 测延迟。
import os
import struct
from collections import defaultdict
import psutil
import pynvml
import atexit

NVML_PCIE_UTIL_TX_BYTES = 0
NVML_PCIE_UTIL_RX_BYTES = 1

LIMIT_REASON_IDLE = 0x0000000000000001
LIMIT_REASON_SW_POWER = 0x0000000000000004
LIMIT_REASON_HW_SLOWDOWN = 0x0000000000000008
LIMIT_REASON_SW_THERMAL = 0x0000000000000020
LIMIT_REASON_HW_THERMAL = 0x0000000000000040

NVML_CLOCK_GRAPHICS = getattr(pynvml, 'NVML_CLOCK_GRAPHICS', 0)
NVML_CLOCK_MEM = getattr(pynvml, 'NVML_CLOCK_MEM', 2)

# ==========================================
# 【新增】：全局 NVML 互斥锁，序列化所有 NVML 相关的 C 语言底层跨线程调用
# ==========================================
NVML_LOCK = threading.Lock()

class DpcLatencyChecker:
    def __init__(self):
        self.max_jitter_us = 0.0
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        def loop():
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
            winmm.timeBeginPeriod.restype = ctypes.c_uint
            winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
            winmm.timeEndPeriod.restype = ctypes.c_uint

            winmm.timeBeginPeriod(1)
            last_reset = time.perf_counter()
            while not self.stop_event.is_set():
                t0 = time.perf_counter()
                time.sleep(0.001)  
                t1 = time.perf_counter()

                elapsed_us = (t1 - t0) * 1000000.0
                jitter_us = max(0.0, elapsed_us - 1000.0)

                # 【数据质量修复】这是用户态 time.sleep 抖动(受 Python GIL 争用/系统睡眠/GC 停顿污染)，
                # 并非真实内核 DPC 延迟。单次溢出 > 1 秒几乎必是系统睡眠/挂起的伪值(实测出现过 8.5 秒)，丢弃；
                # 其余封顶 100ms，使其退化为有界的"用户态调度抖动"代理，杜绝垃圾尖刺污染稳定性大盘。
                if jitter_us > 1000000.0:
                    jitter_us = 0.0
                elif jitter_us > 100000.0:
                    jitter_us = 100000.0

                if jitter_us > self.max_jitter_us:
                    self.max_jitter_us = jitter_us

                if t1 - last_reset >= 1.0:
                    self.max_jitter_us = jitter_us
                    last_reset = t1
            winmm.timeEndPeriod(1)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def get_latency_us(self):
        return self.max_jitter_us

    def stop(self):
        self.stop_event.set()

class HardwareTelemetryWorker:
    # 【PresentMon 门控】只在"活跃渲染"(游戏/3D)时运行 PresentMon。GPU 占用超此阈值即判定在渲染；
    # 渲染停止后再多保活 RENDER_HYSTERESIS_SEC 秒(滞回)避免抖动。这样桌面/窗口化轻载时 PresentMon 关闭，
    # 既省资源、又规避其 24x7 系统级 ETW present 捕获对 DWM 合成(窗口化呈现)的潜在扰动(灰屏嫌疑)。
    RENDER_GPU_THRESHOLD = 18.0
    RENDER_HYSTERESIS_SEC = 75.0
    PRESENTMON_SESSION_NAME = "TimeAuditPresentMon"
    # PresentMon reports frame intervals in milliseconds.  Values outside this
    # broad, physically useful range are malformed/overflow samples rather than
    # a meaningful current frame interval (0.1 ms = 10,000 FPS; 2 s = 0.5 FPS).
    PRESENTMON_FRAME_TIME_MIN_MS = 0.1
    PRESENTMON_FRAME_TIME_MAX_MS = 2000.0
    PRESENTMON_FRAME_FRESH_SECONDS = 5.0

    def __init__(self):
        self._last_render_ts = 0.0   # 最近一次检测到活跃渲染的 monotonic 时刻
        self.nvml_initialized = False
        self.gpu_handle = None
        self.presentmon_process = None
        self._presentmon_process_lock = threading.Lock()
        self._presentmon_process_condition = threading.Condition(
            self._presentmon_process_lock
        )
        self._presentmon_claims_inflight = 0
        self.presentmon_thread = None
        
        self.active_foreground_app = ""
        self.active_foreground_pid = None
        self.last_ts = time.monotonic()
        
        self.network_metrics = {"ping_ms": None, "packet_loss": False, "jitter": 0.0}
        
        self.app_windows = defaultdict(list)
        self.app_last_update = {}
        self.lock = threading.Lock()
        
        self.wmi_lock = threading.Lock()
        self.cached_wmi_temp = None        # CPU 封装温度 (LHM: Core (Tctl/Tdie))
        self.cached_wmi_power = None        # CPU 封装功率 (LHM: Powers/Package)
        self.cached_cpu_vcore = None        # CPU Vcore (LHM: 主板 Super I/O 真实读数)
        self.cached_gpu_voltage = None      # NVIDIA GPU 核心电压 (LHM/NVAPI; NVML 在 GeForce 上无法提供)
        self.cached_gpu_hotspot = None      # NVIDIA GPU 显存结点/热点温度 (LHM)
        self.stop_event = threading.Event()
        
        self.pdh_lock = threading.Lock()
        self.cached_pdh_data = {
            "cpu_mhz": 4300,
            "cpu_package_temp": None,
            "cpu_package_power": None,
            "system_hard_page_faults": 0,
            "system_context_switches_rate": 0,
            "disk_max_latency_ms": 0.0,
            
            "cpu_percents": [0.0] * 32,
            "cpu_total_usage": 0.0,
            
            "gpu_usage": 0.0,
            "gpu_core_voltage": 0.0,
            "gpu_core_clock": 0,
            "gpu_mem_clock": 0,
            "gpu_core_temp": 0.0,
            "gpu_hotspot_temp": 0.0,
            "gpu_board_power": 0.0,
            "gpu_throttling_reasons": 0,
            "pcie_bus_utilization": 0.0
        }
        
        self.dpc_checker = DpcLatencyChecker()
        self.dpc_checker.start()

        self._init_nvml()
        self._init_pdh_cppc_engine()
        self._start_native_socket_network_stream()
        self._start_presentmon_listener()

        # 只负责准备外部硬件监视器文件；运行中的 LHM 由独立计划任务/外部
        # telemetry watchdog 统一拥有，避免本进程与计划任务各拉起一个 NVML
        # 实例。多个 LHM 实例会在显示拓扑/HDR 切换时放大 NVIDIA 驱动竞态。
        self.lhm_download_thread = threading.Thread(target=self._auto_prepare_lhm_async, daemon=True)
        self.lhm_download_thread.start()

        self.wmi_thread = threading.Thread(target=self._background_lhm_loop, daemon=True)
        self.wmi_thread.start()

        self.pdh_thread = threading.Thread(target=self._background_pdh_loop, daemon=True)
        self.pdh_thread.start()

        atexit.register(self.terminate)

    def _init_nvml(self):
        with NVML_LOCK:
            try:
                pynvml.nvmlInit()
                self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self.nvml_initialized = True
                print("[🛸 硬件探针] NVIDIA NVML 驱动内核初始化成功。")
            except Exception as e:
                print(f"[⚠️ 硬件探针] 显卡驱动初始化暂歇性失败: {e}")
                self.nvml_initialized = False

    def _init_pdh_cppc_engine(self):
        try:
            self.pdh_query = ctypes.c_void_p()
            if ctypes.windll.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.pdh_query)) == 0:
                
                def add_first_working_counter(paths):
                    for path in paths:
                        h = ctypes.c_void_p()
                        res = ctypes.windll.pdh.PdhAddCounterW(self.pdh_query, path, 0, ctypes.byref(h))
                        if res == 0:
                            return h
                    return None

                self.h_base_freq = add_first_working_counter([
                    "\\Processor Information(_Total)\\Processor Frequency",
                    "\\Processor Information(0,_Total)\\Processor Frequency"
                ])
                self.h_perf_pct = add_first_working_counter([
                    "\\Processor Information(_Total)\\% Processor Performance",
                    "\\Processor Information(0,_Total)\\% Processor Performance"
                ])
                self.h_temp = add_first_working_counter([
                    "\\Thermal Zone Information(*)\\High Precision Temperature",
                    "\\Thermal Zone Information(\\_TZ.TZ00)\\High Precision Temperature",
                    "\\Thermal Zone Information(*)\\Temperature"
                ])
                self.h_power = add_first_working_counter([
                    "\\Power Meter(*)\\Power",
                    "\\Processor Information(_Total)\\Total Power",
                    "\\Processor Information(0,_Total)\\Total Power"
                ])
                self.h_hard_faults = add_first_working_counter([
                    "\\Memory\\Pages Input/sec",
                    "\\Memory\\Page Reads/sec"
                ])
                self.h_context_switches = add_first_working_counter([
                    "\\System\\Context Switches/sec"
                ])
                self.h_disk_latency = add_first_working_counter([
                    "\\PhysicalDisk(*)\\Avg. Disk sec/Transfer",
                    "\\PhysicalDisk(_Total)\\Avg. Disk sec/Transfer"
                ])
                
                ctypes.windll.pdh.PdhCollectQueryData(self.pdh_query)
                time.sleep(0.05)
                ctypes.windll.pdh.PdhCollectQueryData(self.pdh_query)
                print("[🛸 硬件探针] Win11 CPPC 增强型多轨性能监控并网预热成功。")
        except Exception as e:
            print(f"[❌ 硬件探针] PDH 频率底座并网严重卡顿: {e}")
            self.pdh_query = None

    def _start_native_socket_network_stream(self):
        def network_sensor_thread():
            target_hosts = ["114.114.114.114", "8.8.8.8", "1.1.1.1"]
            host_index = 0
            latency_window = []
            
            try:
                iphlpapi = ctypes.windll.iphlpapi
                ws2_32 = ctypes.windll.ws2_32
                iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
                iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
                iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
                iphlpapi.IcmpSendEcho.argtypes = [
                    wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD,
                    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
                ]
                iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
                ws2_32.inet_addr.argtypes = [ctypes.c_char_p]
                ws2_32.inet_addr.restype = ctypes.c_ulong
                has_icmp = True
            except Exception:
                has_icmp = False

            while True:
                target_host = target_hosts[host_index]
                current_ping = None
                
                if has_icmp:
                    try:
                        handle = iphlpapi.IcmpCreateFile()
                        if handle and handle != 18446744073709551615 and handle != 4294967295:
                            ip_addr = ws2_32.inet_addr(target_host.encode('ascii'))
                            reply_buffer = ctypes.create_string_buffer(256)
                            res = iphlpapi.IcmpSendEcho(handle, ip_addr, b"PING_DATA", 9, None, reply_buffer, 256, 800)
                            iphlpapi.IcmpCloseHandle(handle)
                            if res > 0:
                                status, rtt = struct.unpack_from("<II", reply_buffer, 4)
                                if status == 0:
                                    current_ping = float(rtt)
                    except Exception:
                        current_ping = None

                if current_ping is None:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.8)
                        t0 = time.perf_counter()
                        res = s.connect_ex((target_host, 53))
                        t1 = time.perf_counter()
                        s.close()
                        if res in (0, 10061):
                            current_ping = max(1.0, float((t1 - t0) * 1000.0))
                    except Exception:
                        current_ping = None

                if current_ping is not None:
                    latency_window.append(current_ping)
                    if len(latency_window) > 15: 
                        latency_window.pop(0)
                    self.network_metrics["ping_ms"] = current_ping
                    self.network_metrics["packet_loss"] = False
                    
                    if len(latency_window) > 1:
                        diffs = [abs(latency_window[i] - latency_window[i-1]) for i in range(1, len(latency_window))]
                        self.network_metrics["jitter"] = float(sum(diffs) / len(diffs))
                    else: 
                        self.network_metrics["jitter"] = 0.0
                else:
                    host_index = (host_index + 1) % len(target_hosts)
                    if host_index == 0:
                        self.network_metrics["packet_loss"] = True
                        self.network_metrics["ping_ms"] = None
                
                threading.Event().wait(1.0)
                
        t = threading.Thread(target=network_sensor_thread, daemon=True)
        t.start()

    def _render_active(self):
        """是否处于活跃渲染期(游戏/3D 在跑)。GPU 占用近期超阈值即为真，含 RENDER_HYSTERESIS_SEC 滞回。"""
        return (time.monotonic() - self._last_render_ts) < self.RENDER_HYSTERESIS_SEC

    @classmethod
    def _presentmon_command(cls, presentmon_path):
        return [
            presentmon_path,
            "--session_name",
            cls.PRESENTMON_SESSION_NAME,
            "--output_stdout",
            "--stop_existing_session",
            "--no_console_stats",
        ]

    @staticmethod
    def _normalize_presentmon_application(value):
        """Normalize PresentMon's application field to a basename without .exe."""
        text = str(value or "").strip().strip('"')
        if not text:
            return ""
        # PresentMon normally emits a basename, but accepting a path here keeps
        # the ownership key stable across versions that include the executable
        # path in the Application column.
        text = text.replace("\\", "/").rsplit("/", 1)[-1]
        if text.lower().endswith(".exe"):
            text = text[:-4]
        return text.casefold()

    @staticmethod
    def _parse_presentmon_process_id(value):
        try:
            numeric = float(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
            return None
        return int(numeric)

    @classmethod
    def _valid_presentmon_frame_time(cls, value):
        try:
            frame_time = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(frame_time) and (
            cls.PRESENTMON_FRAME_TIME_MIN_MS
            <= frame_time
            <= cls.PRESENTMON_FRAME_TIME_MAX_MS
        )

    def _record_presentmon_sample(
        self,
        parts,
        header_map,
        app_col_name="application",
        ft_idx=None,
        observed_monotonic=None,
    ):
        """Record one PresentMon CSV row under its exact process/application key.

        Rows without a usable ProcessID or frame interval are discarded.  The
        parser intentionally does not keep an application-name-only bucket:
        selecting one would allow DWM/other applications (or a reused name) to
        masquerade as the current foreground process.  ``observed_monotonic``
        is a test injection in the same monotonic age domain used in production.
        """
        try:
            app_idx = header_map.get(app_col_name)
            pid_idx = header_map.get("processid")
            if (
                app_idx is None
                or pid_idx is None
                or ft_idx is None
                or app_idx >= len(parts)
                or pid_idx >= len(parts)
                or ft_idx >= len(parts)
            ):
                return False

            application = self._normalize_presentmon_application(parts[app_idx])
            process_id = self._parse_presentmon_process_id(parts[pid_idx])
            if not application or process_id is None:
                return False

            raw_frame_time = str(parts[ft_idx]).strip()
            if raw_frame_time.upper() == "NA":
                return False
            frame_time = float(raw_frame_time)
            if not self._valid_presentmon_frame_time(frame_time):
                return False

            timestamp = (
                time.monotonic()
                if observed_monotonic is None
                else float(observed_monotonic)
            )
            key = (process_id, application)
            with self.lock:
                window = self.app_windows[key]
                previous = self.app_last_update.get(key)
                if (
                    previous is not None
                    and timestamp - previous > self.PRESENTMON_FRAME_FRESH_SECONDS
                ):
                    # A gap can be a PM restart or PID reuse.  Do not let the
                    # next process inherit the previous 200-frame average.
                    window.clear()
                window.append(frame_time)
                if len(window) > 200:
                    window.pop(0)
                self.app_last_update[key] = timestamp
            return True
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def _select_presentmon_window(
        self,
        foreground_app_name,
        foreground_pid,
        now_monotonic=None,
    ):
        """Return a copy of the fresh PresentMon window for the exact foreground PID.

        There is deliberately no newest-app fallback.  A missing PID match is
        represented by ``None`` and the caller reports an idle/unknown sample.
        ``now_monotonic`` is a test injection, never a wall-clock timestamp.
        """
        application = self._normalize_presentmon_application(foreground_app_name)
        process_id = self._parse_presentmon_process_id(foreground_pid)
        if not application or process_id is None:
            return None
        current_time = (
            time.monotonic() if now_monotonic is None else float(now_monotonic)
        )
        key = (process_id, application)
        with self.lock:
            last_update = self.app_last_update.get(key)
            window = self.app_windows.get(key)
            if (
                last_update is None
                or current_time - last_update > self.PRESENTMON_FRAME_FRESH_SECONDS
                or not window
            ):
                return None
            return list(window)

    @staticmethod
    def _read_foreground_pid():
        """Read the current Win32 foreground PID without changing focus/state."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value) if pid.value else None
        except Exception:
            return None

    def _begin_presentmon_launch(self):
        """预约一次启动事务，使 terminate 能等待 Popen/claim/拒绝清理全部收束。"""
        with self._presentmon_process_condition:
            if (
                self.stop_event.is_set()
                or self.presentmon_process is not None
                or self._presentmon_claims_inflight
            ):
                return False
            self._presentmon_claims_inflight += 1
            return True

    def _finish_presentmon_claim(self):
        with self._presentmon_process_condition:
            self._presentmon_claims_inflight -= 1
            self._presentmon_process_condition.notify_all()

    def _claim_presentmon_process(self, process, launch_reserved=False):
        """原子接管新进程；停止已开始时拒绝，并在事务完成前回收或发布重试句柄。"""
        if not launch_reserved:
            with self._presentmon_process_condition:
                self._presentmon_claims_inflight += 1

        try:
            with self._presentmon_process_condition:
                if not self.stop_event.is_set() and self.presentmon_process is None:
                    self.presentmon_process = process
                    return True

            stopped = self._terminate_presentmon_process_tree(process, timeout=1.0)
            if not stopped:
                # 先发布失败句柄，再结束事务；terminate 因此不会错过最终重试。
                with self._presentmon_process_condition:
                    if self.presentmon_process is None:
                        self.presentmon_process = process
                logging.getLogger("PresentMon_Debugger").warning(
                    "拒绝接管的 PresentMon 未确认退出，已保留可重试句柄（pid=%s）",
                    getattr(process, "pid", "?"),
                )
            return False
        finally:
            self._finish_presentmon_claim()

    @staticmethod
    def _terminate_presentmon_process_tree(process, timeout=2.0):
        """停止给定 Popen 的进程树；只有根进程和子进程均确认退出才返回 True。"""
        pm_logger = logging.getLogger("PresentMon_Debugger")
        children = []
        tree_confirmed = True

        try:
            root_running = process.poll() is None
        except Exception as exc:
            root_running = True
            tree_confirmed = False
            pm_logger.warning("无法读取 PresentMon 状态（pid=%s）: %s", getattr(process, "pid", "?"), exc)

        if root_running:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except Exception as exc:
                tree_confirmed = False
                pm_logger.warning(
                    "无法枚举 PresentMon 进程树（pid=%s）: %s",
                    getattr(process, "pid", "?"),
                    exc,
                )

        for child in children:
            try:
                child.terminate()
            except Exception as exc:
                pm_logger.warning("终止 PresentMon 子进程失败（pid=%s）: %s", getattr(child, "pid", "?"), exc)

        try:
            process.terminate()
        except Exception as exc:
            pm_logger.warning("终止 PresentMon 根进程失败（pid=%s）: %s", getattr(process, "pid", "?"), exc)

        alive_children = []
        if children:
            try:
                _, alive_children = psutil.wait_procs(children, timeout=timeout)
            except Exception as exc:
                alive_children = children
                pm_logger.warning("等待 PresentMon 子进程失败: %s", exc)

        for child in alive_children:
            try:
                child.kill()
            except Exception as exc:
                pm_logger.warning("强制结束 PresentMon 子进程失败（pid=%s）: %s", getattr(child, "pid", "?"), exc)

        if alive_children:
            try:
                _, alive_children = psutil.wait_procs(alive_children, timeout=timeout)
            except Exception as exc:
                tree_confirmed = False
                pm_logger.warning("确认 PresentMon 子进程退出失败: %s", exc)

        try:
            process.wait(timeout=timeout)
        except Exception as exc:
            pm_logger.warning("等待 PresentMon 根进程失败（pid=%s）: %s", getattr(process, "pid", "?"), exc)
            try:
                process.kill()
                process.wait(timeout=timeout)
            except Exception as kill_exc:
                pm_logger.warning(
                    "强制结束 PresentMon 根进程失败（pid=%s）: %s",
                    getattr(process, "pid", "?"),
                    kill_exc,
                )

        try:
            root_exited = process.poll() is not None
        except Exception as exc:
            root_exited = False
            pm_logger.warning("无法确认 PresentMon 根进程状态（pid=%s）: %s", getattr(process, "pid", "?"), exc)

        stopped = tree_confirmed and not alive_children and root_exited
        if not stopped:
            pm_logger.warning(
                "PresentMon 进程树未确认退出，保留句柄以便重试（pid=%s）",
                getattr(process, "pid", "?"),
            )
        return stopped

    def _stop_owned_presentmon_process(self, expected_process=None, timeout=2.0):
        """只停止本 worker 通过 Popen 持有的 PresentMon 进程树，绝不按进程名扫描全机。"""
        with self._presentmon_process_lock:
            process = self.presentmon_process
            if process is None or (
                expected_process is not None and process is not expected_process
            ):
                return False

        stopped = self._terminate_presentmon_process_tree(process, timeout=timeout)
        if not stopped:
            return False

        with self._presentmon_process_lock:
            if self.presentmon_process is process:
                self.presentmon_process = None
        return True

    def _start_presentmon_listener(self):
        import queue
        import logging
        from logging.handlers import RotatingFileHandler
        pm_logger = logging.getLogger("PresentMon_Debugger")
        pm_logger.setLevel(logging.DEBUG)
        if not pm_logger.handlers:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(script_dir, "presentmon_debug.log")
            # 【日志治理】用滚动文件处理器替代裸 FileHandler：单文件封顶 5MB、保留 2 个历史份，
            # 总占用 ≤ ~15MB。此前是无上限 FileHandler，7x24 常驻数月会无声膨胀到 GB 级。
            fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
            fh.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
            pm_logger.addHandler(fh)

        def monitor_loop():
            pm_logger.info("=== PresentMon 守护线程已启动 ===")
            while not self.stop_event.is_set():
                # 【门控】非活跃渲染期(桌面/窗口化轻载)不运行 PresentMon：确保其已被杀掉后等待重判。
                if not self._render_active():
                    self._stop_owned_presentmon_process()
                    time.sleep(3.0)
                    continue
                script_dir = os.path.dirname(os.path.abspath(__file__))
                pm_path = os.path.join(script_dir, "PresentMonConsole.exe")
                if not os.path.exists(pm_path):
                    pm_logger.error(f"未找到可执行文件 {pm_path}")
                    pm_path = "PresentMonConsole.exe"

                if (
                    self.presentmon_process is not None
                    and not self._stop_owned_presentmon_process()
                ):
                    if self.stop_event.wait(3.0):
                        break
                    continue
                if self.stop_event.wait(0.5):
                    break

                cmd = self._presentmon_command(pm_path)
                if not self._begin_presentmon_launch():
                    if self.stop_event.wait(0.1):
                        break
                    continue

                launch_handed_off = False
                try:
                    # 🟢 核心修复：强行注入隐藏窗体配置，彻底切断 PresentMon 与 DWM 缓冲区的视口纠缠
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0  # SW_HIDE
                    
                    process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                        startupinfo=startupinfo,            # 注入底层配置(SW_HIDE)
                        creationflags=subprocess.CREATE_NO_WINDOW,  # 彻底杜绝控制台窗体一闪而过抢焦点
                        cwd=script_dir
                    )
                    launch_handed_off = True
                    if not self._claim_presentmon_process(
                        process, launch_reserved=True
                    ):
                        break
                except Exception as e:
                    if not launch_handed_off:
                        self._finish_presentmon_claim()
                    # 【健壮性修复】此前只接住 FileNotFoundError；但 PresentMon 启动还会抛其它 OSError——
                    # 如非提权环境下的 WinError 740(需要提升)、ETW 会话被占用等。这些异常会击穿 except、
                    # 让整个看门狗线程永久死亡，再不重启 PresentMon。改为兜底退避重试，绝不让守护线程崩溃。
                    pm_logger.error(f"启动 PresentMon 失败，5 秒后重试: {e}")
                    time.sleep(5)
                    continue

                q = queue.Queue()
                
                def reader_thread():
                    try:
                        for line in iter(process.stdout.readline, ''):
                            q.put(line)
                    except Exception:
                        pass
                
                t_reader = threading.Thread(target=reader_thread, daemon=True)
                t_reader.start()

                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                header_map = {}
                ft_idx = None
                app_col_name = "application"

                while not self.stop_event.is_set():
                    try:
                        line = q.get(timeout=1.0)
                        raw_line = ansi_escape.sub('', line).replace('\ufeff', '').strip()
                        if not raw_line: 
                            continue
                            
                        raw_split = raw_line.split(',') if ',' in raw_line else raw_line.split()
                        parts = [p.strip() for p in raw_split]
                        if not parts or not parts[0]: 
                            continue
                        
                        lower_parts = [p.lower() for p in parts]
                        
                        if not header_map and any(kw in lower_parts for kw in ["application", "processname", "processid", "frametime"]):
                            header_map = {col: idx for idx, col in enumerate(lower_parts)}
                            for target in ["msbetweenpresents", "cpuframetime", "frametime", "msbetweendisplaychange"]:
                                if target in header_map:
                                    ft_idx = header_map[target]
                                    break
                            
                            if "application" in header_map:
                                app_col_name = "application"
                            elif "processname" in header_map:
                                app_col_name = "processname"
                            continue
                        
                        if not header_map or app_col_name not in header_map: 
                            continue
                            
                        self._record_presentmon_sample(
                            parts,
                            header_map,
                            app_col_name=app_col_name,
                            ft_idx=ft_idx,
                        )

                    except queue.Empty:
                        if process.poll() is not None:
                            # 🟢 核心修复：一旦 PresentMon 意外崩塌，强制冷冻 3 秒再重启，拒绝高频连击显卡驱动
                            time.sleep(3.0)
                            break
                        if not self._render_active():
                            # 【门控】渲染已停止(退出游戏/回到桌面) → 杀掉 PresentMon，回到外层门控等待。
                            self._stop_owned_presentmon_process(process)
                            break
                        continue
                    except Exception:
                        break

                self._stop_owned_presentmon_process(process)

        t = threading.Thread(target=monitor_loop, daemon=True)
        self.presentmon_thread = t
        t.start()

    def _auto_prepare_lhm_async(self):
        """【自拉取模块】：异步解析 GitHub 发行版列表，拉取并解压 LibreHardwareMonitor 组件"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        lhm_path = os.path.join(script_dir, "LibreHardwareMonitor.exe")
        
        if os.path.exists(lhm_path):
            return

        import urllib.request
        import json
        import zipfile
        import io

        print("[🛸 硬件探针] 本地未找到 LibreHardwareMonitor，正在尝试从 GitHub 动态检索最新发行版...")
        api_url = "https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                
            download_url = None
            for asset in data.get("assets", []):
                # 筛选官方常规发布压缩包，过滤带有.NET等前缀的包
                if asset.get("name") == "LibreHardwareMonitor.zip":
                    download_url = asset.get("browser_download_url")
                    break
                    
            if not download_url:
                # API解析兜底
                download_url = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest/download/LibreHardwareMonitor.zip"
                
            print(f"[🛸 硬件探针] 找到发布包地址，启动多线程下载: {download_url}")
            
            req_dl = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(req_dl, timeout=30) as dl_response:
                zip_data = dl_response.read()
                
            print("[🛸 硬件探针] 文件拉取完成，正在本地解压释放驱动...")
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                z.extractall(script_dir)
                
            print("[🛸 硬件探针] LibreHardwareMonitor 驱动包拉取解压成功，硬件温控并网准备就绪。")
        except Exception as e:
            print(f"[⚠️ 硬件探针] 动态拉取 LibreHardwareMonitor 异常 (可能遭遇脱机或网络抖动): {e}")

    def _read_lhm_port(self):
        # 从 LibreHardwareMonitor.config 读取 Web 服务端口(listenerPort)，缺省 18085。
        try:
            cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LibreHardwareMonitor.config")
            if os.path.exists(cfg):
                with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                    m = re.search(r'key="listenerPort"\s+value="(\d+)"', f.read())
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
        return 18085

    @staticmethod
    def _lhm_num(s):
        # 解析 LHM 值串(如 "0.965 V" / "56.0 °C" / "100.5 W")的前导数值，兼容逗号小数。
        try:
            return float(str(s).strip().split(" ")[0].replace(",", "."))
        except Exception:
            return None

    def _background_lhm_loop(self):
        """通过 LibreHardwareMonitor 内置 Web 服务(/data.json)采集 NVML/PDH 无法可靠提供的真实硬件量：
        CPU Vcore、CPU 封装温度(Tctl/Tdie)与功率、NVIDIA GPU 核心电压与显存结点(热点)温度。
        相比旧的 WMI(root\\LibreHardwareMonitor) 通道：该命名空间在本机并未发布、旧通道长期失败并退化到
        伪造/静态值；HTTP JSON 通道无需 WMI 注册，稳定且为 LHM 官方推荐。

        LHM 是本项目之外的单一运行时 owner（计划任务 ``LibreHardwareMonitor``，
        由 ``telemetry_watchdog.ps1`` 负责恢复）。本 worker 只能读取端点，绝不
        启动、结束或替换 LHM 进程；否则会与计划任务形成双 owner，在显示/HDR
        拓扑变化时同时触发多个 NVML 初始化和驱动重置。"""
        import urllib.request
        import json as _json

        url = "http://127.0.0.1:%d/data.json" % self._read_lhm_port()
        endpoint_down = False

        def flatten(node, path, out):
            text = node.get("Text", "")
            cur = (path + "/" + text) if text else path
            val = node.get("Value", "")
            children = node.get("Children", [])
            if val and not children:
                out[cur.lower()] = val
            for c in children:
                flatten(c, cur, out)

        try:
            while not self.stop_event.is_set():
                cpu_temp = cpu_power = cpu_vcore = gpu_voltage = gpu_hotspot = None
                json_ok = False
                try:
                    with urllib.request.urlopen(url, timeout=1.5) as resp:
                        flat = {}
                        flatten(_json.loads(resp.read().decode("utf-8", "ignore")), "", flat)
                    json_ok = True

                    for key, raw in flat.items():
                        if key.endswith("/voltages/vcore"):
                            cpu_vcore = self._lhm_num(raw)
                        elif key.endswith("/powers/package") and "gpu" not in key:
                            cpu_power = self._lhm_num(raw)
                        elif "/temperatures/" in key and "core (tctl/tdie)" in key:
                            cpu_temp = self._lhm_num(raw)
                        elif "nvidia" in key and key.endswith("gpu core voltage"):
                            gpu_voltage = self._lhm_num(raw)
                        elif "nvidia" in key and "/temperatures/" in key and ("hot spot" in key or "junction" in key):
                            v = self._lhm_num(raw)
                            # 优先真正的核心热点(Hot Spot)，否则采用显存结点(Memory Junction)。
                            if v is not None and (gpu_hotspot is None or "hot spot" in key):
                                gpu_hotspot = v
                except Exception:
                    pass

                if json_ok:
                    endpoint_down = False
                else:
                    if not endpoint_down:
                        print("[⚠️ 硬件探针] LHM Web 端点不可用；由外部 telemetry watchdog 负责恢复，当前样本留空。")
                        endpoint_down = True

                with self.wmi_lock:
                    self.cached_wmi_temp = cpu_temp
                    self.cached_wmi_power = cpu_power
                    self.cached_cpu_vcore = cpu_vcore
                    self.cached_gpu_voltage = gpu_voltage
                    self.cached_gpu_hotspot = gpu_hotspot

                for _ in range(10):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
        finally:
            # LHM 由外部 owner 管理；此处只结束本 worker 的读取线程。
            pass

    def _read_gpu_throttle_reasons(self):
        """隔离的降频原因读取。GeForce 同样支持该接口(本机 NVIDIA 驱动实测返回 0x0，AI 所谓"仅
        Tesla/Quadro 支持→GeForce 必崩"的结论被实机证伪)，但不同驱动/pynvml 版本里该函数已从
        ...ThrottleReasons 改名为 ...EventReasons。任一失败都返回 0 且绝不上抛——防止单个非致命
        调用拖垮整块 GPU 采集并触发 nvmlShutdown 级联重载，把温度/时钟/占用全部清零。"""
        for fn_name in ("nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons"):
            fn = getattr(pynvml, fn_name, None)
            if fn is None:
                continue
            try:
                return int(fn(self.gpu_handle)) & 0x7FFF
            except Exception:
                continue
        return 0

    def _read_gpu_pcie_util(self):
        """隔离的 PCIe 吞吐利用率读取；任一失败返回 0，不连累其余 GPU 指标。"""
        try:
            tx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_TX_BYTES)
            rx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_RX_BYTES)
            return ((tx_bytes + rx_bytes) / 64000000.0) * 100.0
        except Exception:
            return 0.0

    def _sample_nvml_gpu_metrics(self):
        """采集一拍 NVML GPU 指标并返回字典，内含 GDDR7 显存结温(sensor=1)与核心热点的合并保护
        gpu_hotspot = max(core+12, mem_junction)。易在特定驱动上抛异常的降频原因/PCIe 调用已各自
        隔离，故只有核心 util/温度/功率/时钟真正失败才会卸载并重置 NVML(交后台循环重初始化)。
        抽成独立方法亦便于测试在不依赖后台线程时序的前提下确定性校验热点合并逻辑(test_04)。"""
        res = {
            "gpu_usage": 0.0, "gpu_core_voltage": 0.0, "gpu_core_clock": 0, "gpu_mem_clock": 0,
            "gpu_core_temp": 0.0, "gpu_hotspot_temp": 0.0, "gpu_board_power": 0.0,
            "gpu_throttling_reasons": 0, "pcie_bus_utilization": 0.0,
        }
        if not self.nvml_initialized:
            return res
        with NVML_LOCK:
            try:
                res["gpu_usage"] = float(pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle).gpu)
                core_temp = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, 0))
                res["gpu_core_temp"] = core_temp

                mem_temp = None
                try:
                    mem_temp = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, 1))
                except Exception:
                    pass
                hotspot = core_temp + 12.0
                if mem_temp is not None:
                    hotspot = max(hotspot, mem_temp)
                res["gpu_hotspot_temp"] = hotspot

                res["gpu_board_power"] = float(pynvml.nvmlDeviceGetPowerUsage(self.gpu_handle)) / 1000.0
                res["gpu_core_clock"] = int(pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, NVML_CLOCK_GRAPHICS))
                # 列类型为 integer，NVML 返回真实显存时钟(MHz)；去掉旧的 smallint(32767)截断 bug。
                mem_clock = int(pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, NVML_CLOCK_MEM))
                res["gpu_mem_clock"] = mem_clock if 0 <= mem_clock <= 100000 else 0

                # 占位：下游 collect_hardware_snapshot 用 LHM 真实核心电压覆盖(NVML 在 GeForce 无法提供)。
                res["gpu_core_voltage"] = 0.0
                # 易在特定驱动/版本上抛异常的非核心调用——各自隔离，绝不连累上面已取得的核心指标。
                res["gpu_throttling_reasons"] = self._read_gpu_throttle_reasons()
                res["pcie_bus_utilization"] = self._read_gpu_pcie_util()
            except Exception:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass
                self.nvml_initialized = False
        return res

    def _background_pdh_loop(self):
        pdh_fail_count = 0
        
        while not self.stop_event.is_set():
            cpu_percents = [0.0] * 32
            cpu_total = 0.0
            try:
                cpu_percents = psutil.cpu_percent(interval=None, percpu=True) or [0.0]
                cpu_total = sum(cpu_percents) / len(cpu_percents) if cpu_percents else 0.0
            except Exception:
                pass

            cpu_mhz = 4300
            cpu_package_temp = None
            cpu_package_power = None
            system_hard_page_faults = 0
            system_context_switches_rate = 0
            disk_max_latency_ms = 0.0

            if self.pdh_query:
                try:
                    if ctypes.windll.pdh.PdhCollectQueryData(self.pdh_query) == 0:
                        pdh_fail_count = 0
                        type_val = ctypes.c_ulong()
                        class PDH_FMT_COUNTERVALUE_DOUBLE_L(ctypes.Structure):
                            _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]
                        
                        def get_val(h):
                            if not h: return None
                            v = PDH_FMT_COUNTERVALUE_DOUBLE_L()
                            res = ctypes.windll.pdh.PdhGetFormattedCounterValue(h, 0x00000200, ctypes.byref(type_val), ctypes.byref(v))
                            return v.doubleValue if res == 0 else None

                        base_freq = get_val(self.h_base_freq) or 4300.0
                        perf_ratio = get_val(self.h_perf_pct) or 100.0
                        cpu_mhz = int(base_freq * (perf_ratio / 100.0))

                        t_val = get_val(self.h_temp)
                        if t_val:
                            cpu_package_temp = (t_val / 10.0) - 273.15 if t_val > 1000 else t_val
                        
                        p_val = get_val(self.h_power)
                        if p_val:
                            cpu_package_power = p_val / 1000.0 if p_val > 1000 else p_val

                        system_hard_page_faults = int(get_val(self.h_hard_faults) or 0)
                        system_context_switches_rate = int(
                            max(0.0, get_val(self.h_context_switches) or 0.0)
                        )

                        d_val = get_val(self.h_disk_latency)
                        if d_val:
                            disk_max_latency_ms = d_val * 1000.0
                    else:
                        pdh_fail_count += 1
                except Exception:
                    pdh_fail_count += 1

                if pdh_fail_count >= 3:
                    print("[⚠️ 硬件探针] PDH 查询句柄连续失效，可能遭遇系统睡眠唤醒，正在尝试重建 PDH 观测引擎...")
                    try:
                        ctypes.windll.pdh.PdhCloseQuery(self.pdh_query)
                    except Exception:
                        pass
                    self.pdh_query = None
                    self._init_pdh_cppc_engine()
                    pdh_fail_count = 0
            else:
                self._init_pdh_cppc_engine()

            if self.nvml_initialized:
                g = self._sample_nvml_gpu_metrics()
                gpu_usage = g["gpu_usage"]
                gpu_voltage_est = g["gpu_core_voltage"]
                gpu_core_clock = g["gpu_core_clock"]
                gpu_mem_clock = g["gpu_mem_clock"]
                gpu_core_temp = g["gpu_core_temp"]
                gpu_hotspot_temp = g["gpu_hotspot_temp"]
                gpu_board_power = g["gpu_board_power"]
                gpu_throttling_reasons = g["gpu_throttling_reasons"]
                pcie_bus_utilization = g["pcie_bus_utilization"]
            else:
                gpu_usage = 0.0
                gpu_voltage_est = 0.0
                gpu_core_clock = 0
                gpu_mem_clock = 0
                gpu_core_temp = 0.0
                gpu_hotspot_temp = 0.0
                gpu_board_power = 0.0
                gpu_throttling_reasons = 0
                pcie_bus_utilization = 0.0
                self._init_nvml()

            # 【PresentMon 门控】GPU 占用高=有游戏/3D 在跑，记录时刻供 PresentMon 看门狗判断是否该运行。
            if gpu_usage is not None and gpu_usage > self.RENDER_GPU_THRESHOLD:
                self._last_render_ts = time.monotonic()

            with self.pdh_lock:
                self.cached_pdh_data["cpu_mhz"] = cpu_mhz
                self.cached_pdh_data["cpu_package_temp"] = cpu_package_temp
                self.cached_pdh_data["cpu_package_power"] = cpu_package_power
                self.cached_pdh_data["system_hard_page_faults"] = system_hard_page_faults
                self.cached_pdh_data["system_context_switches_rate"] = system_context_switches_rate
                self.cached_pdh_data["disk_max_latency_ms"] = disk_max_latency_ms
                
                self.cached_pdh_data["cpu_percents"] = cpu_percents
                self.cached_pdh_data["cpu_total_usage"] = cpu_total
                
                self.cached_pdh_data["gpu_usage"] = gpu_usage
                self.cached_pdh_data["gpu_core_voltage"] = gpu_voltage_est
                self.cached_pdh_data["gpu_core_clock"] = gpu_core_clock
                self.cached_pdh_data["gpu_mem_clock"] = gpu_mem_clock
                self.cached_pdh_data["gpu_core_temp"] = gpu_core_temp
                self.cached_pdh_data["gpu_hotspot_temp"] = gpu_hotspot_temp
                self.cached_pdh_data["gpu_board_power"] = gpu_board_power
                self.cached_pdh_data["gpu_throttling_reasons"] = gpu_throttling_reasons
                self.cached_pdh_data["pcie_bus_utilization"] = pcie_bus_utilization
            
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)

    @staticmethod
    def _get_commit_charge_gb():
        # 真实"提交内存"(Committed Bytes，与任务管理器"已提交"一致) = 提交上限 - 可提交余量。
        # 旧实现误用 swap_memory().used(仅页面文件占用)，严重偏小且语义错误。
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return max(0.0, (m.ullTotalPageFile - m.ullAvailPageFile) / (1024 ** 3))
        except Exception:
            pass
        return 0.0

    def collect_hardware_snapshot(self, foreground_app_name, foreground_pid=None):
        self.active_foreground_app = foreground_app_name if foreground_app_name else ""
        self.active_foreground_pid = (
            self._parse_presentmon_process_id(foreground_pid)
            if foreground_pid is not None
            else self._read_foreground_pid()
        )
        now_ts = time.monotonic()
        dt = max(0.001, now_ts - self.last_ts)
        self.last_ts = now_ts
        
        with self.pdh_lock:
            cpu_mhz = self.cached_pdh_data["cpu_mhz"]
            cpu_package_temp = self.cached_pdh_data["cpu_package_temp"]
            cpu_package_power = self.cached_pdh_data["cpu_package_power"]
            system_hard_page_faults = self.cached_pdh_data["system_hard_page_faults"]
            system_context_switches_rate = self.cached_pdh_data["system_context_switches_rate"]
            disk_max_latency_ms = self.cached_pdh_data["disk_max_latency_ms"]
            
            cpu_percents = self.cached_pdh_data["cpu_percents"]
            cpu_total = self.cached_pdh_data["cpu_total_usage"]
            
            gpu_usage = self.cached_pdh_data["gpu_usage"]
            gpu_core_voltage = self.cached_pdh_data["gpu_core_voltage"]
            gpu_core_clock = self.cached_pdh_data["gpu_core_clock"]
            gpu_mem_clock = self.cached_pdh_data["gpu_mem_clock"]
            gpu_core_temp = self.cached_pdh_data["gpu_core_temp"]
            gpu_hotspot_temp = self.cached_pdh_data["gpu_hotspot_temp"]
            gpu_board_power = self.cached_pdh_data["gpu_board_power"]
            gpu_throttling_reasons = self.cached_pdh_data["gpu_throttling_reasons"]
            pcie_bus_utilization = self.cached_pdh_data["pcie_bus_utilization"]

        with self.wmi_lock:
            lhm_temp = self.cached_wmi_temp
            lhm_power = self.cached_wmi_power
            cpu_vcore = self.cached_cpu_vcore
            lhm_gpu_voltage = self.cached_gpu_voltage
            lhm_gpu_hotspot = self.cached_gpu_hotspot

        # CPU 封装温度/功率：优先 LHM 真实读数(Tctl/Tdie、Package)，其次 PDH(ACPI 热区/电表)，最后合成兜底。
        if lhm_temp is not None:
            cpu_package_temp = lhm_temp
        elif cpu_package_temp is None:
            cpu_package_temp = 39.0 + (cpu_total * 0.46)

        if lhm_power is not None:
            cpu_package_power = lhm_power
        elif cpu_package_power is None:
            cpu_package_power = 24.0 + (cpu_total * 1.46)

        # GPU 核心电压：NVML 在 GeForce 无法提供，仅采用 LHM 真实读数，无则置空(NULL)，不再伪造。
        gpu_core_voltage = lhm_gpu_voltage
        # GPU 热点温度：优先 LHM 显存结点真实温度，否则退化为 NVML 估算(core+12)。
        if lhm_gpu_hotspot is not None:
            gpu_hotspot_temp = lhm_gpu_hotspot

        ccd0_load = 0.0
        ccd1_load = 0.0
        try:
            if len(cpu_percents) >= 32:
                ccd0_load = sum(cpu_percents[0:16]) / 16.0
                ccd1_load = sum(cpu_percents[16:32]) / 16.0
                is_rendering = any(x in self.active_foreground_app.lower() for x in ["game", "steam", "dx11", "dx12", "vk"])
                if is_rendering and ccd1_load > 40.0 and ccd0_load < 5.0:
                    print(f"[{datetime.datetime.now().strftime('%X')} ⚠️ 调度警报] 9950X3D 调度失重！")
        except Exception: 
            pass

        # psutil 7.2.2 frees its Windows CPU-statistics buffer before reading
        # ContextSwitches/SystemCalls.  Use the localized-safe PDH rate instead.
        ctx_rate = int(max(0, system_context_switches_rate))
        
        ram_pct = psutil.virtual_memory().percent
        commit_gb = self._get_commit_charge_gb()

        system_dpc_latency = self.dpc_checker.get_latency_us()

        current_fps = average_fps = one_percent_low_fps = frametime_ms = frametime_jitter = None
        frametimes = self._select_presentmon_window(
            self.active_foreground_app,
            self.active_foreground_pid,
        )
        if frametimes:
            sorted_ft = sorted(frametimes)
            low_99_idx = min(len(sorted_ft) - 1, int(len(sorted_ft) * 0.99))
            ft = frametimes[-1]

            current_fps = 1000.0 / ft
            one_percent_low_fps = 1000.0 / sorted_ft[low_99_idx]
            average_fps = 1000.0 / (sum(frametimes) / len(frametimes))
            frametime_ms = ft
            frametime_jitter = abs(frametimes[-1] - frametimes[-2]) if len(frametimes) > 1 else 0.0

        return {
            "current_fps": current_fps if current_fps is not None else 0.0,
            "average_fps": average_fps if average_fps is not None else 0.0,
            "one_percent_low_fps": one_percent_low_fps if one_percent_low_fps is not None else 0.0,
            "frametime_ms": frametime_ms if frametime_ms is not None else 0.0,
            "frametime_jitter": frametime_jitter if frametime_jitter is not None else 0.0,
            
            "cpu_total_usage": cpu_total,
            "cpu_vcore_voltage": cpu_vcore,   # LHM 主板真实 Vcore；不可用时写 NULL，绝不伪造
            "cpu_clock_mhz": cpu_mhz,
            "cpu_package_temp": cpu_package_temp,
            "cpu_package_power": cpu_package_power, 
            "system_dpc_latency": system_dpc_latency,
            "system_context_switches": ctx_rate, 
            "system_ram_usage_pct": ram_pct,
            "system_commit_size_gb": commit_gb, 
            "system_hard_page_faults": system_hard_page_faults,
            "gpu_usage": gpu_usage if gpu_usage is not None else 0.0,
            "gpu_core_voltage": gpu_core_voltage,   # LHM(NVAPI) NVIDIA GPU 真实核心电压；不可用时写 NULL
            "gpu_core_clock": gpu_core_clock if gpu_core_clock is not None else 0,
            "gpu_mem_clock": gpu_mem_clock if gpu_mem_clock is not None else 0,
            "gpu_core_temp": gpu_core_temp if gpu_core_temp is not None else 0.0, 
            "gpu_hotspot_temp": gpu_hotspot_temp if gpu_hotspot_temp is not None else 0.0, 
            "gpu_board_power": gpu_board_power if gpu_board_power is not None else 0.0, 
            "gpu_throttling_reasons": gpu_throttling_reasons if gpu_throttling_reasons is not None else 0,
            "pcie_bus_utilization": pcie_bus_utilization if pcie_bus_utilization is not None else 0.0, 
            "disk_max_latency_ms": disk_max_latency_ms if disk_max_latency_ms is not None else 0.0,
            "network_ping_ms": self.network_metrics["ping_ms"], 
            "is_packet_loss": self.network_metrics["packet_loss"], 
            "network_jitter": self.network_metrics["jitter"],
            "cpu_ccd0_usage": ccd0_load,
            "cpu_ccd1_usage": ccd1_load
        }

    async def write_to_db(self, pool, data, timestamp=None):
        if timestamp is None:
            timestamp = datetime.datetime.now(datetime.timezone.utc)
            
        ccd0_load = data.get("cpu_ccd0_usage", 0.0)
        ccd1_load = data.get("cpu_ccd1_usage", 0.0)

        query = """
            INSERT INTO public.fact_system_hardware 
            ("timestamp", current_fps, average_fps, one_percent_low_fps, frametime_ms, frametime_jitter,
             cpu_total_usage, cpu_vcore_voltage, cpu_clock_mhz, cpu_package_temp, cpu_package_power, 
             system_dpc_latency, system_context_switches, gpu_usage, gpu_core_voltage, gpu_core_clock, gpu_mem_clock, 
             gpu_core_temp, gpu_hotspot_temp, gpu_board_power, gpu_throttling_reasons, pcie_bus_utilization,
             system_ram_usage_pct, system_commit_size_gb, system_hard_page_faults, disk_max_latency_ms,
             network_ping_ms, is_packet_loss, network_jitter, cpu_ccd0_usage, cpu_ccd1_usage)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31);
        """
        async with pool.acquire() as conn:
            await conn.execute(
                query, timestamp, data["current_fps"], data["average_fps"], data["one_percent_low_fps"],
                data["frametime_ms"], data["frametime_jitter"], data["cpu_total_usage"], data["cpu_vcore_voltage"],
                data["cpu_clock_mhz"], data["cpu_package_temp"], data["cpu_package_power"],
                data["system_dpc_latency"], data["system_context_switches"], data["gpu_usage"], data["gpu_core_voltage"],
                data["gpu_core_clock"], data["gpu_mem_clock"], data["gpu_core_temp"], data["gpu_hotspot_temp"],
                data["gpu_board_power"], data["gpu_throttling_reasons"], data["pcie_bus_utilization"],
                data["system_ram_usage_pct"], data["system_commit_size_gb"], data["system_hard_page_faults"],
                data["disk_max_latency_ms"], data["network_ping_ms"], 
                1 if data["is_packet_loss"] else 0, data["network_jitter"],
                ccd0_load, ccd1_load
            )

    def terminate(self):
        with self._presentmon_process_condition:
            self.stop_event.set()
        self.dpc_checker.stop()
        if hasattr(self, 'presentmon_thread') and self.presentmon_thread:
            try:
                self.presentmon_thread.join(timeout=2.0)
            except Exception:
                pass
        # join 只用于尽快回收线程，不作为安全门。真正的门是启动/claim 事务归零：
        # stop 已在同一 Condition 下发布，因此不会再产生新的合法启动预约。
        with self._presentmon_process_condition:
            while self._presentmon_claims_inflight:
                self._presentmon_process_condition.wait()
        if hasattr(self, 'presentmon_process'):
            self._stop_owned_presentmon_process(timeout=1.0)
        
        if hasattr(self, 'pdh_thread') and self.pdh_thread:
            try:
                self.pdh_thread.join(timeout=1.0)
            except Exception:
                pass
            self.pdh_thread = None

        if self.nvml_initialized:
            with NVML_LOCK:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass
                self.nvml_initialized = False
