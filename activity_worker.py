# -*- coding: utf-8 -*-
import datetime
import asyncio
import sys
import ctypes
import psutil
import pynvml
import re

# 🟢 导入生命周期舱的高精指纹识别器
from lifecycle_worker import check_process_elevation, check_file_signature

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
    except: pass

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
        self.path_elevation_cache = {} # 🟢 新增：路径-提权本地高速缓存 (针对单人单机环境优化)
        self.nvml_initialized = False
        self.gpu_handle = None
        
        self._init_nvml()
        try_enable_debug_privilege()

    def _init_nvml(self):
        try:
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.nvml_initialized = True
        except:
            self.nvml_initialized = False

    async def get_or_register_cached(self, conn, proc_info):
        """向维度表安全登记进程指纹，动态识别特权与签名特征以消除重复"""
        is_elevated = proc_info.get("is_elevated")
        if is_elevated is None or is_elevated < 0:
            is_elevated = check_process_elevation(proc_info["os_pid"])
            if is_elevated < 0:
                # 🟢 [自愈级降级 1]：若进程已死，首选从本地内存路径缓存中打捞历史提权状态
                is_elevated = self.path_elevation_cache.get(proc_info["exe"])
                if is_elevated is None:
                    # 🟢 [自愈级降级 2]：若内存无记录，穿透至 dim_process_registry 历史归档中提取最近一次的特权状态
                    is_elevated = await conn.fetchval(
                        "SELECT is_elevated FROM public.dim_process_registry WHERE executable_path = $1 ORDER BY created_at DESC LIMIT 1",
                        proc_info["exe"]
                    )
                    if is_elevated is None:
                        # 🟢 [自愈级降级 3]：终极降级安全兜底，单人工作站默认为 0 (普通权限)
                        is_elevated = 0

        signature_status = proc_info.get("signature_status")
        if signature_status is None:
            signature_status = check_file_signature(proc_info["exe"])
        
        # 🟢 固化特权级缓存
        if is_elevated >= 0:
            self.path_elevation_cache[proc_info["exe"]] = is_elevated
        
        cache_tuple = (
            proc_info["name"], 
            proc_info["exe"], 
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
            proc_info["exe"], 
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

        vram_map = {}
        gpu_util_map = {}
        if self.nvml_initialized:
            try:
                for nv_proc in pynvml.nvmlDeviceGetGraphicsRunningProcesses(self.gpu_handle):
                    vram_map[nv_proc.pid] = nv_proc.usedGpuMemory / (1024 ** 3)
                util_samples = pynvml.nvmlDeviceGetProcessUtilization(self.gpu_handle, 0)
                for sample in util_samples:
                    gpu_util_map[sample.pid] = float(sample.gpuUtil)
            except: pass

        global_conns = {}
        try:
            from collections import defaultdict
            grouped = defaultdict(list)
            for conn in psutil.net_connections(kind='inet'):
                if conn.pid: grouped[conn.pid].append(conn)
            global_conns = grouped
        except Exception: pass

        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'io_counters', 'create_time']):
            try:
                info = proc.info
                create_time = info['create_time']
                if create_time is None: continue
                
                cache_key = (info['pid'], create_time)
                io = info['io_counters']
                
                r_bytes = io.read_bytes if io else 0
                w_bytes = io.write_bytes if io else 0
                r_ops = io.read_count if io else 0
                w_ops = io.write_count if io else 0
                other_bytes = io.other_bytes if io else 0
                
                r_rate_mb = w_rate_mb = iops_rate = other_rate_kb = 0.0
                if cache_key in self.io_delta_cache:
                    last_r, last_w, last_ro, last_wo, last_other, last_t = self.io_delta_cache[cache_key]
                    dt = max(0.001, now_ts - last_t)
                    r_rate_mb = max(0.0, (r_bytes - last_r) / (1024 * 1024) / dt)
                    w_rate_mb = max(0.0, (w_bytes - last_w) / (1024 * 1024) / dt)
                    iops_rate = max(0.0, ((r_ops + w_ops) - (last_ro + last_wo)) / dt)
                    other_rate_kb = max(0.0, (other_bytes - last_other) / 1024.0 / dt)
                
                next_io_cache[cache_key] = (r_bytes, w_bytes, r_ops, w_ops, other_bytes, now_ts)
                cpu = info['cpu_percent'] or 0.0

                if cpu <= 0.1 and r_rate_mb <= 0.01 and w_rate_mb <= 0.01 and other_rate_kb <= 0.5: 
                    continue

                try:
                    exe_path = proc.exe()
                    if not exe_path: continue
                    cmdline_str = " ".join(proc.cmdline()) if proc.cmdline() else ""
                    cmdline_str = sanitize_command_line(info['name'], cmdline_str)
                    
                    p_mem = proc.memory_info()
                    ram_mb = int(p_mem.rss / (1024 * 1024)) if p_mem else 0
                    p_threads = proc.num_threads()
                    threads = int(p_threads) if p_threads is not None else 1
                    
                    net_conn_count = 0
                    net_remote = None
                    cmdline_lower = cmdline_str.lower()
                    is_sandbox = any(x in cmdline_lower for x in ["--type=renderer", "--type=utility", "--type=gpu-process"])
                    
                    if not is_sandbox:
                        try:
                            conns = global_conns.get(info['pid'], [])
                            net_conn_count = len(conns)
                            remote_targets = [f"{c.raddr.ip}:{c.raddr.port}" for c in conns if c.raddr]
                            if remote_targets: net_remote = ",".join(remote_targets)
                        except: pass
                    
                    parent_name = None
                    try:
                        parent_proc = proc.parent()
                        if parent_proc: parent_name = parent_proc.name()
                    except: pass
                    
                except (psutil.AccessDenied, psutil.NoSuchProcess): continue 

                service_name = ""
                if info['name'].lower() == 'svchost.exe':
                    try:
                        services = proc.services()
                        if services: service_name = ",".join([s.name for s in services])[:100]
                    except:
                        cmd_parts = proc.cmdline()
                        if "-k" in cmd_parts:
                            idx = cmd_parts.index("-k")
                            if idx + 1 < len(cmd_parts): service_name = f"Group:{cmd_parts[idx+1]}"[:100]

                affinity_mask = 0
                try:
                    for cpu_id in proc.cpu_affinity():
                        if cpu_id < 32: affinity_mask |= (1 << cpu_id)
                    if affinity_mask & 0x80000000: affinity_mask -= 0x100000000
                except: 
                    affinity_mask = -2 

                p_vram = float(vram_map.get(info['pid'], 0.0))
                p_gpu_util = float(gpu_util_map.get(info['pid'], 0.0))
                shared_vram_mb = int(ram_mb * 0.12) if p_vram > 0 else 0

                is_dead = 0
                try: is_dead = 1 if proc.status() == psutil.STATUS_STOPPED else 0
                except: pass

                proc_net_send_kb = float(other_rate_kb * 0.4)
                proc_net_recv_kb = float(other_rate_kb * 0.6)

                # 🟢 【核心修复】：在进程存活的瞬间直接解析特权与签名状态
                is_elevated = check_process_elevation(info['pid'])
                signature_status = check_file_signature(exe_path)

                # 🟢 固化特权级缓存
                if is_elevated >= 0:
                    self.path_elevation_cache[exe_path] = is_elevated

                active_list.append({
                    "os_pid": info['pid'], "name": info['name'], "exe": exe_path, "cmdline": cmdline_str,
                    "parent_name": parent_name, "service_name": service_name if service_name else None, 
                    "cpu": float(cpu), "gpu": p_gpu_util,
                    "ram_mb": ram_mb, "vram_gb": p_vram, "vram_shared_mb": shared_vram_mb,
                    "r_rate": r_rate_mb, "w_rate": w_rate_mb, "iops": int(iops_rate), 
                    "net_send_kb": proc_net_send_kb, "net_recv_kb": proc_net_recv_kb,
                    "affinity": affinity_mask, "threads": threads, "is_not_responding": is_dead,
                    "net_conn_count": int(net_conn_count), "net_remote": net_remote,
                    "is_elevated": is_elevated,          
                    "signature_status": signature_status  
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied): continue
                
        self.io_delta_cache = next_io_cache 
        return active_list

    async def write_to_db_by_worker(self, pool, active_processes):
        await self.write_batch_to_db(pool, active_processes)

    async def write_batch_to_db(self, pool, active_processes):
        if not active_processes: return
        now = datetime.datetime.now(datetime.timezone.utc)
        
        uncached_procs = []
        for proc in active_processes:
            is_elevated = proc.get("is_elevated")
            if is_elevated is None or is_elevated < 0:
                is_elevated = check_process_elevation(proc["os_pid"])
                if is_elevated < 0:
                    is_elevated = self.path_elevation_cache.get(proc["exe"], 0)

            signature_status = proc.get("signature_status")
            if signature_status is None:
                signature_status = check_file_signature(proc["exe"])

            # 🟢 固化缓存
            if is_elevated >= 0:
                self.path_elevation_cache[proc["exe"]] = is_elevated

            cache_tuple = (
                proc["name"], 
                proc["exe"], 
                proc["parent_name"], 
                proc["cmdline"], 
                proc["service_name"],
                is_elevated,
                signature_status
            )
            if cache_tuple not in self.key_cache:
                proc["is_elevated"] = is_elevated
                proc["signature_status"] = signature_status
                uncached_procs.append(proc)
        
        if uncached_procs:
            async def resolve_one_process(proc_info):
                async with pool.acquire() as conn:
                    await self.get_or_register_cached(conn, proc_info)
            await asyncio.gather(*(resolve_one_process(p) for p in uncached_procs), return_exceptions=True)

        query = """
            INSERT INTO public.fact_process_activity 
            ("timestamp", process_key, os_pid, proc_cpu_usage, proc_gpu_usage, proc_ram_mb, 
             proc_vram_used_gb, proc_vram_shared_mb, proc_disk_read_rate_mb, proc_disk_write_rate_mb, proc_disk_iops, 
             proc_network_send_kb, proc_network_recv_kb, proc_active_connections, proc_remote_ip_port, 
             proc_cpu_affinity, proc_thread_count, is_not_responding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18);
        """
        async with pool.acquire() as conn:
            async with conn.transaction():
                batch_args = []
                for proc in active_processes:
                    is_elevated = proc.get("is_elevated")
                    if is_elevated is None or is_elevated < 0:
                        is_elevated = check_process_elevation(proc["os_pid"])
                        if is_elevated < 0:
                            is_elevated = self.path_elevation_cache.get(proc["exe"], 0)
                        
                    signature_status = proc.get("signature_status")
                    if signature_status is None:
                        signature_status = check_file_signature(proc["exe"])
                        
                    cache_tuple = (
                        proc["name"], 
                        proc["exe"], 
                        proc["parent_name"], 
                        proc["cmdline"], 
                        proc["service_name"],
                        is_elevated,
                        signature_status
                    )
                    p_key = self.key_cache.get(cache_tuple)
                    if not p_key: 
                        p_key = await self.get_or_register_cached(conn, proc)
                    if not p_key: 
                        continue
                    
                    batch_args.append((
                        now, p_key, proc["os_pid"], proc["cpu"], proc["gpu"], proc["ram_mb"],
                        proc["vram_gb"], proc["vram_shared_mb"], proc["r_rate"], proc["w_rate"], proc["iops"],
                        proc["net_send_kb"], proc["net_recv_kb"],
                        proc["net_conn_count"], proc["net_remote"], proc["affinity"], proc["threads"], proc["is_not_responding"]
                    ))
                if batch_args: 
                    await conn.executemany(query, batch_args)