# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import asyncio
import datetime
import sys
import os
import time
import asyncpg
import psutil
import ctypes  

LOG_FILE = r"E:\TimeAudit\telemetry.log"
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout
except Exception:
    pass

from hardware_worker import HardwareTelemetryWorker
from context_worker import WindowStateTracker
from activity_worker import ProcessActivityWorker
from lifecycle_worker import ProcessLifecycleWorker

DB_DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"
WARMUP_INTERVAL_SEC = 43200  

def enforce_singleton():
    mutex_name = "Global\\TimeAuditTelemetryEngineMutex"
    kernel32 = ctypes.windll.kernel32
    
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()
    
    if last_error == 183:
        print(f"[{datetime.datetime.now().strftime('%X')} 🔄 强制重启] 检测到互斥体占用，启动覆盖抢占机制...")
        
        current_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['pid'] != current_pid and proc.info['name'] and 'python' in proc.info['name'].lower():
                    cmdline = proc.info['cmdline']
                    if cmdline and any('main.py' in arg for arg in cmdline):
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        kernel32.CloseHandle(mutex)
        
        for _ in range(10):
            time.sleep(0.5)
            mutex = kernel32.CreateMutexW(None, False, mutex_name)
            if kernel32.GetLastError() != 183:
                print(f"[{datetime.datetime.now().strftime('%X')} ✅ 抢占成功] 旧实例已蒸发，新引擎接管内核锁。")
                return mutex
            kernel32.CloseHandle(mutex)
            
        print("[主控] ❌ 抢占失败！当前进程安全退出。")
        sys.exit(0)
        
    return mutex

def get_week_bounds(delta_weeks=0):
    # 【修复】：将系统时区对齐由本地时区偏移彻底移至 UTC(+00) 时间轴，对齐 fact 时序写入，消灭分交界处的溢出崩溃
    today = datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(weeks=delta_weeks)
    monday = today - datetime.timedelta(days=today.weekday())
    next_monday = monday + datetime.timedelta(days=7)
    iso_year, iso_week, _ = monday.isocalendar()
    return iso_year, iso_week, f"{monday} 00:00:00+00", f"{next_monday} 00:00:00+00"

def get_month_bounds(delta_months=0):
    # 【修复】：同样对齐至标准 UTC(+00) 进行时序物理分区边界构建
    today = datetime.datetime.now(datetime.timezone.utc).date()
    year = today.year
    month = today.month + delta_months
    while month > 12:
        month -= 12
        year += 1
    start_date = datetime.date(year, month, 1)
    end_date = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return year, month, f"{start_date} 00:00:00+00", f"{end_date} 00:00:00+00"

async def auto_warmup_partitions(pool):
    async with pool.acquire() as conn:
        print(f"[{datetime.datetime.now().strftime('%X')} 🔧 预热引擎] 开始对齐时序物理分区桶...")
        for delta in [0, 1]:
            year, week, start, end = get_week_bounds(delta)
            suffix = f"y{year}w{week:02d}"
            for parent_table in ["fact_process_context", "fact_process_activity"]:
                sub_table = f"{parent_table}_{suffix}"
                query = f"""
                    CREATE TABLE IF NOT EXISTS public.{sub_table} 
                    PARTITION OF public.{parent_table} 
                    FOR VALUES FROM ('{start}') TO ('{end}');
                """
                await conn.execute(query)
                
        for delta in [0, 1]:
            year, month, start, end = get_month_bounds(delta)
            sub_table = f"fact_system_hardware_y{year}m{month:02d}"
            query = f"""
                CREATE TABLE IF NOT EXISTS public.{sub_table} 
                PARTITION OF public.fact_system_hardware 
                FOR VALUES FROM ('{start}') TO ('{end}');
            """
            await conn.execute(query)
        print("[✅ 预热引擎] 当期及未来分区舱室绑定完毕！")

async def main():
    _singleton_mutex = enforce_singleton()

    print("====================================================")
    print(f"🚀 Windows 11 Native Telemetry Engine 正在拉起... [PID: {os.getpid()}]")
    print("====================================================")
    
    pool = None
    while pool is None:
        try:
            pool = await asyncpg.create_pool(
                dsn=DB_DSN, min_size=2, max_size=12, command_timeout=5.0
            )
            print("[连接池] 成功创建数据库连接池！")
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%X')} ⚠️ 等待数仓] {e}，5秒后重试...")
            await asyncio.sleep(5)

    last_warmup_time = 0
    global_pid_key_map = {}
    
    tracker = WindowStateTracker()
    hardware_worker = HardwareTelemetryWorker()
    activity_worker = ProcessActivityWorker()
    lifecycle_worker = ProcessLifecycleWorker(global_pid_key_map)
    
    lifecycle_worker.start_kernel_listener(pool)
    
    print("[主控] 正在同步系统当前活体运行映像...")
    async with pool.acquire() as conn:
        for proc in psutil.process_iter(['pid']):
            try: 
                await lifecycle_worker.register_live_pid(conn, proc.info['pid'])
            except Exception: 
                continue
    print(f"[主控] 初次活体映射扫描完毕，共计登记了 {len(global_pid_key_map)} 个存活进程。")
    
    last_known_pid = None
    fg_app_name = ""
    last_tick_time = asyncio.get_event_loop().time()
    
    try:
        while True:
            t0 = asyncio.get_event_loop().time()
            
            # 【优化】：虽然底层硬件检测与性能轮询保持在 1Hz，但主进程由于需要默认 3 秒收集一次，休眠发生跨度判定调整为 10.0 秒
            if t0 - last_tick_time > 10.0:
                print(f"[{datetime.datetime.now().strftime('%X')} 🔄 休眠监测] 时钟发生大跨度跳变({t0 - last_tick_time:.1f}秒)，判定系统已恢复唤醒...")
            
            if t0 - last_warmup_time >= WARMUP_INTERVAL_SEC or last_warmup_time == 0:
                try:
                    await auto_warmup_partitions(pool)
                    last_warmup_time = t0
                except Exception as e:
                    print(f"❌ 警告: 自动分区预热失败, 详情: {e}")

                try:
                    MAX_LOG_SIZE_MB = 50
                    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE_MB * 1024 * 1024:
                        current_out = sys.stdout
                        sys.stdout = sys.__stdout__
                        sys.stderr = sys.__stderr__
                        if current_out and not current_out.closed:
                            current_out.close()
                        
                        with open(LOG_FILE, "w", encoding="utf-8") as f:
                            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] ✨ 日志超过 {MAX_LOG_SIZE_MB}MB，已清空截断。\n")
                        
                        sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
                        sys.stderr = sys.stdout
                        print(f"[{datetime.datetime.now().strftime('%X')} 🔄 日志管理] 空间安全释放。")
                except Exception:
                    try:
                        sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
                        sys.stderr = sys.stdout
                    except Exception:
                        pass

            if pool is None:
                try:
                    pool = await asyncpg.create_pool(
                        dsn=DB_DSN, min_size=2, max_size=12, command_timeout=5.0
                    )
                    if hasattr(lifecycle_worker, 'update_pool'):
                        lifecycle_worker.update_pool(pool)
                    print("[连接池] 自愈引擎：重新构建并穿透数据库连接池成功！")
                except Exception as reconnect_err:
                    print(f"[{datetime.datetime.now().strftime('%X')} ⚠️ 自愈重试] 重建连接池失败: {reconnect_err}，等待下次循环...")
                    await asyncio.sleep(5)
                    last_tick_time = asyncio.get_event_loop().time()
                    continue

            batch_timestamp = datetime.datetime.now(datetime.timezone.utc)
            
            try:
                await tracker.poll_heartbeat(pool, batch_timestamp)
            except Exception as worker_err:
                print(f"⚠️ 切窗监测工作异常: {worker_err}")
            
            if tracker.last_process_key:
                if tracker.last_pid != last_known_pid:
                    try:
                        fg_app_name = psutil.Process(tracker.last_pid).name()
                        last_known_pid = tracker.last_pid
                    except Exception:
                        fg_app_name = ""
            else:
                fg_app_name = ""
                last_known_pid = None

            try:
                active_procs = activity_worker.collect_active_processes()
                hw_data = hardware_worker.collect_hardware_snapshot(fg_app_name)
                
                await asyncio.gather(
                    hardware_worker.write_to_db(pool, hw_data, batch_timestamp),
                    activity_worker.write_batch_to_db(pool, active_procs, batch_timestamp)
                )
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as db_err:
                print(f"[{datetime.datetime.now().strftime('%X')} 🚨 连接断开] 检测到连接异常: {db_err}")
                if pool:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                    pool = None
                    if hasattr(lifecycle_worker, 'update_pool'):
                        lifecycle_worker.update_pool(None)
            except Exception as loop_err:
                print(f"⚠️ 并发遥测落库异动: {loop_err}")
                
            elapsed = asyncio.get_event_loop().time() - t0
            
            # 【修复】：将主线程采集控制周期改为 3.0 秒
            await asyncio.sleep(max(0.01, 3.0 - elapsed))
            
            last_tick_time = asyncio.get_event_loop().time()
            
    except asyncio.CancelledError:
        print("[主控] 捕获终止信号，停机...")
    finally:
        lifecycle_worker.terminate()
        hardware_worker.terminate()
        if pool:
            await pool.close()
        print("[主控] 遥测管线释放，闭舱。")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    while True:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            break
        except Exception as watchdog_err:
            print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] 🚨 [守护外壳] 监测到主引擎异常!")
            print(f"堆栈异常原因: {watchdog_err}")
            time.sleep(5)