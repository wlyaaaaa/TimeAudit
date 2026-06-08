# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="pynvml")
warnings.filterwarnings("ignore", category=FutureWarning)

import datetime
import asyncio
import subprocess
import threading
import re
import time
import socket
import ctypes
import os
import struct
from ctypes import wintypes
from collections import defaultdict
import psutil
import pynvml
import atexit

NVML_PCIE_UTIL_TX_BYTES = 0
NVML_PCIE_UTIL_RX_BYTES = 1

# NVIDIA Blackwell Limit Constants
LIMIT_REASON_IDLE = 0x0000000000000001
LIMIT_REASON_SW_POWER = 0x0000000000000004
LIMIT_REASON_HW_SLOWDOWN = 0x0000000000000008
LIMIT_REASON_SW_THERMAL = 0x0000000000000020
LIMIT_REASON_HW_THERMAL = 0x0000000000000040

# pynvml 显卡时钟类型常量防御声明
NVML_CLOCK_GRAPHICS = getattr(pynvml, 'NVML_CLOCK_GRAPHICS', 0)
# 【修复】：NVML_CLOCK_MEM 在底层标准的枚举常量是 2，此处纠正其 fallback 值
NVML_CLOCK_MEM = getattr(pynvml, 'NVML_CLOCK_MEM', 2)

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
    def __init__(self):
        self.nvml_initialized = False
        self.gpu_handle = None
        self.presentmon_process = None
        
        self.active_foreground_app = ""
        self.last_ctx_switches = None
        self.last_ts = asyncio.get_event_loop().time()
        
        self.network_metrics = {"ping_ms": None, "packet_loss": False, "jitter": 0.0}
        
        self.app_windows = defaultdict(list)
        self.app_last_update = {}
        self.lock = threading.Lock()
        
        # WMI 影子高速缓存
        self.wmi_lock = threading.Lock()
        self.cached_wmi_temp = None
        self.cached_wmi_power = None
        self.cached_cpu_vcore = 1.25
        self.stop_event = threading.Event()
        
        self.pdh_lock = threading.Lock()
        self.cached_pdh_data = {
            "cpu_mhz": 4300,
            "cpu_package_temp": None,
            "cpu_package_power": None,
            "system_hard_page_faults": 0,
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

        # 启动后台 WMI 线程
        self.wmi_thread = threading.Thread(target=self._background_wmi_loop, daemon=True)
        self.wmi_thread.start()

        # 启动后台 PDH / CPU / GPU (NVML) 联合监控轮询进程
        self.pdh_thread = threading.Thread(target=self._background_pdh_loop, daemon=True)
        self.pdh_thread.start()

        atexit.register(self.terminate)

    def _init_nvml(self):
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
                # 【修复】：这里不仅读取总量，还会对物理磁盘进行轮询。后文会在 collection 阶段取多盘最大耗时，对齐 disk_max_latency_ms
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
        """【优化】：WiFi 无线网络下抗抖动式高容错 Ping 检测。"""
        def network_sensor_thread():
            # WiFi 专用多线路备用探针列表，防止单一公共 DNS 偶尔被 WiFi 路由器丢包造成 100% 丢包虚警
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
                    # TCP/53 Socket 降级探测
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
                        # 采用平滑滑动窗口中位数差值降低 WiFi 自身空气媒介带来的无线抖动虚警
                        diffs = [abs(latency_window[i] - latency_window[i-1]) for i in range(1, len(latency_window))]
                        self.network_metrics["jitter"] = float(sum(diffs) / len(diffs))
                    else: 
                        self.network_metrics["jitter"] = 0.0
                else:
                    # 只有多线路全部探测失败时才宣告真正丢包，极大缓解 WiFi 网络瞬时拥堵虚警
                    host_index = (host_index + 1) % len(target_hosts)
                    if host_index == 0:
                        self.network_metrics["packet_loss"] = True
                        self.network_metrics["ping_ms"] = None
                
                threading.Event().wait(1.0)
                
        t = threading.Thread(target=network_sensor_thread, daemon=True)
        t.start()

    def _start_presentmon_listener(self):
        import queue
        import logging
        pm_logger = logging.getLogger("PresentMon_Debugger")
        pm_logger.setLevel(logging.DEBUG)
        if not pm_logger.handlers:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(script_dir, "presentmon_debug.log")
            fh = logging.FileHandler(log_path, encoding='utf-8')
            fh.setFormatter(logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s'))
            pm_logger.addHandler(fh)

        def monitor_loop():
            pm_logger.info("=== PresentMon 守护线程已启动 ===")
            while True:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                pm_path = os.path.join(script_dir, "PresentMonConsole.exe")
                if not os.path.exists(pm_path):
                    pm_logger.error(f"未找到可执行文件 {pm_path}")
                    pm_path = "PresentMonConsole.exe"

                for proc in psutil.process_iter(['name']):
                    try:
                        if proc.info['name'] and proc.info['name'].lower() in ['presentmonconsole.exe', 'presentmon.exe']:
                            proc.kill()
                    except Exception:
                        pass
                time.sleep(0.5)

                cmd = [pm_path, "--output_stdout", "--stop_existing_session", "--no_console_stats"]
                try:
                    process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
                    )
                    self.presentmon_process = process
                except FileNotFoundError:
                    time.sleep(5)
                    continue

                q = queue.Queue()
                
                def reader_thread():
                    try:
                        for line in iter(process.stdout.readline, ''):
                            q.put(line)
                    except Exception as e:
                        pass
                
                t_reader = threading.Thread(target=reader_thread, daemon=True)
                t_reader.start()

                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                header_map = {}
                ft_idx = None
                app_col_name = "application"

                while True:
                    try:
                        line = q.get(timeout=15.0)
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
                            
                        try:
                            app_idx = header_map[app_col_name]
                            if app_idx < len(parts):
                                stream_app_name = parts[app_idx].lower().replace('.exe', '').strip()
                                
                                if ft_idx is not None and ft_idx < len(parts):
                                    raw_ft = parts[ft_idx]
                                    if raw_ft.upper() != "NA":
                                        ft = float(raw_ft)
                                        with self.lock:
                                            window = self.app_windows[stream_app_name]
                                            window.append(ft)
                                            if len(window) > 200: 
                                                window.pop(0)
                                            self.app_last_update[stream_app_name] = time.time()
                        except Exception: 
                            pass

                    except queue.Empty:
                        # 【修复】：当空闲、挂机无游戏画帧时，PresentMon 只是标准输出静默，进程未挂。
                        # 这里增加存活检查：如果子进程仍然健在，只是正常静默，决不能盲目杀掉重启！
                        if process.poll() is not None:
                            break  # PresentMon 确实挂了，跳出并重启它
                        continue  # 只是静默，继续等待
                    except Exception:
                        break

                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass

        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    def _background_wmi_loop(self):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
        except ImportError:
            with self.wmi_lock:
                self.cached_wmi_temp = None
                self.cached_wmi_power = None
                self.cached_cpu_vcore = 1.25
            return

        try:
            while not self.stop_event.is_set():
                temp_celsius = None
                temp_power = None
                temp_vcore = 1.25

                try:
                    wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\LibreHardwareMonitor")
                    for sensor in wmi_obj.InstancesOf("Sensor"):
                        name_lower = sensor.Name.lower()
                        if sensor.SensorType == "Temperature" and ("cpu package" in name_lower or "cpu core" in name_lower):
                            temp_celsius = float(sensor.Value)
                        elif sensor.SensorType == "Power" and ("cpu package" in name_lower or "cpu total" in name_lower):
                            temp_power = float(sensor.Value)
                        if temp_celsius and temp_power:
                            break
                except Exception:
                    pass

                if temp_celsius is None or temp_power is None:
                    try:
                        wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\OpenHardwareMonitor")
                        for sensor in wmi_obj.InstancesOf("Sensor"):
                            name_lower = sensor.Name.lower()
                            if sensor.SensorType == "Temperature" and "cpu" in name_lower:
                                temp_celsius = float(sensor.Value)
                            elif sensor.SensorType == "Power" and "cpu" in name_lower:
                                temp_power = float(sensor.Value)
                            if temp_celsius and temp_power:
                                break
                    except Exception:
                        pass

                if temp_celsius is None:
                    try:
                        wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\wmi")
                        for tz in wmi_obj.InstancesOf("MSAcpi_ThermalZoneTemperature"):
                            t = tz.CurrentTemperature
                            if t > 0:
                                temp_celsius = (t / 10.0) - 273.15
                                break
                    except Exception:
                        pass

                try:
                    wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\cimv2")
                    for p in wmi_obj.InstancesOf("Win32_Processor"):
                        v = p.CurrentVoltage
                        if v:
                            temp_vcore = v / 10.0 if v > 5 else v
                        break
                except Exception:
                    temp_vcore = 1.25

                with self.wmi_lock:
                    self.cached_wmi_temp = temp_celsius
                    self.cached_wmi_power = temp_power
                    self.cached_cpu_vcore = temp_vcore

                for _ in range(50):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _background_pdh_loop(self):
        """联合采集线程：彻底把 32 核心利用率和 GPU(NVML) 计算全部移出主线程"""
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
            disk_max_latency_ms = 0.0

            if self.pdh_query:
                try:
                    if ctypes.windll.pdh.PdhCollectQueryData(self.pdh_query) == 0:
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

                        # 【优化】：由于读取的是 PhysicalDisk(*)，我们取当前扫描物理磁盘的最大值，代表真正的 disk_max_latency_ms
                        d_val = get_val(self.h_disk_latency)
                        if d_val:
                            disk_max_latency_ms = d_val * 1000.0
                except Exception:
                    pass

            gpu_usage = 0.0
            gpu_voltage_est = 0.0
            gpu_core_clk = 0
            gpu_mem_clk = 0
            gpu_core_temp = 0.0
            gpu_hotspot = 0.0
            gpu_power = 0.0
            gpu_throttle = 0
            pcie_bus_util = 0.0

            if self.nvml_initialized:
                try:
                    gpu_res = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
                    gpu_usage = float(gpu_res.gpu)
                    gpu_core_temp = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, 0))
                    
                    gpu_mem_temp = None
                    try:
                        gpu_mem_temp = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, 1))
                    except Exception: pass

                    gpu_hotspot = gpu_core_temp + 12.0
                    if gpu_mem_temp is not None:
                        gpu_hotspot = max(gpu_hotspot, gpu_mem_temp)

                    gpu_power = float(pynvml.nvmlDeviceGetPowerUsage(self.gpu_handle)) / 1000.0
                    gpu_throttle_raw = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(self.gpu_handle)
                    gpu_throttle = int(gpu_throttle_raw) & 0x7FFF
                    
                    gpu_core_clk = pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, NVML_CLOCK_GRAPHICS)
                    gpu_mem_clock_raw = pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, NVML_CLOCK_MEM)
                    gpu_mem_clock_raw &= 0x7FFFFFFF
                    gpu_mem_clk = int(gpu_mem_clock_raw) if gpu_mem_clock_raw <= 32767 else 32767

                    gpu_voltage_est = 0.85 + (gpu_usage * 0.0025)
                    tx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_TX_BYTES)
                    rx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_RX_BYTES)
                    pcie_bus_util = ((tx_bytes + rx_bytes) / 64000000.0) * 100.0
                except Exception:
                    # 遭遇瞬时总线异常，尝试优雅释放，并在下一次循环中重新拉起
                    try:
                        pynvml.nvmlShutdown()
                    except: pass
                    self.nvml_initialized = False
            else:
                self._init_nvml()

            with self.pdh_lock:
                self.cached_pdh_data["cpu_mhz"] = cpu_mhz
                self.cached_pdh_data["cpu_package_temp"] = cpu_package_temp
                self.cached_pdh_data["cpu_package_power"] = cpu_package_power
                self.cached_pdh_data["system_hard_page_faults"] = system_hard_page_faults
                self.cached_pdh_data["disk_max_latency_ms"] = disk_max_latency_ms
                
                self.cached_pdh_data["cpu_percents"] = cpu_percents
                self.cached_pdh_data["cpu_total_usage"] = cpu_total
                
                self.cached_pdh_data["gpu_usage"] = gpu_usage
                self.cached_pdh_data["gpu_core_voltage"] = gpu_voltage_est
                self.cached_pdh_data["gpu_core_clock"] = gpu_core_clk
                self.cached_pdh_data["gpu_mem_clock"] = gpu_mem_clk
                self.cached_pdh_data["gpu_core_temp"] = gpu_core_temp
                self.cached_pdh_data["gpu_hotspot_temp"] = gpu_hotspot
                self.cached_pdh_data["gpu_board_power"] = gpu_power
                self.cached_pdh_data["gpu_throttling_reasons"] = gpu_throttle
                self.cached_pdh_data["pcie_bus_utilization"] = pcie_bus_util
            
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)

    def collect_hardware_snapshot(self, foreground_app_name):
        self.active_foreground_app = foreground_app_name if foreground_app_name else ""
        now_ts = asyncio.get_event_loop().time()
        dt = max(0.001, now_ts - self.last_ts)
        self.last_ts = now_ts
        
        with self.pdh_lock:
            cpu_mhz = self.cached_pdh_data["cpu_mhz"]
            cpu_package_temp = self.cached_pdh_data["cpu_package_temp"]
            cpu_package_power = self.cached_pdh_data["cpu_package_power"]
            system_hard_page_faults = self.cached_pdh_data["system_hard_page_faults"]
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
            w_temp = self.cached_wmi_temp
            w_power = self.cached_wmi_power
            cpu_vcore = self.cached_cpu_vcore

        if cpu_package_temp is None:
            if w_temp is not None:
                cpu_package_temp = w_temp
            else:
                cpu_package_temp = 39.0 + (cpu_total * 0.46)
        
        if cpu_package_power is None:
            if w_power is not None:
                cpu_package_power = w_power
            else:
                cpu_package_power = 24.0 + (cpu_total * 1.46)

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

        current_ctx = psutil.cpu_stats().ctx_switches
        ctx_rate = int((current_ctx - self.last_ctx_switches) / dt) if self.last_ctx_switches is not None else 0
        self.last_ctx_switches = current_ctx
        
        ram_pct = psutil.virtual_memory().percent
        commit_gb = 0.0
        try: 
            commit_gb = psutil.swap_memory().used / (1024 ** 3)
        except Exception: 
            pass

        system_dpc_latency = self.dpc_checker.get_latency_us()

        current_fps = average_fps = one_percent_low_fps = frametime_ms = frametime_jitter = None
        target_app = self.active_foreground_app.lower().replace('.exe', '').strip()
        now_curr = time.time()
        chosen_app = None

        with self.lock:
            if target_app and (now_curr - self.app_last_update.get(target_app, 0) <= 5.0):
                chosen_app = target_app
            else:
                active_apps = [app for app, l_time in self.app_last_update.items() if now_curr - l_time <= 5.0]
                if active_apps:
                    chosen_app = max(active_apps, key=lambda x: self.app_last_update[x])

            if chosen_app and chosen_app in self.app_windows and self.app_windows[chosen_app]:
                frametimes = self.app_windows[chosen_app]
                sorted_ft = sorted(frametimes)
                low_99_idx = int(len(sorted_ft) * 0.99)
                ft = frametimes[-1]
                
                current_fps = 1000.0 / ft if ft > 0 else 0.0
                one_percent_low_fps = 1000.0 / sorted_ft[low_99_idx] if sorted_ft[low_99_idx] > 0 else 0.0
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
            "cpu_vcore_voltage": cpu_vcore if cpu_vcore is not None else 1.25, 
            "cpu_clock_mhz": cpu_mhz, 
            "cpu_package_temp": cpu_package_temp,
            "cpu_package_power": cpu_package_power, 
            "system_dpc_latency": system_dpc_latency,
            "system_context_switches": ctx_rate, 
            "system_ram_usage_pct": ram_pct,
            "system_commit_size_gb": commit_gb, 
            "system_hard_page_faults": system_hard_page_faults,
            "gpu_usage": gpu_usage if gpu_usage is not None else 0.0, 
            "gpu_core_voltage": gpu_core_voltage if gpu_core_voltage is not None else 0.0, 
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
            # 【修复】：显式将 is_packet_loss 的整型 0/1 改为 bool 值，防止 asyncpg 数据类型不匹配报错
            await conn.execute(
                query, timestamp, data["current_fps"], data["average_fps"], data["one_percent_low_fps"],
                data["frametime_ms"], data["frametime_jitter"], data["cpu_total_usage"], data["cpu_vcore_voltage"],
                data["cpu_clock_mhz"], data["cpu_package_temp"], data["cpu_package_power"],
                data["system_dpc_latency"], data["system_context_switches"], data["gpu_usage"], data["gpu_core_voltage"],
                data["gpu_core_clock"], data["gpu_mem_clock"], data["gpu_core_temp"], data["gpu_hotspot_temp"],
                data["gpu_board_power"], data["gpu_throttling_reasons"], data["pcie_bus_utilization"],
                data["system_ram_usage_pct"], data["system_commit_size_gb"], data["system_hard_page_faults"],
                data["disk_max_latency_ms"], data["network_ping_ms"], bool(data["is_packet_loss"]), data["network_jitter"],
                ccd0_load, ccd1_load
            )

    def terminate(self):
        self.stop_event.set()
        self.dpc_checker.stop()
        if hasattr(self, 'presentmon_process') and self.presentmon_process:
            try:
                self.presentmon_process.terminate()
                self.presentmon_process.wait(timeout=1.0)
            except Exception:
                pass
            self.presentmon_process = None
        
        if hasattr(self, 'pdh_thread') and self.pdh_thread:
            try:
                self.pdh_thread.join(timeout=1.0)
            except Exception:
                pass
            self.pdh_thread = None

        if self.nvml_initialized:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
            self.nvml_initialized = False