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
        
        self.wmi_lock = threading.Lock()
        self.cached_wmi_temp = None        # CPU 封装温度 (LHM: Core (Tctl/Tdie))
        self.cached_wmi_power = None        # CPU 封装功率 (LHM: Powers/Package)
        self.cached_cpu_vcore = None        # CPU Vcore (LHM: 主板 Super I/O 真实读数)
        self.cached_gpu_voltage = None      # RTX5080 核心电压 (LHM/NVAPI; NVML 在 GeForce 上无法提供)
        self.cached_gpu_hotspot = None      # RTX5080 显存结点/热点温度 (LHM)
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

        # 启动后台自动下载线程，拉取最新版外部硬件监视器
        self.lhm_download_thread = threading.Thread(target=self._auto_prepare_lhm_async, daemon=True)
        self.lhm_download_thread.start()

        self.wmi_thread = threading.Thread(target=self._background_lhm_loop, daemon=True)
        self.wmi_thread.start()

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
                    # 🟢 核心修复：强行注入隐藏窗体配置，彻底切断 PresentMon 与 DWM 缓冲区的视口纠缠
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0  # SW_HIDE
                    
                    process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                        startupinfo=startupinfo,            # 注入底层配置(SW_HIDE)
                        creationflags=subprocess.CREATE_NO_WINDOW  # 彻底杜绝控制台窗体一闪而过抢焦点
                    )
                    self.presentmon_process = process
                except Exception as e:
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
                        if process.poll() is not None:
                            # 🟢 核心修复：一旦 PresentMon 意外崩塌，强制冷冻 3 秒再重启，拒绝高频连击显卡驱动
                            time.sleep(3.0)
                            break  
                        continue  
                    except Exception:
                        break

                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass

        t = threading.Thread(target=monitor_loop, daemon=True)
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
        # 从 LibreHardwareMonitor.config 读取 Web 服务端口(listenerPort)，缺省 8085。
        try:
            cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LibreHardwareMonitor.config")
            if os.path.exists(cfg):
                with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                    m = re.search(r'key="listenerPort"\s+value="(\d+)"', f.read())
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
        return 8085

    @staticmethod
    def _lhm_num(s):
        # 解析 LHM 值串(如 "0.965 V" / "56.0 °C" / "100.5 W")的前导数值，兼容逗号小数。
        try:
            return float(str(s).strip().split(" ")[0].replace(",", "."))
        except Exception:
            return None

    def _background_lhm_loop(self):
        """通过 LibreHardwareMonitor 内置 Web 服务(/data.json)采集 NVML/PDH 无法可靠提供的真实硬件量：
        CPU Vcore、CPU 封装温度(Tctl/Tdie)与功率、RTX5080 核心电压与显存结点(热点)温度。
        相比旧的 WMI(root\\LibreHardwareMonitor) 通道：该命名空间在本机并未发布、旧通道长期失败并退化到
        伪造/静态值；HTTP JSON 通道无需 WMI 注册、稳定且为 LHM 官方推荐。同时内置看门狗保活 LHM 进程。"""
        import urllib.request
        import json as _json

        url = "http://127.0.0.1:%d/data.json" % self._read_lhm_port()
        lhm_process = None
        lhm_stuck_fails = 0   # 连续 JSON 拉取失败计数：进程在但 Web 服务卡死时据此强制重启自愈
        lhm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LibreHardwareMonitor.exe")

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
                # 【看门狗】物理文件存在但进程缺失时，3s 内静默(隐藏窗口)重新拉起。
                if os.path.exists(lhm_path):
                    is_alive = bool(lhm_process and lhm_process.poll() is None)
                    if not is_alive:
                        lhm_running = False
                        for proc in psutil.process_iter(['name']):
                            try:
                                if proc.info['name'] and proc.info['name'].lower() == "librehardwaremonitor.exe":
                                    lhm_running = True
                                    break
                            except Exception:
                                pass
                        if not lhm_running:
                            try:
                                startupinfo = subprocess.STARTUPINFO()
                                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                                startupinfo.wShowWindow = 0
                                lhm_process = subprocess.Popen(
                                    [lhm_path], startupinfo=startupinfo,
                                    creationflags=subprocess.CREATE_NO_WINDOW
                                )
                                print("[🛸 硬件探针] 伴随驱动进程缺失，自动看门狗已隐藏复活 LibreHardwareMonitor。")
                            except Exception as e:
                                print(f"[⚠️ 硬件探针] 自动看门狗拉起 LibreHardwareMonitor 失败: {e}")

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

                # 【自愈】LHM 进程在、但 Web 服务连续 ~15s 无响应(卡死/端口冲突)时，强制结束 LHM，
                # 令上方看门狗拉起健康新实例 —— 贯彻"始终在采集、绝不默默降级为 NULL"。
                if json_ok:
                    lhm_stuck_fails = 0
                else:
                    lhm_stuck_fails += 1
                    if lhm_stuck_fails >= 15:
                        killed = False
                        for proc in psutil.process_iter(['name']):
                            try:
                                if proc.info['name'] and proc.info['name'].lower() == "librehardwaremonitor.exe":
                                    proc.kill()
                                    killed = True
                            except Exception:
                                pass
                        if lhm_process:
                            try:
                                lhm_process.kill()
                            except Exception:
                                pass
                            lhm_process = None
                        if killed:
                            print("[⚠️ 硬件探针] LHM Web 服务连续无响应，已强制重启以恢复真实采集(非降级)。")
                        lhm_stuck_fails = 0

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
            if lhm_process:
                try:
                    lhm_process.terminate()
                    lhm_process.wait(timeout=1.0)
                except Exception:
                    pass

    def _read_gpu_throttle_reasons(self):
        """隔离的降频原因读取。GeForce 同样支持该接口(本机 RTX5080 实测返回 0x0，AI 所谓"仅
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

        current_ctx = psutil.cpu_stats().ctx_switches
        ctx_rate = int((current_ctx - self.last_ctx_switches) / dt) if self.last_ctx_switches is not None else 0
        self.last_ctx_switches = current_ctx
        
        ram_pct = psutil.virtual_memory().percent
        commit_gb = self._get_commit_charge_gb()

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
            "gpu_core_voltage": gpu_core_voltage,   # LHM(NVAPI) RTX5080 真实核心电压；不可用时写 NULL
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