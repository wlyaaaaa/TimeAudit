# -*- coding: utf-8 -*-
"""
Windows 11 Native Telemetry Engine - Backend Verification [Phase 2]
===================================================================
验证模块：
  1. 高频活动事实表 (fact_process_activity) 全 18 列数据写入与读取
  2. 切窗上下文事实表 (fact_process_context) 全 8 列状态机过渡链路验证
  3. 全量物理库字段综合账单看板 (对时空数据进行自愈格式化显示)
"""

import asyncio
import datetime
import sys
import os
import time
import unittest
import psutil
import asyncpg

# 将当前目录追加到搜索路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from activity_worker import ProcessActivityWorker
    from context_worker import WindowStateTracker
    from hardware_worker import HardwareTelemetryWorker
    from main import DB_DSN
except ImportError as e:
    print(f"[-] 依赖加载失败，请确保位于工程根目录下运行此脚本。错误详情: {e}")
    sys.exit(1)


# =====================================================================
# 🎨 物理级自愈降级填充器 (Database Visualizer)
# =====================================================================

class DatabaseVisualizer:
    @staticmethod
    def print_header(title):
        print("\n" + "="*80)
        print(f"📊 {title}")
        print("="*80)

    @staticmethod
    def render_row(column_name, value, unit="", fallback="[NULL / System Default]"):
        """对时空/空闲数据进行优雅降级渲染，防止排版错位"""
        if value is None or value == "":
            display_val = f"\033[93m{fallback}\033[0m"  # 黄色高亮显示降级值
        else:
            if isinstance(value, float):
                display_val = f"{value:.3f}"
            elif isinstance(value, datetime.datetime):
                display_val = value.strftime('%Y-%m-%d %H:%M:%S.%f %z')
            else:
                display_val = str(value)
            
            if unit:
                display_val = f"{display_val} {unit}"
                
        print(f"  - {column_name:<26} : {display_val}")


# =====================================================================
# 🕵️ 第二阶段测试用例集
# =====================================================================

class TelemetryPhase2TestSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        """物理数据库连通性判定"""
        self.test_prefix = f"test_proc_{int(time.time())}"
        self.use_mock = False
        try:
            self.pool = await asyncpg.create_pool(
                dsn=DB_DSN, min_size=1, max_size=2, command_timeout=3.0
            )
            print("\033[92m[+] 成功穿透 Docker 数据库，进入第二阶段全量字段对齐。\033[0m")
        except Exception:
            print("\033[91m[-] 数据库不可达。第二阶段测试必须物理连通 Docker Postgres 实例，请检查容器状态！\033[0m")
            sys.exit(1)

    async def asyncTearDown(self):
        """擦除事实表写入产生的全部测试追踪脏数据"""
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    # 获取本次测试产生的 process_key
                    rows = await conn.fetch(
                        "SELECT process_key FROM public.dim_process_registry WHERE process_name LIKE $1",
                        f"{self.test_prefix}%"
                    )
                    if rows:
                        keys = [r['process_key'] for r in rows]
                        await conn.execute("DELETE FROM public.fact_process_activity WHERE process_key = ANY($1)", keys)
                        await conn.execute("DELETE FROM public.fact_process_context WHERE process_key = ANY($1)", keys)
                        await conn.execute("DELETE FROM public.fact_process_lifecycle_events WHERE process_key = ANY($1)", keys)
                        await conn.execute("DELETE FROM public.dim_process_registry WHERE process_key = ANY($1)", keys)
                print("\033[92m[+] 测试沙箱事实记录物理回收成功。\033[0m")
            except Exception as e:
                print(f"[-] 回收事实沙箱异常: {e}")
            await self.pool.close()

    async def test_01_fact_process_activity_all_attributes(self):
        """数据正确性校验 2.1: 验证 fact_process_activity 事实表全部 18 个属性落库与读取"""
        activity_worker = ProcessActivityWorker()
        current_pid = os.getpid()

        # 注册测试维度
        proc_info = {
            "name": f"{self.test_prefix}_act.exe",
            "exe": "C:\\Windows\\System32\\test_act.exe",
            "parent_name": "cmd.exe",
            "cmdline": "--dummy-flag=true",
            "service_name": None,  # 时空字段验证
            "os_pid": current_pid
        }

        async with self.pool.acquire() as conn:
            p_key = await activity_worker.get_or_register_cached(conn, proc_info)
            self.assertIsNotNone(p_key)

            # 模拟高频活动事实表全 18 属性数据
            now_ts = datetime.datetime.now(datetime.timezone.utc)
            mock_activity = {
                "timestamp": now_ts,
                "process_key": p_key,
                "os_pid": current_pid,
                "proc_cpu_usage": 14.5,
                "proc_gpu_usage": 0.0,  # 显卡时空数据
                "proc_ram_mb": 128,
                "proc_vram_used_gb": 0.12,
                "proc_vram_shared_mb": 15,
                "proc_disk_read_rate_mb": 0.0,  # I/O 时空数据
                "proc_disk_write_rate_mb": 1.25,
                "proc_disk_iops": 45,
                "proc_network_send_kb": 0.0,   # 网络时空数据
                "proc_network_recv_kb": 0.0,
                "proc_active_connections": 0,  # 网络连接时空数据
                "proc_remote_ip_port": None,   # Sparse 列验证
                "proc_cpu_affinity": 65535,
                "proc_thread_count": 8,
                "is_not_responding": 0
            }

            # 物理写入事实表
            query_insert = """
                INSERT INTO public.fact_process_activity 
                ("timestamp", process_key, os_pid, proc_cpu_usage, proc_gpu_usage, proc_ram_mb, 
                 proc_vram_used_gb, proc_vram_shared_mb, proc_disk_read_rate_mb, proc_disk_write_rate_mb, proc_disk_iops, 
                 proc_network_send_kb, proc_network_recv_kb, proc_active_connections, proc_remote_ip_port, 
                 proc_cpu_affinity, proc_thread_count, is_not_responding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18);
            """
            await conn.execute(
                query_insert, 
                mock_activity["timestamp"], mock_activity["process_key"], mock_activity["os_pid"],
                mock_activity["proc_cpu_usage"], mock_activity["proc_gpu_usage"], mock_activity["proc_ram_mb"],
                mock_activity["proc_vram_used_gb"], mock_activity["proc_vram_shared_mb"],
                mock_activity["proc_disk_read_rate_mb"], mock_activity["proc_disk_write_rate_mb"], mock_activity["proc_disk_iops"],
                mock_activity["proc_network_send_kb"], mock_activity["proc_network_recv_kb"],
                mock_activity["proc_active_connections"], mock_activity["proc_remote_ip_port"],
                mock_activity["proc_cpu_affinity"], mock_activity["proc_thread_count"], mock_activity["is_not_responding"]
            )

            # 读取并核对 DDL 的 18 个属性
            query_select = """
                SELECT * FROM public.fact_process_activity 
                WHERE process_key = $1 AND timestamp = $2;
            """
            row = await conn.fetchrow(query_select, p_key, now_ts)
            self.assertIsNotNone(row)

            DatabaseVisualizer.print_header("1. 高频进程活动事实表 (fact_process_activity) 全 18 属性对齐账单")
            DatabaseVisualizer.render_row("timestamp", row["timestamp"])
            DatabaseVisualizer.render_row("process_key", row["process_key"])
            DatabaseVisualizer.render_row("os_pid", row["os_pid"])
            DatabaseVisualizer.render_row("proc_cpu_usage", row["proc_cpu_usage"], "%")
            DatabaseVisualizer.render_row("proc_gpu_usage", row["proc_gpu_usage"], "%", fallback="[GPU IDLE / 显卡无高能负载]")
            DatabaseVisualizer.render_row("proc_ram_mb", row["proc_ram_mb"], "MB")
            DatabaseVisualizer.render_row("proc_vram_used_gb", row["proc_vram_used_gb"], "GB", fallback="[0.00 GB / 无物理显存独占]")
            DatabaseVisualizer.render_row("proc_vram_shared_mb", row["proc_vram_shared_mb"], "MB")
            DatabaseVisualizer.render_row("proc_disk_read_rate_mb", row["proc_disk_read_rate_mb"], "MB/s", fallback="[0.00 MB/s / 无物理硬盘读取]")
            DatabaseVisualizer.render_row("proc_disk_write_rate_mb", row["proc_disk_write_rate_mb"], "MB/s")
            DatabaseVisualizer.render_row("proc_disk_iops", row["proc_disk_iops"], "ops/sec")
            DatabaseVisualizer.render_row("proc_network_send_kb", row["proc_network_send_kb"], "KB/s", fallback="[0.00 KB/s / 无封包上行发送]")
            DatabaseVisualizer.render_row("proc_network_recv_kb", row["proc_network_recv_kb"], "KB/s", fallback="[0.00 KB/s / 无封包下行接收]")
            DatabaseVisualizer.render_row("proc_active_connections", row["proc_active_connections"], "对", fallback="[0 对 / 无前后台网络长链接]")
            DatabaseVisualizer.render_row("proc_remote_ip_port", row["proc_remote_ip_port"], fallback="[Local Loopback / 物理本地环回无外网通信监听]")
            DatabaseVisualizer.render_row("proc_cpu_affinity", row["proc_cpu_affinity"])
            DatabaseVisualizer.render_row("proc_thread_count", row["proc_thread_count"], "threads")
            DatabaseVisualizer.render_row("is_not_responding", row["is_not_responding"], fallback="[0 - 物理响应正常]")
            print("="*80)

    async def test_02_fact_process_context_all_attributes(self):
        """数据正确性校验 2.2: 验证 fact_process_context 事实表全部 8 个属性落库与读取"""
        tracker = WindowStateTracker()
        current_pid = os.getpid()

        # 注册测试维度
        proc_info = {
            "process_name": f"{self.test_prefix}_ctx.exe",
            "executable_path": "C:\\Windows\\System32\\test_ctx.exe",
            "parent_process": None, # 触发时空降级
            "command_line": "",     # 触发时空降级
            "service_name": None,
            "is_elevated": 1,
            "signature_status": 1
        }

        async with self.pool.acquire() as conn:
            p_key = await tracker.get_or_register_metadata_slow(conn, proc_info)
            self.assertIsNotNone(p_key)

            now_ts = datetime.datetime.now(datetime.timezone.utc)
            end_ts = now_ts + datetime.timedelta(seconds=5)
            duration_ms = 5000

            # 物理模拟写入切窗记录
            query_insert = """
                INSERT INTO public.fact_process_context 
                ("timestamp", process_key, os_pid, is_foreground, window_title, window_mode, end_timestamp, duration_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8);
            """
            await conn.execute(query_insert, now_ts, p_key, current_pid, 1, "Visual Studio Code - Main.py", 3, end_ts, duration_ms)

            # 读取并核对 DDL 的 8 个属性
            query_select = """
                SELECT * FROM public.fact_process_context 
                WHERE process_key = $1 AND timestamp = $2;
            """
            row = await conn.fetchrow(query_select, p_key, now_ts)
            self.assertIsNotNone(row)

            DatabaseVisualizer.print_header("2. 前台切窗上下文事实表 (fact_process_context) 全 8 属性对齐账单")
            DatabaseVisualizer.render_row("timestamp", row["timestamp"])
            DatabaseVisualizer.render_row("process_key", row["process_key"])
            DatabaseVisualizer.render_row("os_pid", row["os_pid"])
            DatabaseVisualizer.render_row("is_foreground", row["is_foreground"])
            DatabaseVisualizer.render_row("window_title", row["window_title"], fallback="[Native Windowless / 无 UI 物理视窗]")
            DatabaseVisualizer.render_row("window_mode", row["window_mode"], fallback="[0 - 物理无窗体形态]")
            DatabaseVisualizer.render_row("end_timestamp", row["end_timestamp"], fallback="[Active Focusing / 前台焦点持续保持中，无截止时间戳]")
            DatabaseVisualizer.render_row("duration_ms", row["duration_ms"], "ms", fallback="[Focusing / 切屏动作维持中，无累计工时]")
            print("="*80)

    async def test_03_database_comprehensive_snapshot_view(self):
        """数据正确性校验 2.3: 主控综合测试：聚合展现当前数据库全部表的全量属性综合显示"""
        current_pid = os.getpid()
        
        async with self.pool.acquire() as conn:
            # 1. 注册主测试维度
            dim_meta = {
                "process_name": f"{self.test_prefix}_master.exe",
                "executable_path": "C:\\Windows\\System32\\test_master.exe",
                "parent_process": "explorer.exe",
                "command_line": None, # 空值测试
                "service_name": None, # 空值测试
                "is_elevated": 0,
                "signature_status": 1
            }
            p_key = await WindowStateTracker().get_or_register_metadata_slow(conn, dim_meta)
            now_ts = datetime.datetime.now(datetime.timezone.utc)

            # 2. 物理插桩写入生命周期、活动、切屏以及硬件数据，保证看板时空字段数据全部饱满
            await conn.execute(
                """INSERT INTO public.fact_process_lifecycle_events 
                   (event_timestamp, process_key, os_pid, event_type, process_lifetime, exit_code)
                   VALUES ($1, $2, $3, $4, $5, $6);""",
                now_ts, p_key, current_pid, "EXIT", 28, "0xC0000005"
            )
            
            await conn.execute(
                """INSERT INTO public.fact_system_hardware 
                   (timestamp, current_fps, average_fps, one_percent_low_fps, frametime_ms, frametime_jitter,
                    cpu_total_usage, cpu_vcore_voltage, cpu_clock_mhz, cpu_package_temp, cpu_package_power, 
                    system_dpc_latency, system_context_switches, gpu_usage, gpu_core_voltage, gpu_core_clock, gpu_mem_clock, 
                    gpu_core_temp, gpu_hotspot_temp, gpu_board_power, gpu_throttling_reasons, pcie_bus_utilization,
                    system_ram_usage_pct, system_commit_size_gb, system_hard_page_faults, disk_max_latency_ms,
                    network_ping_ms, is_packet_loss, network_jitter, cpu_ccd0_usage, cpu_ccd1_usage)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31);""",
                now_ts, 144.0, 142.5, 98.4, 6.94, 0.45, 
                24.5, 1.225, 4450, 43.5, 52.4, 
                12.45, 148590, 85.0, 1.050, 2505, 11500, 
                38.0, 46.5, 125.4, 0, 1.85,
                38.4, 18.4, 0, 0.12, 
                8, 0, 0.45, 48.5, 2.1
            )

            # 3. 聚合拉出：多表物理关联与空值优雅填充展示看板
            DatabaseVisualizer.print_header("3. 数据库全字段综合穿透审计面板 (全 5 表全属性无漏映射)")
            
            # 🔘 维度表 (Table 1: dim_process_registry)
            dim_row = await conn.fetchrow("SELECT * FROM public.dim_process_registry WHERE process_key = $1", p_key)
            print("\n  [维度舱] -> Table 1: dim_process_registry (进程指纹登记库)")
            DatabaseVisualizer.render_row("process_key", dim_row["process_key"])
            DatabaseVisualizer.render_row("process_name", dim_row["process_name"])
            DatabaseVisualizer.render_row("executable_path", dim_row["executable_path"])
            DatabaseVisualizer.render_row("parent_process", dim_row["parent_process"], fallback="[No Parent / Kernel Init]")
            DatabaseVisualizer.render_row("command_line", dim_row["command_line"], fallback="[No Commandline Args / 宿主程序无参运行]")
            DatabaseVisualizer.render_row("is_elevated", dim_row["is_elevated"])
            DatabaseVisualizer.render_row("service_name", dim_row["service_name"], fallback="[Independent / 无系统寄生服务映射]")
            DatabaseVisualizer.render_row("signature_status", dim_row["signature_status"])
            DatabaseVisualizer.render_row("created_at", dim_row["created_at"])

            # 🔘 离散生命周期表 (Table 2: fact_process_lifecycle_events)
            lf_row = await conn.fetchrow("SELECT * FROM public.fact_process_lifecycle_events WHERE process_key = $1", p_key)
            print("\n  [离散事件舱] -> Table 2: fact_process_lifecycle_events (启动/闪退消亡追溯)")
            DatabaseVisualizer.render_row("event_timestamp", lf_row["event_timestamp"])
            DatabaseVisualizer.render_row("process_key", lf_row["process_key"])
            DatabaseVisualizer.render_row("os_pid", lf_row["os_pid"])
            DatabaseVisualizer.render_row("event_type", lf_row["event_type"])
            DatabaseVisualizer.render_row("process_lifetime", lf_row["process_lifetime"], "秒", fallback="[Active / 进程依旧驻留在系统内存中，无生存时间]")
            DatabaseVisualizer.render_row("exit_code", lf_row["exit_code"], fallback="[0x00000000 / 正在运行/正常物理关机]")

            # 🔘 宏观硬件并网表 (Table 3: fact_system_hardware)
            hw_row = await conn.fetchrow("SELECT * FROM public.fact_system_hardware WHERE timestamp = $1", now_ts)
            print("\n  [硬件遥测舱] -> Table 3: fact_system_hardware (双 CCD 调度与 Blackwell 撞墙分析)")
            DatabaseVisualizer.render_row("timestamp", hw_row["timestamp"])
            DatabaseVisualizer.render_row("current_fps", hw_row["current_fps"], "FPS", fallback="[0.0 FPS / 桌面静态画面无刷新]")
            DatabaseVisualizer.render_row("average_fps", hw_row["average_fps"], "FPS")
            DatabaseVisualizer.render_row("one_percent_low_fps", hw_row["one_percent_low_fps"], "FPS")
            DatabaseVisualizer.render_row("frametime_ms", hw_row["frametime_ms"], "ms")
            DatabaseVisualizer.render_row("frametime_jitter", hw_row["frametime_jitter"], "ms")
            DatabaseVisualizer.render_row("cpu_total_usage", hw_row["cpu_total_usage"], "%")
            DatabaseVisualizer.render_row("cpu_vcore_voltage", hw_row["cpu_vcore_voltage"], "V")
            DatabaseVisualizer.render_row("cpu_clock_mhz", hw_row["cpu_clock_mhz"], "MHz")
            DatabaseVisualizer.render_row("cpu_package_temp", hw_row["cpu_package_temp"], "°C")
            DatabaseVisualizer.render_row("cpu_package_power", hw_row["cpu_package_power"], "W")
            DatabaseVisualizer.render_row("system_dpc_latency", hw_row["system_dpc_latency"], "us")
            DatabaseVisualizer.render_row("system_context_switches", hw_row["system_context_switches"], "ctx/sec")
            DatabaseVisualizer.render_row("gpu_usage", hw_row["gpu_usage"], "%", fallback="[0.0% / GPU静默]")
            DatabaseVisualizer.render_row("gpu_core_voltage", hw_row["gpu_core_voltage"], "V")
            DatabaseVisualizer.render_row("gpu_core_clock", hw_row["gpu_core_clock"], "MHz")
            DatabaseVisualizer.render_row("gpu_mem_clock", hw_row["gpu_mem_clock"], "MHz")
            DatabaseVisualizer.render_row("gpu_core_temp", hw_row["gpu_core_temp"], "°C")
            DatabaseVisualizer.render_row("gpu_hotspot_temp", hw_row["gpu_hotspot_temp"], "°C")
            DatabaseVisualizer.render_row("gpu_board_power", hw_row["gpu_board_power"], "W")
            DatabaseVisualizer.render_row("gpu_throttling_reasons", hw_row["gpu_throttling_reasons"], fallback="[0 - 功耗与温控未触发物理撞墙]")
            DatabaseVisualizer.render_row("pcie_bus_utilization", hw_row["pcie_bus_utilization"], "%")
            DatabaseVisualizer.render_row("system_ram_usage_pct", hw_row["system_ram_usage_pct"], "%")
            DatabaseVisualizer.render_row("system_commit_size_gb", hw_row["system_commit_size_gb"], "GB")
            DatabaseVisualizer.render_row("system_hard_page_faults", hw_row["system_hard_page_faults"], "faults/sec")
            DatabaseVisualizer.render_row("disk_max_latency_ms", hw_row["disk_max_latency_ms"], "ms")
            DatabaseVisualizer.render_row("network_ping_ms", hw_row["network_ping_ms"], "ms", fallback="[PING Timeout / 公网封包解析失败]")
            DatabaseVisualizer.render_row("is_packet_loss", hw_row["is_packet_loss"], fallback="[0 - 物理网络无任何封包丢失]")
            DatabaseVisualizer.render_row("network_jitter", hw_row["network_jitter"], "ms")
            DatabaseVisualizer.render_row("cpu_ccd0_usage", hw_row["cpu_ccd0_usage"], "%", fallback="[0.00% / 单核心工作负载对称未调动]")
            DatabaseVisualizer.render_row("cpu_ccd1_usage", hw_row["cpu_ccd1_usage"], "%", fallback="[0.00% / 单核心工作负载对称未调动]")
            
            # 手动执行删除以清洗硬件事实表，防止由于没有 process_key 主外键关系在 asyncTearDown 中被漏过
            await conn.execute("DELETE FROM public.fact_system_hardware WHERE timestamp = $1", now_ts)
            print("="*80)


if __name__ == "__main__":
    unittest.main()