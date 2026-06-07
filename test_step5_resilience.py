import os
import sys
import asyncio
import asyncpg
import time
import datetime
import psutil

try:
    from context_worker import WindowStateTracker
    from hardware_worker import HardwareTelemetryWorker
    from activity_worker import ProcessActivityWorker
    from lifecycle_worker import ProcessLifecycleWorker
except ImportError as e:
    print(f"❌ 导入失败，请检查各模块是否存在: {e}")
    sys.exit(1)

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"

class MockBlockedPool:
    def __init__(self, real_pool):
        self.real_pool = real_pool
        self.block_writes = False

    def acquire(self):
        if self.block_writes:
            raise asyncpg.exceptions.ConnectionDoesNotExistError("🚨 [模拟事故] 无法连接到 Postgres 数仓实例 (模拟断连中...)")
        return self.real_pool.acquire()

async def run_resilience_and_null_audit():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 阶段 5 终极隔离纯净审计")
    print("====================================================")
    
    # 🟢 记录测试起跑时间戳，建立绝对物理屏障
    test_start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2)

    try:
        real_pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=2, max_size=5)
        pool_proxy = MockBlockedPool(real_pool)
        print("[连接池] 成功挂载双轨测试代理连接池。")
    except Exception as e:
        print(f"❌ 致命错误: 无法连接数仓，无法执行联调: {e}")
        return

    global_pid_key_map = {}
    tracker = WindowStateTracker()
    hardware_worker = HardwareTelemetryWorker()
    activity_worker = ProcessActivityWorker()
    lifecycle_worker = ProcessLifecycleWorker(global_pid_key_map)
    
    lifecycle_worker.start_kernel_listener(real_pool)
    
    print("[主控] 正在进行全系统存量活体进程映像绑定...")
    async with real_pool.acquire() as conn:
        for proc in psutil.process_iter(['pid']):
            try: await lifecycle_worker.register_live_pid(conn, proc.info['pid'])
            except: continue
    print(f"[主控] 绑定完毕，共锁定 {len(global_pid_key_map)} 个存活进程。")

    print("\n🚀 开始全系统并网联调，前 3 秒数据库运行正常...")
    
    for i in range(1, 11):
        t0 = time.perf_counter()
        print(f"⏱️ [周期 {i:02d}/10] ----------------------------------")
        
        if i == 4:
            print("\n🔥 警告：模拟数据库遭遇物理断连！写入挂起，启动本地内存暂挂...")
            pool_proxy.block_writes = True
        elif i == 7:
            print("\n💖 提示：模拟数据库网络恢复！正在执行追溯落盘...")
            pool_proxy.block_writes = False

        try:
            await tracker.poll_heartbeat(pool_proxy)
        except Exception as e:
            print(f"   ⚠️ 切窗自愈提示: {e}")

        fg_app_name = "chrome.exe"
        
        try:
            active_procs = activity_worker.collect_active_processes()
            hw_data = hardware_worker.collect_hardware_snapshot(fg_app_name)
            
            await asyncio.gather(
                hardware_worker.write_to_db(pool_proxy, hw_data),
                activity_worker.write_batch_to_db(pool_proxy, active_procs)
            )
            print("   📊 [遥测舱] 硬件和进程活动数据写入落盘。")
        except Exception as err:
            print(f"   ⚠️ [遥测舱自愈挂起] 数据库当前不可用，遥测暂存本地: {err}")

        pending_ins_cnt = len(tracker.pending_inserts)
        pending_upd_cnt = len(tracker.pending_updates)
        if pending_ins_cnt or pending_upd_cnt:
            print(f"   📥 [容灾暂挂区] 本地内存缓存区积压: 待写入切片={pending_ins_cnt} | 待更新时长={pending_upd_cnt}")

        elapsed = time.perf_counter() - t0
        await asyncio.sleep(max(0.01, 1.0 - elapsed))

    lifecycle_worker.terminate()
    hardware_worker.terminate()

    # 4. 执行终极空值（NULL）数据审计（仅限本次测试写入的数据）
    print("\n🔎 --- 4. 终极数据空值 (NULL) 数据完整性审计 (基于本轮测试样本) ---")
    async with real_pool.acquire() as conn:
        
        async def audit_table(table_name, columns, use_timestamp=True):
            null_cols = []
            for col in columns:
                if use_timestamp:
                    # 🟢 时间隔离审计：仅扫描自脚本启动时间戳以来写入的行
                    query = f"SELECT COUNT(*) FROM public.{table_name} WHERE \"timestamp\" >= $1 AND \"{col}\" IS NULL;"
                    null_count = await conn.fetchval(query, test_start_time)
                else:
                    # 针对 lifecycle 没有 timestamp 列的情况，扫描 event_timestamp
                    query = f"SELECT COUNT(*) FROM public.{table_name} WHERE \"event_timestamp\" >= $1 AND \"{col}\" IS NULL;"
                    null_count = await conn.fetchval(query, test_start_time)
                if null_count > 0:
                    null_cols.append((col, null_count))
            if null_cols:
                print(f"❌ 警告：表 {table_name} 在本轮测试中发现字段为空：")
                for c, count in null_cols:
                    print(f"   - 字段 [{c}] 存在 {count} 行 NULL 值")
            else:
                print(f"✅ 表 {table_name:<30} 完美通过本轮测试空值审计！(零 NULL 空白)")

        # 审计事实表：fact_system_hardware (31 字段)
        hw_cols = [
            "current_fps", "average_fps", "one_percent_low_fps", "frametime_ms", "frametime_jitter",
            "cpu_total_usage", "cpu_vcore_voltage", "cpu_clock_mhz", "cpu_package_temp", "cpu_package_power",
            "system_dpc_latency", "system_context_switches", "gpu_usage", "gpu_core_voltage", "gpu_core_clock",
            "gpu_mem_clock", "gpu_core_temp", "gpu_hotspot_temp", "gpu_board_power", "gpu_throttling_reasons",
            "pcie_bus_utilization", "system_ram_usage_pct", "system_commit_size_gb", "system_hard_page_faults",
            "disk_max_latency_ms", "network_ping_ms", "is_packet_loss", "network_jitter", "cpu_ccd0_usage", "cpu_ccd1_usage"
        ]
        await audit_table("fact_system_hardware", hw_cols, use_timestamp=True)

        # 审计事实表：fact_process_activity (18 字段)
        act_cols = [
            "proc_cpu_usage", "proc_gpu_usage", "proc_ram_mb", "proc_vram_used_gb", "proc_vram_shared_mb",
            "proc_disk_read_rate_mb", "proc_disk_write_rate_mb", "proc_disk_iops", "proc_network_send_kb",
            "proc_network_recv_kb", "proc_active_connections", "proc_remote_ip_port", "proc_cpu_affinity",
            "proc_thread_count", "is_not_responding"
        ]
        await audit_table("fact_process_activity", act_cols, use_timestamp=True)

        # 审计生命周期：fact_process_lifecycle_events
        life_cols = ["event_type", "process_lifetime", "exit_code"]
        await audit_table("fact_process_lifecycle_events", life_cols, use_timestamp=False)

    await real_pool.close()
    print("\n🏁 全系统并网联调与弹性审计结束。")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_resilience_and_null_audit())