# -*- coding: utf-8 -*-
import asyncio
import datetime
import sys
import os    # 🟢 确保导入，修复 PID 物理锁冲突
import time  # 🟢 引入时间模块，用于守护延迟
import asyncpg
import psutil

# 🟢 原生重定向：无感接管控制台标准流到物理日志中，彻底脱离命令行重定向束缚
LOG_FILE = r"E:\TimeAudit\telemetry.log"
try:
    # buffering=1 开启行级缓冲，保证 Get-Content -Wait 能微秒级流式同步读取
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout
except Exception:
    pass

from hardware_worker import HardwareTelemetryWorker
from context_worker import WindowStateTracker
from activity_worker import ProcessActivityWorker
from lifecycle_worker import ProcessLifecycleWorker

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"
WARMUP_INTERVAL_SEC = 43200  

def get_week_bounds(delta_weeks=0):
    today = datetime.date.today() + datetime.timedelta(weeks=delta_weeks)
    monday = today - datetime.timedelta(days=today.weekday())
    next_monday = monday + datetime.timedelta(days=7)
    iso_year, iso_week, _ = monday.isocalendar()
    return iso_year, iso_week, f"{monday} 00:00:00+08", f"{next_monday} 00:00:00+08"

def get_month_bounds(delta_months=0):
    today = datetime.date.today()
    year = today.year
    month = today.month + delta_months
    while month > 12:
        month -= 12
        year += 1
    start_date = datetime.date(year, month, 1)
    end_date = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    return year, month, f"{start_date} 00:00:00+08", f"{end_date} 00:00:00+08"

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
        print("[✅ 预热引擎] 当期及未来周/月度物理存储舱室全面对齐绑定！")

async def main():
    # 🟢 写入 PID 物理锁文件，方便标准权限下的一键体检
    PID_FILE = r"E:\TimeAudit\time_audit.pid"
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    print("====================================================")
    print(f"🚀 Windows 11 Native Telemetry Engine 正在拉起... [PID: {os.getpid()}]")
    print("====================================================")
    
    try:
        pool = await asyncpg.create_pool(
            dsn=DB_DSN, min_size=2, max_size=12, command_timeout=5.0
        )
        print("[连接池] 成功穿透 55432 端口死锁 Postgres 实例连接池！")
    except Exception as e:
        # 🟢 核心改动：不再直接退出整个进程，而是抛出异常让外层自愈外壳捕获并重试
        raise ConnectionError(f"无法连接至 Docker 数仓实例，可能容器内部服务尚未完全就绪。详情: {e}")

    print("[主控] 全时段异步遥测心跳就绪，开始执行高频采样守护...")
    last_warmup_time = 0
    global_pid_key_map = {}
    
    tracker = WindowStateTracker()
    hardware_worker = HardwareTelemetryWorker()
    activity_worker = ProcessActivityWorker()
    lifecycle_worker = ProcessLifecycleWorker(global_pid_key_map)
    
    lifecycle_worker.start_kernel_listener(pool)
    
    print("[主控] 正在对系统当前活体运行进行初次全面映像同步绑定...")
    async with pool.acquire() as conn:
        for proc in psutil.process_iter(['pid']):
            try: await lifecycle_worker.register_live_pid(conn, proc.info['pid'])
            except: continue
    print(f"[主控] 初次活体映射扫描完毕，共计在内存中死锁了 {len(global_pid_key_map)} 个存活进程。")
    
    # 🧠 引入主控内存状态机看板，死锁系统开销
    last_known_pid = None
    fg_app_name = ""
    
    try:
        while True:
            t0 = asyncio.get_event_loop().time()  # 🟢 【精确计时起点】：在每轮物理采样最开始记录时间
            current_time = asyncio.get_event_loop().time()
            
            if current_time - last_warmup_time >= WARMUP_INTERVAL_SEC or last_warmup_time == 0:
                try:
                    await auto_warmup_partitions(pool)
                    last_warmup_time = current_time
                except Exception as e:
                    print(f"❌ 警告: 自动分区预热失败, 请检查 DDL 权限! 详情: {e}")

                # 🟢 【新增：日志体积无感物理截断器】
                try:
                    MAX_LOG_SIZE_MB = 50  # 超过 50MB 自动清空，确保 VS Code 永远秒开
                    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE_MB * 1024 * 1024:
                        # 1. 临时安全剥离当前接管的句柄
                        current_out = sys.stdout
                        sys.stdout = sys.__stdout__
                        sys.stderr = sys.__stderr__
                        if current_out and not current_out.closed:
                            current_out.close()
                        
                        # 2. 用 "w" 模式强行置零（Windows 允许对 Get-Content -Wait 状态下的文件执行覆写清空）
                        with open(LOG_FILE, "w", encoding="utf-8") as f:
                            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] ✨ 日志体积超越 {MAX_LOG_SIZE_MB}MB，已触发自动物理截断清空，开启新一轮无感记录。\n")
                        
                        # 3. 重新以追加行缓冲模式无感接管标准流
                        sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
                        sys.stderr = sys.stdout
                        print(f"[{datetime.datetime.now().strftime('%X')} 🔄 日志管理] 已成功释放磁盘空间占用，VS Code 恢复秒开体验。")
                except Exception as log_err:
                    # 终极容灾恢复：一旦发生未知冲突，必须确保标准流重定向回来，绝对不能让主循环崩掉
                    try:
                        sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
                        sys.stderr = sys.stdout
                    except:
                        pass
            
            # 🎯 管道 1：顺序触发切窗监测
            try:
                await tracker.poll_heartbeat(pool)
            except Exception as worker_err:
                print(f"⚠️ 舱室2工作异常: {worker_err}")
            
            # 🟢 核心优化：只有前台 PID 真正变动的刹那才打捞名字，常态挂机开销归零
            if tracker.last_process_key:
                if tracker.last_pid != last_known_pid:
                    try:
                        fg_app_name = psutil.Process(tracker.last_pid).name()
                        last_known_pid = tracker.last_pid
                    except:
                        fg_app_name = ""
            else:
                fg_app_name = ""
                last_known_pid = None

            try:
                active_procs = activity_worker.collect_active_processes()
                hw_data = hardware_worker.collect_hardware_snapshot(fg_app_name)
                
                # 两轨最纯净的并发入库
                await asyncio.gather(
                    hardware_worker.write_to_db(pool, hw_data),
                    activity_worker.write_batch_to_db(pool, active_procs)
                )
            except Exception as loop_err:
                print(f"⚠️ 并发守护舱突发异动: {loop_err}")
                
            # 🟢 【精确时钟对齐】：计算真实消耗并进行物理扣除，使循环频率高度对齐至恒定 1 秒，彻底消灭累积延迟
            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0.01, 1.0 - elapsed))
            
    except asyncio.CancelledError:
        print("[主控] 捕获线程退出事件，正在下发停机指令...")
    finally:
        lifecycle_worker.terminate()
        await pool.close()
        # 🟢 退出时销毁锁文件
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass
        print("[主控] 探针内核释放完毕，安全退出。")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # 🟢 物理自愈托底外壳逻辑
    while True:
        try:
            asyncio.run(main())
            # 如果 main() 没有任何异常且正常退出了，判定为服务主动结束，打印后退出
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] [守护外壳] 主程序正常闭舱，退出守护。")
            break
        except KeyboardInterrupt:
            print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] [用户指令] 收到 Ctrl+C，正在紧急安全闭舱...")
            break
        except Exception as watchdog_err:
            # 捕获内核抛出的所有致命错误（包括网络连不上、代码未捕获异常）
            print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] 🚨 [⚠️ 守护外壳] 检测到内核突发性崩溃!")
            print(f"异常堆栈/原因: {watchdog_err}")
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] [守护外壳] 正在进入 5 秒自愈冷却期，随后将自动重新拉起内核...\n")
            time.sleep(5)