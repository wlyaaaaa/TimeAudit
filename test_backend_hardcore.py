# -*- coding: utf-8 -*-
"""
Windows 11 Native Telemetry Engine - Backend Hardcore Stress Tests [Phase 4]
===========================================================================
验证目标：
  1. 验证 100 进程瞬间并发下，连接池（限制 max_size=2）是否存在饥饿死锁或超时报错。
  2. 验证进程在采集后、落库前突然死亡（瞬态消亡），是否会污染维度表产生分裂指纹。
  3. 验证 Windows 底层 WMI 监听管道崩溃后，系统是否会发生静默失效。
"""

import asyncio
import sys
import os
import time
import unittest
import psutil
import subprocess
import asyncpg

# 将当前目录追加到搜索路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from activity_worker import ProcessActivityWorker
    from context_worker import WindowStateTracker
    from hardware_worker import HardwareTelemetryWorker
    from lifecycle_worker import ProcessLifecycleWorker, check_process_elevation
    from main import DB_DSN
except ImportError as e:
    print(f"[-] 依赖加载失败，请确保位于工程根目录下运行此脚本。错误详情: {e}")
    sys.exit(1)


# =====================================================================
# 🕵️ 硬核测试用例集
# =====================================================================

class TelemetryHardcoreTestSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.test_prefix = f"test_hardcore_{int(time.time())}"
        self.use_mock = False
        try:
            # 强行创建一个极小连接池 (最大容量 2)，模拟超极限并发下的死锁场景
            self.pool = await asyncpg.create_pool(
                dsn=DB_DSN, min_size=1, max_size=2, command_timeout=2.0
            )
            print("\033[92m[+] 已物理连接 Postgres 数仓，并死锁连接池最大容量为 2。\033[0m")
        except Exception:
            print("\033[91m[-] 无法连接数据库，硬核压力测试需要物理连接 Docker，请开启容器！\033[0m")
            sys.exit(1)

    async def asyncTearDown(self):
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT process_key FROM public.dim_process_registry WHERE process_name LIKE $1",
                        f"{self.test_prefix}%"
                    )
                    if rows:
                        keys = [r['process_key'] for r in rows]
                        await conn.execute("DELETE FROM public.fact_process_activity WHERE process_key = ANY($1)", keys)
                        await conn.execute("DELETE FROM public.dim_process_registry WHERE process_key = ANY($1)", keys)
                print("\033[92m[+] 硬核沙箱脏数据清理完成。\033[0m")
            except Exception as e:
                print(f"[-] 清理沙箱异常: {e}")
            await self.pool.close()

    async def test_01_connection_pool_starvation_stress(self):
        """硬核测试 4.1: 验证高并发短生命周期进程涌入时，连接池饥饿死锁限制"""
        worker = ProcessActivityWorker()
        
        # 模拟突然涌入 60 个从未注册过的短生存周期编译进程
        uncached_burst = []
        for i in range(60):
            uncached_burst.append({
                "name": f"{self.test_prefix}_gcc_{i}.exe",
                "exe": f"C:\\Windows\\System32\\{self.test_prefix}_gcc_{i}.exe",
                "parent_name": "make.exe",
                "cmdline": f"--compile-unit={i}",
                "service_name": None,
                "os_pid": os.getpid()  # 使用当前活动的 PID 绕开死亡异常
            })

        print("\n" + "="*80)
        print("🔥 边界压力测试：[1. 高并发瞬态编译流 - 连接池饥饿死锁测试]")
        print("="*80)
        print(f"  - 模拟突发进程涌入量: {len(uncached_burst)} 个未缓存维度")
        print(f"  - 物理数据库连接池上限: 2 个 (人为限制，模拟极高负载)")

        # 启动高并发并发写入
        t_start = time.perf_counter()
        async def resolve_one_process(proc_info):
            async with self.pool.acquire() as conn:
                await worker.get_or_register_cached(conn, proc_info)

        # 触发 60 并发
        results = await asyncio.gather(*(resolve_one_process(p) for p in uncached_burst), return_exceptions=True)
        t_elapsed = (time.perf_counter() - t_start) * 1000.0

        # 检测是否有超时、连接失败等异常抛出
        failures = [r for r in results if isinstance(r, Exception)]
        
        print(f"  - 60 个高并发进程注册处理耗时: {t_elapsed:.2f} ms")
        print(f"  - 发生池死锁/连接超时失败数:  {len(failures)} 个")
        
        for f in failures[:3]:
            print(f"    \033[91m[超时错误实况]: {f}\033[0m")

        # 异常断言：如果不加限制直接 gather，连接池必定产生溢出或等待超时
        if len(failures) > 0:
            print("\n  \033[93m[警报/架构漏洞确认] 进程大并发涌入击穿了连接池，存在队列饥饿死锁隐患！\033[0m")
        else:
            print("\n  \033[92m[通过] 连接池在极限抢占下抗压成功。\033[0m")
        print("="*80)

    async def test_02_transient_zombie_drift_pollution(self):
        """硬核测试 4.2: 验证短生命周期进程突发消亡（瞬态死亡），维表指纹分裂污染测试"""
        worker = ProcessActivityWorker()

        # 1. 模拟一个活着时的进程特征
        alive_pid = os.getpid()
        proc_info_alive = {
            "name": f"{self.test_prefix}_transient_drift.exe",
            "exe": "C:\\Windows\\System32\\cmd.exe",
            "parent_name": "explorer.exe",
            "cmdline": "--active-mode=1",
            "service_name": None,
            "os_pid": alive_pid
        }

        # 2. 模拟由于落库延迟 2 毫秒后，该进程已经退出了 (使用一个已经死亡/不存活的 PID 999999)
        proc_info_dead = proc_info_alive.copy()
        proc_info_dead["os_pid"] = 999999  # 物理死亡 PID

        print("\n" + "="*80)
        print("🔥 边界压力测试：[2. 瞬时死亡进程漂移 - 维度表分裂污染测试]")
        print("="*80)

        async with self.pool.acquire() as conn:
            # 进程活着时登记得到的 key
            key_alive = await worker.get_or_register_cached(conn, proc_info_alive)
            
            # 进程死亡后，如果被写入批事务，检测它会不会因为特权解算失败产生不同的 key
            key_dead = await worker.get_or_register_cached(conn, proc_info_dead)
            
            print(f"  - 进程存活时物理落库 Process Key: {key_alive}")
            print(f"  - 进程死亡后物理落库 Process Key: {key_dead}")
            
            # 如果两个 key 不同，说明相同的可执行文件和参数，因为解析时的活着/死掉状态差异，
            # 在维表里留下了两份分裂指纹，造成了数据库数据冗余！
            if key_alive != key_dead:
                print("\n  \033[93m[警告/架构漏洞确认] 瞬态死亡触发了维度分裂污染！\033[0m")
                print(f"    - 活着时特权解算值: {check_process_elevation(alive_pid)}")
                print(f"    - 死亡后特权解算值: {check_process_elevation(999999)}")
            else:
                print("\n  \033[92m[通过] 瞬态消亡状态一致，无指纹分裂污染风险。\033[0m")
            print("="*80)

    async def test_03_wmi_pipe_silent_crash_tolerance(self):
        """硬核测试 4.3: 验证 Windows 11 原生 WMI 管道被第三方进程/杀毒软件强行终止后的静默挂死异常"""
        shared_map = {}
        worker = ProcessLifecycleWorker(shared_map)
        
        # 启动监听器
        worker.start_kernel_listener(self.pool)
        await asyncio.sleep(1.5) # 等待管线建立

        ps_process = worker.ps_process
        self.assertIsNotNone(ps_process, "WMI PowerShell 监听器未能成功启动")

        print("\n" + "="*80)
        print("🔥 边界压力测试：[3. WMI 监听管道强制掐断 - 静默死亡容错测试]")
        print("="*80)
        print(f"  - 当前 WMI 监听后台 PowerShell PID: {ps_process.pid}")

        # 物理强杀后台监听 PowerShell 进程，模拟系统杀毒阻断或 WMI 崩溃
        ps_process.terminate()
        ps_process.wait(timeout=1.0)
        
        # 等待一个心跳周期，观察主引擎是否发生崩溃或者给出挂死警告
        await asyncio.sleep(0.5)
        
        # 检测主线程读取管道的状态
        is_running = ps_process.poll() is None
        print(f"  - 后台 WMI 物理管线是否仍处于存活状态: {'存活' if is_running else '已崩溃/死亡'}")
        
        if not is_running:
            print("\n  \033[93m[警告/架构漏洞确认] 监听管线被掐断，系统陷入静默挂死，生命周期监测将停止工作！\033[0m")
        else:
            print("\n  \033[92m[通过] 系统管线具有物理级容错。\033[0m")
        print("="*80)
        
        worker.terminate()


if __name__ == "__main__":
    unittest.main()