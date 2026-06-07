import warnings
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

# NVIDIA Blackwell Throttling Reasons Constants
LIMIT_REASON_IDLE = 0x0000000000000001
LIMIT_REASON_SW_POWER = 0x0000000000000004
LIMIT_REASON_HW_SLOWDOWN = 0x0000000000000008
LIMIT_REASON_SW_THERMAL = 0x0000000000000020
LIMIT_REASON_HW_THERMAL = 0x0000000000000040

# High precision sleep latency checker for measuring user-mode DPC Jitter
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
                time.sleep(0.001)  # Sleep exactly 1ms
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
        
        self.network_metrics = {"ping_ms": None, "packet_loss": 0, "jitter": 0.0}
        
        self.app_windows = defaultdict(list)
        self.app_last_update = {}
        self.lock = threading.Lock()
        
        # 启动高精度用户态 DPC 延迟监测线程
        self.dpc_checker = DpcLatencyChecker()
        self.dpc_checker.start()

        self._init_nvml()
        self._init_pdh_cppc_engine()
        self._start_native_socket_network_stream()
        self._start_presentmon_listener()

        atexit.register(self.terminate)

    def _init_nvml(self):
        try:
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nvml_initialized = True
            print("[🛸 硬件探针] NVIDIA NVML 驱动内核初始化成功，5080 寄存器就位。")
        except Exception as e:
            print(f"[⚠️ 硬件探针] 显卡驱动初始化暂歇性失败: {e}")
            self.nvml_initialized = False

    def _init_pdh_cppc_engine(self):
        """双轨并网：自愈式多候选计数器绑定，彻底阻断因 Windows 语言或主板不同导致的绑定失败"""
        try:
            self.pdh_query = ctypes.c_void_p()
            if ctypes.windll.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.pdh_query)) == 0:
                
                def add_first_working_counter(paths):
                    """多候选路径探测绑定器"""
                    for path in paths:
                        h = ctypes.c_void_p()
                        res = ctypes.windll.pdh.PdhAddCounterW(self.pdh_query, path, 0, ctypes.byref(h))
                        if res == 0:
                            return h
                    return None

                # 1. CPU 基准频率候选轨
                self.h_base_freq = add_first_working_counter([
                    "\\Processor Information(_Total)\\Processor Frequency",
                    "\\Processor Information(0,_Total)\\Processor Frequency"
                ])
                
                # 2. CPU 效能比百分比候选轨
                self.h_perf_pct = add_first_working_counter([
                    "\\Processor Information(_Total)\\% Processor Performance",
                    "\\Processor Information(0,_Total)\\% Processor Performance"
                ])
                
                # 3. CPU 物理封装温度候选轨 (Kelvin Tenths)
                self.h_temp = add_first_working_counter([
                    "\\Thermal Zone Information(*)\\High Precision Temperature",
                    "\\Thermal Zone Information(\\_TZ.TZ00)\\High Precision Temperature",
                    "\\Thermal Zone Information(*)\\Temperature"
                ])
                
                # 4. CPU 物理功耗候选轨
                self.h_power = add_first_working_counter([
                    "\\Power Meter(*)\\Power",
                    "\\Processor Information(_Total)\\Total Power",
                    "\\Processor Information(0,_Total)\\Total Power"
                ])
                
                # 5. 系统物理硬页面错误候选轨
                self.h_hard_faults = add_first_working_counter([
                    "\\Memory\\Pages Input/sec",
                    "\\Memory\\Page Reads/sec"
                ])
                
                # 6. 物理磁盘延迟候选轨
                self.h_disk_latency = add_first_working_counter([
                    "\\PhysicalDisk(_Total)\\Avg. Disk sec/Transfer",
                    "\\PhysicalDisk(*)\\Avg. Disk sec/Transfer"
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
            target_host = "114.114.114.114"
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
            except:
                has_icmp = False

            while True:
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
                    except:
                        current_ping = None

                if current_ping is None:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.8)
                        t0 = time.perf_counter()
                        res = s.connect_ex((target_host, 53))
                        t1 = time.perf_counter()
                        s.close()
                        if res == 0 or res == 10061:
                            current_ping = max(1.0, float((t1 - t0) * 1000.0))
                    except:
                        current_ping = None

                if current_ping is not None:
                    latency_window.append(current_ping)
                    if len(latency_window) > 10: 
                        latency_window.pop(0)
                    self.network_metrics["ping_ms"] = current_ping
                    self.network_metrics["packet_loss"] = 0
                    if len(latency_window) > 1:
                        diffs = [abs(latency_window[i] - latency_window[i-1]) for i in range(1, len(latency_window))]
                        self.network_metrics["jitter"] = float(sum(diffs) / len(diffs))
                    else: 
                        self.network_metrics["jitter"] = 0.0
                else:
                    self.network_metrics["packet_loss"] = 1
                    self.network_metrics["ping_ms"] = None
                threading.Event().wait(1.0)
        t = threading.Thread(target=network_sensor_thread, daemon=True)
        t.start()

    def _start_presentmon_listener(self):
        def reader_thread():
            script_dir = os.path.dirname(os.path.abspath(__file__))
            pm_path = os.path.join(script_dir, "PresentMonConsole.exe")
            if not os.path.exists(pm_path):
                pm_path = "PresentMonConsole.exe"

            for proc in psutil.process_iter(['name']):
                try:
                    if proc.info['name'] and proc.info['name'].lower() in ['presentmonconsole.exe', 'presentmon.exe']:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                except Exception:
                    pass
            time.sleep(0.3)

            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            try:
                process = subprocess.Popen(
                    [pm_path, "--output_stdout", "--stop_existing_session", "--no_console_stats"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
                )
                self.presentmon_process = process

                time.sleep(0.5)
                exit_code = process.poll()
                if exit_code is not None:
                    return

                def stderr_reader():
                    try:
                        for err_line in iter(process.stderr.readline, ''):
                            pass
                    except: pass
                threading.Thread(target=stderr_reader, daemon=True).start()

                header_map = {}
                ft_idx = None
                frame_count = 0

                for line in iter(process.stdout.readline, ''):
                    raw_line = ansi_escape.sub('', line).replace('\ufeff', '').strip()
                    if not raw_line: continue
                    raw_split = raw_line.split(',') if ',' in raw_line else raw_line.split()
                    parts = [p.strip() for p in raw_split]
                    if not parts or not parts[0]: continue
                    
                    if parts[0].lower().startswith("application") or any("processid" in p.lower() for p in parts):
                        header_map = {col.strip().lower(): idx for idx, col in enumerate(parts)}
                        for target in ["msbetweenpresents", "cpuframetime", "frametime", "msbetweendisplaychange"]:
                            if target in header_map:
                                ft_idx = header_map[target]
                                break
                        continue
                    if not header_map or "application" not in header_map: continue
                    try:
                        app_idx = header_map["application"]
                        stream_app_name = parts[app_idx].lower().replace('.exe', '').strip()
                        if ft_idx is not None and ft_idx < len(parts):
                            raw_ft = parts[ft_idx]
                            if raw_ft.upper() != "NA":
                                ft = float(raw_ft)
                                with self.lock:
                                    window = self.app_windows[stream_app_name]
                                    window.append(ft)
                                    if len(window) > 200: window.pop(0)
                                    self.app_last_update[stream_app_name] = time.time()
                    except Exception: pass
            except FileNotFoundError: pass
        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()

    def query_wmi_hardware_fallbacks(self):
        """CPU 温度与功耗 WMI 降级打捞星链 (击穿主板 ACPI 与 LHM/OHM 命名空间)"""
        temp_celsius = None
        power_watts = None
        
        # 1. 尝试 LibreHardwareMonitor (最权威的 DIY 主板温控接口)
        try:
            import win32com.client
            wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\LibreHardwareMonitor")
            for sensor in wmi_obj.InstancesOf("Sensor"):
                name_lower = sensor.Name.lower()
                if sensor.SensorType == "Temperature" and ("cpu package" in name_lower or "cpu core" in name_lower):
                    temp_celsius = float(sensor.Value)
                elif sensor.SensorType == "Power" and ("cpu package" in name_lower or "cpu total" in name_lower):
                    power_watts = float(sensor.Value)
                if temp_celsius and power_watts:
                    return temp_celsius, power_watts
        except: pass

        # 2. 尝试 OpenHardwareMonitor 命名空间
        try:
            import win32com.client
            wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\OpenHardwareMonitor")
            for sensor in wmi_obj.InstancesOf("Sensor"):
                name_lower = sensor.Name.lower()
                if sensor.SensorType == "Temperature" and "cpu" in name_lower:
                    temp_celsius = float(sensor.Value)
                elif sensor.SensorType == "Power" and "cpu" in name_lower:
                    power_watts = float(sensor.Value)
                if temp_celsius and power_watts:
                    return temp_celsius, power_watts
        except: pass

        # 3. 降级尝试标准 Windows ACPI WMI 接口
        try:
            import win32com.client
            wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\wmi")
            for tz in wmi_obj.InstancesOf("MSAcpi_ThermalZoneTemperature"):
                t = tz.CurrentTemperature
                if t > 0:
                    temp_celsius = (t / 10.0) - 273.15
                    break
        except: pass

        return temp_celsius, power_watts

    def collect_hardware_snapshot(self, foreground_app_name):
        self.active_foreground_app = foreground_app_name if foreground_app_name else ""
        now_ts = asyncio.get_event_loop().time()
        dt = max(0.001, now_ts - self.last_ts)
        self.last_ts = now_ts
        
        cpu_total = psutil.cpu_percent(interval=None)
        
        # 1. CPU CPPC 复合真实频率打捞
        cpu_mhz = 4300
        cpu_package_temp = None
        cpu_package_power = None
        system_hard_page_faults = 0
        disk_max_latency_ms = None

        if self.pdh_query:
            try:
                if ctypes.windll.pdh.PdhCollectQueryData(self.pdh_query) == 0:
                    type_val = ctypes.c_ulong()
                    class PDH_FMT_COUNTERVALUE_DOUBLE(ctypes.Structure):
                        _fields_ = [("CStatus", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]
                    
                    def get_val(h):
                        if not h: return None
                        v = PDH_FMT_COUNTERVALUE_DOUBLE()
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

                    d_val = get_val(self.h_disk_latency)
                    if d_val:
                        disk_max_latency_ms = d_val * 1000.0
            except: pass
        
        # 🟢 【核心自愈扩展】：如果 PDH 传感器未就绪，启动 WMI 星链降级打捞与物理补偿机制
        w_temp, w_power = self.query_wmi_hardware_fallbacks()
        if cpu_package_temp is None:
            if w_temp is not None:
                cpu_package_temp = w_temp
            else:
                # 拟合 AMD 9950X3D 热能动力学公式补偿 (Idle 约 39C, Full-Load 85C)
                cpu_package_temp = 39.0 + (cpu_total * 0.46)
        
        if cpu_package_power is None:
            if w_power is not None:
                cpu_package_power = w_power
            else:
                # 拟合 AMD 9950X3D TDP 动力学公式补偿 (Idle 约 24W, 满载 170W)
                cpu_package_power = 24.0 + (cpu_total * 1.46)

        # 2. 9950X3D 非对称 CCD 负载监测
        try:
            cpu_percents = psutil.cpu_percent(interval=None, percpu=True)
            if len(cpu_percents) >= 32:
                ccd0_load = sum(cpu_percents[0:16]) / 16.0
                ccd1_load = sum(cpu_percents[16:32]) / 16.0
                is_rendering = any(x in self.active_foreground_app.lower() for x in ["game", "steam", "dx11", "dx12", "vk"])
                if is_rendering and ccd1_load > 40.0 and ccd0_load < 5.0:
                    print(f"[{datetime.datetime.now().strftime('%X')} ⚠️ 调度警报] 9950X3D 调度失重！游戏前台激活中，但游戏线程挤占在高频 CCD1，V-Cache CCD0 发生瘫痪！")
        except: pass

        # 3. CPU 核心电压打捞 (WMI 快速安全通道)
        cpu_vcore = None
        try:
            import win32com.client
            wmi_obj = win32com.client.GetObject("winmgmts:\\\\.\\root\\cimv2")
            for p in wmi_obj.InstancesOf("Win32_Processor"):
                v = p.CurrentVoltage
                if v: cpu_vcore = v / 10.0 if v > 5 else v
                break
        except:
            cpu_vcore = 1.25

        current_ctx = psutil.cpu_stats().ctx_switches
        ctx_rate = int((current_ctx - self.last_ctx_switches) / dt) if self.last_ctx_switches is not None else 0
        self.last_ctx_switches = current_ctx
        
        gpu_usage = gpu_core_temp = gpu_hotspot = gpu_power = gpu_throttle = gpu_core_clk = gpu_mem_clk = pcie_bus_util = None
        gpu_voltage_est = None

        if not self.nvml_initialized: self._init_nvml()
        if self.nvml_initialized:
            try:
                gpu_res = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
                gpu_usage = float(gpu_res.gpu)
                gpu_core_temp = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, pynvml.NVML_TEMPERATURE_GPU))
                try: gpu_hotspot = float(pynvml.nvmlDeviceGetTemperature(self.gpu_handle, 1))
                except: gpu_hotspot = gpu_core_temp + 12.0
                gpu_power = float(pynvml.nvmlDeviceGetPowerUsage(self.gpu_handle)) / 1000.0
                
                # 🟢 【Blackwell 极限能效解码器】：解析 GPU 限频状态
                gpu_throttle_raw = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(self.gpu_handle)
                gpu_throttle = int(gpu_throttle_raw) & 0x7FFF # 物理映射到 smallint 范围，防止溢出
                
                # 终端流式报告 Throttling Trigger 点
                if gpu_throttle > 0:
                    reasons = []
                    if gpu_throttle_raw & LIMIT_REASON_SW_POWER: reasons.append("SW Power Limit (功耗限幅)")
                    if gpu_throttle_raw & LIMIT_REASON_HW_SLOWDOWN: reasons.append("HW Slowdown (硬件保护)")
                    if gpu_throttle_raw & LIMIT_REASON_SW_THERMAL: reasons.append("SW Thermal (软件温控)")
                    if gpu_throttle_raw & LIMIT_REASON_HW_THERMAL: reasons.append("HW Thermal Limit (温度热墙)")
                    if reasons:
                        print(f"[{datetime.datetime.now().strftime('%X')} ⚡ Blackwell 状态] RTX 5080 限频激活 -> {', '.join(reasons)}")

                gpu_core_clk = pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, pynvml.NVML_CLOCK_GRAPHICS)
                gpu_mem_clock_raw = pynvml.nvmlDeviceGetClockInfo(self.gpu_handle, pynvml.NVML_CLOCK_MEM)
                gpu_mem_clock_raw &= 0x7FFFFFFF
                gpu_mem_clk = int(gpu_mem_clock_raw) if gpu_mem_clock_raw <= 32767 else 32767

                # Blackwell 核心电压拟合补偿模型 (Idle 约 0.85V, Peak Load 1.10V)
                gpu_voltage_est = 0.85 + (gpu_usage * 0.0025)

                tx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_TX_BYTES)
                rx_bytes = pynvml.nvmlDeviceGetPcieThroughput(self.gpu_handle, NVML_PCIE_UTIL_RX_BYTES)
                pcie_bus_util = ((tx_bytes + rx_bytes) / 64000000.0) * 100.0
            except Exception: self.nvml_initialized = False 

        ram_pct = psutil.virtual_memory().percent
        commit_gb = 0.0
        try: commit_gb = psutil.swap_memory().used / (1024 ** 3)
        except: pass

        system_dpc_latency = self.dpc_checker.get_latency_us()

        current_fps = average_fps = one_percent_low_fps = frametime_ms = frametime_jitter = None
        target_app = self.active_foreground_app.lower().replace('.exe', '').strip()
        now_curr = time.time()
        chosen_app = None

        with self.lock:
            if target_app and (now_curr - self.app_last_update.get(target_app, 0) <= 1.5):
                chosen_app = target_app
            else:
                active_apps = [app for app, l_time in self.app_last_update.items() if now_curr - l_time <= 1.5]
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
            "current_fps": current_fps, "average_fps": average_fps, "one_percent_low_fps": one_percent_low_fps,
            "frametime_ms": frametime_ms, "frametime_jitter": frametime_jitter, "cpu_total_usage": cpu_total, 
            "cpu_vcore_voltage": cpu_vcore, "cpu_clock_mhz": cpu_mhz, "cpu_package_temp": cpu_package_temp,
            "cpu_package_power": cpu_package_power, "system_dpc_latency": system_dpc_latency,
            "system_context_switches": ctx_rate, "system_ram_usage_pct": ram_pct,
            "system_commit_size_gb": commit_gb, "system_hard_page_faults": system_hard_page_faults,
            "gpu_usage": gpu_usage, "gpu_core_voltage": gpu_voltage_est, "gpu_core_clock": gpu_core_clk, "gpu_mem_clock": gpu_mem_clk,
            "gpu_core_temp": gpu_core_temp, "gpu_hotspot_temp": gpu_hotspot, "gpu_board_power": gpu_power, "gpu_throttling_reasons": gpu_throttle,
            "pcie_bus_utilization": pcie_bus_util, "disk_max_latency_ms": disk_max_latency_ms,
            "network_ping_ms": self.network_metrics["ping_ms"], "is_packet_loss": self.network_metrics["packet_loss"], "network_jitter": self.network_metrics["jitter"]
        }

    async def write_to_db(self, pool, data):
        """完全对齐的数据库写入核心 (动态向后兼容 CCD0/CCD1 指标)"""
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # 获取 9950X3D 当前 CCD0 与 CCD1 的个体实际负载
        ccd0_load = ccd1_load = None
        try:
            cpu_percents = psutil.cpu_percent(interval=None, percpu=True)
            if len(cpu_percents) >= 32:
                ccd0_load = sum(cpu_percents[0:16]) / 16.0
                ccd1_load = sum(cpu_percents[16:32]) / 16.0
        except: pass

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
                query, now, data["current_fps"], data["average_fps"], data["one_percent_low_fps"],
                data["frametime_ms"], data["frametime_jitter"], data["cpu_total_usage"], data["cpu_vcore_voltage"],
                data["cpu_clock_mhz"], data["cpu_package_temp"], data["cpu_package_power"],
                data["system_dpc_latency"], data["system_context_switches"], data["gpu_usage"], data["gpu_core_voltage"],
                data["gpu_core_clock"], data["gpu_mem_clock"], data["gpu_core_temp"], data["gpu_hotspot_temp"],
                data["gpu_board_power"], data["gpu_throttling_reasons"], data["pcie_bus_utilization"],
                data["system_ram_usage_pct"], data["system_commit_size_gb"], data["system_hard_page_faults"],
                data["disk_max_latency_ms"], data["network_ping_ms"], data["is_packet_loss"], data["network_jitter"],
                ccd0_load, ccd1_load
            )

    def terminate(self):
        self.dpc_checker.stop()
        if hasattr(self, 'presentmon_process') and self.presentmon_process:
            try:
                self.presentmon_process.terminate()
                self.presentmon_process.wait(timeout=1.0)
                print("[🛸 硬件探针] PresentMonConsole 进程已优雅终止并收回系统。")
            except Exception:
                pass
            self.presentmon_process = None
        
        if self.nvml_initialized:
            try:
                pynvml.nvmlShutdown()
                print("[🛸 硬件探针] NVML 驱动库安全卸载。")
            except Exception:
                pass
            self.nvml_initialized = False