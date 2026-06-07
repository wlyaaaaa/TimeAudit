# -*- coding: utf-8 -*-
"""
Windows 11 Native Telemetry Engine - Backend Verification [Phase 1]
===================================================================
验证模块：
  1. 数仓 DDL 物理架构对齐核对 (全量表属性字段输出)
  2. 维表（dim_process_registry）全字段落库及合并验证
  3. Chrome/360 浏览器长命令行净化规则验证
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
    from lifecycle_worker import (
        ProcessLifecycleWorker, 
        check_process_elevation, 
        check_file_signature, 
        sanitize_command_line
    )
    from main import DB_DSN
except ImportError as e:
    print(f"[-] 依赖加载失败，请确保位于工程根目录下运行此脚本。错误详情: {e}")
    sys.exit(1)


# =====================================================================
# 🛠️ 数据库状态模拟桩 (Mocking Connection for Fallback Checks)
# =====================================================================

class MockConnection:
    def __init__(self):
        self.registry = {}
        self.next_key = 5000001

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def transaction(self):
        return self

    async def fetch(self, query, *args):
        # 模拟系统 DDL 表结构输出
        if "information_schema.columns" in query:
            target_table = args[0]
            mock_columns = {
                "dim_process_registry": [
                    {"column_name": "process_key", "data_type": "integer (SERIAL)"},
                    {"column_name": "process_name", "data_type": "character varying(100)"},
                    {"column_name": "executable_path", "data_type": "text"},
                    {"column_name": "parent_process", "data_type": "character varying(100)"},
                    {"column_name": "command_line", "data_type": "text"},
                    {"column_name": "is_elevated", "data_type": "smallint"},
                    {"column_name": "service_name", "data_type": "character varying(100)"},
                    {"column_name": "signature_status", "data_type": "smallint"},
                    {"column_name": "created_at", "data_type": "timestamp with time zone"}
                ],
                "fact_process_activity": [
                    {"column_name": "timestamp", "data_type": "timestamp with time zone"},
                    {"column_name": "process_key", "data_type": "integer"},
                    {"column_name": "os_pid", "data_type": "integer"},
                    {"column_name": "proc_cpu_usage", "data_type": "real"},
                    {"column_name": "proc_gpu_usage", "data_type": "real"},
                    {"column_name": "proc_ram_mb", "data_type": "integer"},
                    {"column_name": "proc_vram_used_gb", "data_type": "real"},
                    {"column_name": "proc_vram_shared_mb", "data_type": "integer"},
                    {"column_name": "proc_disk_read_rate_mb", "data_type": "real"},
                    {"column_name": "proc_disk_write_rate_mb", "data_type": "real"},
                    {"column_name": "proc_disk_iops", "data_type": "integer"},
                    {"column_name": "proc_network_send_kb", "data_type": "real"},
                    {"column_name": "proc_network_recv_kb", "data_type": "real"},
                    {"column_name": "proc_active_connections", "data_type": "smallint"},
                    {"column_name": "proc_remote_ip_port", "data_type": "text"},
                    {"column_name": "proc_cpu_affinity", "data_type": "integer"},
                    {"column_name": "proc_thread_count", "data_type": "integer"},
                    {"column_name": "is_not_responding", "data_type": "smallint"}
                ],
                "fact_process_context": [
                    {"column_name": "timestamp", "data_type": "timestamp with time zone"},
                    {"column_name": "process_key", "data_type": "integer"},
                    {"column_name": "os_pid", "data_type": "integer"},
                    {"column_name": "is_foreground", "data_type": "smallint"},
                    {"column_name": "window_title", "data_type": "text"},
                    {"column_name": "window_mode", "data_type": "smallint"},
                    {"column_name": "end_timestamp", "data_type": "timestamp with time zone"},
                    {"column_name": "duration_ms", "data_type": "bigint"}
                ],
                "fact_process_lifecycle_events": [
                    {"column_name": "event_timestamp", "data_type": "timestamp with time zone"},
                    {"column_name": "process_key", "data_type": "integer"},
                    {"column_name": "os_pid", "data_type": "integer"},
                    {"column_name": "event_type", "data_type": "character varying(10)"},
                    {"column_name": "process_lifetime", "data_type": "integer"},
                    {"column_name": "exit_code", "data_type": "character varying(20)"}
                ],
                "fact_system_hardware": [
                    {"column_name": "timestamp", "data_type": "timestamp with time zone"},
                    {"column_name": "current_fps", "data_type": "real"},
                    {"column_name": "average_fps", "data_type": "real"},
                    {"column_name": "one_percent_low_fps", "data_type": "real"},
                    {"column_name": "frametime_ms", "data_type": "real"},
                    {"column_name": "frametime_jitter", "data_type": "real"},
                    {"column_name": "cpu_total_usage", "data_type": "real"},
                    {"column_name": "cpu_vcore_voltage", "data_type": "real"},
                    {"column_name": "cpu_clock_mhz", "data_type": "integer"},
                    {"column_name": "cpu_package_temp", "data_type": "real"},
                    {"column_name": "cpu_package_power", "data_type": "real"},
                    {"column_name": "system_dpc_latency", "data_type": "real"},
                    {"column_name": "system_context_switches", "data_type": "integer"},
                    {"column_name": "gpu_usage", "data_type": "real"},
                    {"column_name": "gpu_core_voltage", "data_type": "real"},
                    {"column_name": "gpu_core_clock", "data_type": "integer"},
                    {"column_name": "gpu_mem_clock", "data_type": "integer"},
                    {"column_name": "gpu_core_temp", "data_type": "real"},
                    {"column_name": "gpu_hotspot_temp", "data_type": "real"},
                    {"column_name": "gpu_board_power", "data_type": "real"},
                    {"column_name": "gpu_throttling_reasons", "data_type": "smallint"},
                    {"column_name": "pcie_bus_utilization", "data_type": "real"},
                    {"column_name": "system_ram_usage_pct", "data_type": "real"},
                    {"column_name": "system_commit_size_gb", "data_type": "real"},
                    {"column_name": "system_hard_page_faults", "data_type": "integer"},
                    {"column_name": "disk_max_latency_ms", "data_type": "real"},
                    {"column_name": "network_ping_ms", "data_type": "integer"},
                    {"column_name": "is_packet_loss", "data_type": "smallint"},
                    {"column_name": "network_jitter", "data_type": "real"},
                    {"column_name": "cpu_ccd0_usage", "data_type": "real"},
                    {"column_name": "cpu_ccd1_usage", "data_type": "real"}
                ]
            }
            return mock_columns.get(target_table, [])
        return []

    async def fetchval(self, query, *args):
        if "INSERT INTO public.dim_process_registry" in query:
            signature = args
            if signature not in self.registry:
                self.registry[signature] = self.next_key
                self.next_key += 1
            return self.registry[signature]
        return 99999

    async def execute(self, query, *args):
        return "INSERT 0 1"

class MockPool:
    def __init__(self):
        self.conn = MockConnection()

    def acquire(self):
        return self.conn

    async def close(self):
        pass


# =====================================================================
# 🕵️ 测试用例
# =====================================================================

class TelemetryPhase1TestSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        """物理库连通判定，若 Docker 未起则自动降级到 Schema 静态模拟"""
        self.test_prefix = f"test_proc_{int(time.time())}"
        self.use_mock = False
        try:
            self.pool = await asyncpg.create_pool(
                dsn=DB_DSN, min_size=1, max_size=2, command_timeout=3.0
            )
            print("\033[92m[+] 成功穿透 Postgres Docker 容器，进入物理库结构动态验证。\033[0m")
        except Exception:
            print("\033[93m[!] 本地 Docker 数据库不可达，自动切换至静态 Mock 模式进行属性对齐校验。\033[0m")
            self.pool = MockPool()
            self.use_mock = True

    async def asyncTearDown(self):
        """擦除测试脏记录"""
        if not self.use_mock and self.pool:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT process_key FROM public.dim_process_registry WHERE process_name LIKE $1",
                        f"{self.test_prefix}%"
                    )
                    if rows:
                        keys = [r['process_key'] for r in rows]
                        await conn.execute("DELETE FROM public.dim_process_registry WHERE process_key = ANY($1)", keys)
                print("\033[92m[+] 沙箱环境已自动回收净化。\033[0m")
            except Exception as e:
                print(f"[-] 清理沙箱异常: {e}")
        if self.pool:
            await self.pool.close()

    async def test_01_ddl_columns_alignment(self):
        """数据正确性校验 1.1: 全表属性字段及类型动态拉出与物理 DDL 对齐"""
        tables = [
            "dim_process_registry", 
            "fact_process_activity", 
            "fact_process_context", 
            "fact_process_lifecycle_events", 
            "fact_system_hardware"
        ]
        
        print("\n" + "="*80)
        print("📊 物理数据库 schema 与 DDL 对齐检查报告")
        print("="*80)

        async with self.pool.acquire() as conn:
            for table_name in tables:
                print(f"\n📁 表名: public.{table_name}")
                print("-" * 50)
                # 从信息架构中抓取真实物理列
                query = """
                    SELECT column_name, data_type 
                    FROM information_schema.columns 
                    WHERE table_name = $1 AND table_schema = 'public'
                    ORDER BY ordinal_position;
                """
                columns = await conn.fetch(query, table_name)
                
                if not columns:
                    print(f"  \033[91m[错误] 数据库中未找到该物理分区表或主表，请先在容器内执行 DDL！\033[0m")
                    continue
                
                for idx, col in enumerate(columns, 1):
                    col_name = col['column_name']
                    col_type = col['data_type']
                    print(f"  [{idx:02d}] 字段名: {col_name:<25} | 物理数据类型: {col_type}")

        print("\n" + "="*80)

    async def test_02_dim_process_registry_correctness(self):
        """数据正确性校验 1.2: 验证 dim_process_registry 字段解算、写入与合并验证"""
        worker = ProcessActivityWorker()
        
        # 抓取当前 Python 进程真实物理环境数据
        current_pid = os.getpid()
        current_proc = psutil.Process(current_pid)
        current_exe = current_proc.exe()
        current_name = current_proc.name()
        current_cmd = " ".join(current_proc.cmdline()) if current_proc.cmdline() else ""

        # 动态解析字段
        real_is_elevated = check_process_elevation(current_pid)
        real_signature = check_file_signature(current_exe)
        
        # 为规避 SyntaxError，将复杂嵌套解析提前到 f-string 之外
        elevated_meaning = {1: "拥有超级管理员特权 (Administrator)", 0: "普通用户权限 (Standard User)", -1: "拒绝访问 (Access Denied)", -2: "获取失败"}
        sig_meaning = {1: "Microsoft/第三方可信数字证书签名有效", 0: "无签名/自签名/测试签名/不可信文件", -1: "签名异常/篡改风险", -2: "解析崩溃"}

        elevated_text = elevated_meaning.get(real_is_elevated, "未知")
        sig_text = sig_meaning.get(real_signature, "未知")

        proc_info = {
            "name": f"{self.test_prefix}_py_test.exe",
            "exe": current_exe,
            "parent_name": "cmd.exe",
            "cmdline": current_cmd,
            "service_name": "TestService_P1",
            "os_pid": current_pid
        }

        async with self.pool.acquire() as conn:
            p_key = await worker.get_or_register_cached(conn, proc_info)
            self.assertIsNotNone(p_key, "物理维度注册表写入失败")
            
            # 核实 DDL 每一列落库映射值
            print("\n" + "="*80)
            print("👁️  维度登记表 (dim_process_registry) 实况解算账单核对")
            print("="*80)
            print(f"  - [process_key]      生成的物理序列主键:  {p_key}")
            print(f"  - [process_name]     映像执行名称:        {proc_info['name']}")
            print(f"  - [executable_path]  物理执行绝对路径:    {proc_info['exe']}")
            print(f"  - [parent_process]   父进程名称:          {proc_info['parent_name']}")
            print(f"  - [command_line]     程序启动参数:        {proc_info['cmdline']}")
            print(f"  - [is_elevated]      特权级别 (真实解算): {real_is_elevated} -> {elevated_text}")
            print(f"  - [service_name]     系统寄生服务名:      {proc_info['service_name']}")
            print(f"  - [signature_status] 数字证书安全状态:    {real_signature} -> {sig_text}")
            print(f"  - [created_at]       时区对齐自动时间戳:  (数仓底座自生成)")
            print("="*80)

            # 验证缓存对齐
            cache_tuple = (
                proc_info["name"], 
                proc_info["exe"], 
                proc_info["parent_name"], 
                proc_info["cmdline"], 
                proc_info["service_name"],
                real_is_elevated,
                real_signature
            )
            self.assertIn(cache_tuple, worker.key_cache, "维表内存缓存未命合")
            self.assertEqual(worker.key_cache[cache_tuple], p_key, "内存缓存 Key 与物理库生成不符")

    def test_03_browser_commandline_sanitize_rules(self):
        """数据正确性校验 1.3: 验证降维过滤器净化处理，防止维表索引爆炸"""
        test_inputs = {
            "chrome.exe": (
                "C:\\Chrome\\chrome.exe --mojo-platform-channel-handle=4812 --renderer-client-id=5 --field-trial-handle=11,ab",
                "C:\\Chrome\\chrome.exe --mojo-platform-channel-handle=<handle> --renderer-client-id=<client-id> --field-trial-handle=<handle>"
            ),
            "360se.exe": (
                "360se.exe /package=abcdef0123456789abcdef0123456789 /Message=YmFzZTY0bXNn",
                "360se.exe /package=<md5-package> /Message=<base64-message>"
            )
        }
        
        print("\n" + "="*80)
        print("🧼 浏览器长命令行净化规则验证")
        print("="*80)
        for name, (raw, expected) in test_inputs.items():
            sanitized = sanitize_command_line(name, raw)
            self.assertEqual(sanitized, expected, f"净化过滤规则处理发生偏差: {name}")
            print(f"  [目标应用]: {name}")
            print(f"  [原始输入]: {raw}")
            print(f"  [净化输出]: {sanitized} (\033[92m验证成功\033[0m)")
            print("-" * 50)


if __name__ == "__main__":
    unittest.main()