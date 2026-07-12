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
import threading
import re

# ==========================================
# 【保留】：全局日志路径定义
# ==========================================
LOG_FILE = r"E:\Projects\Tools\TimeAudit\telemetry.log"

# ==========================================
# 线程安全日志代理（支持盘符自愈与屏幕+文件双工同步输出）
# ==========================================
class SafeStdoutWrapper:
    def __init__(self, filepath):
        self.filepath = filepath
        self.lock = threading.Lock()
        self.terminal = sys.__stdout__  # 备份原始标准控制台输出句柄
        
        # 盘符自愈回退逻辑。若 E:\ 盘由于物理硬件原因不存在，自动回退到当前代码运行目录下创建 telemetry.log
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            self.file = open(filepath, "a", encoding="utf-8", buffering=1)
        except Exception:
            fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.log")
            self.filepath = fallback_path
            self.file = open(fallback_path, "a", encoding="utf-8", buffering=1)
    
    def write(self, data):
        with self.lock:
            try:
                self.file.write(data)
            except Exception:
                pass
            try:
                self.terminal.write(data)  # 同步双写输出到物理终端屏幕
            except Exception:
                pass
    
    def flush(self):
        with self.lock:
            try:
                self.file.flush()
            except Exception:
                pass
            try:
                self.terminal.flush()
            except Exception:
                pass
                
    def truncate_log(self, max_size_mb):
        with self.lock:
            try:
                if os.path.exists(self.filepath) and os.path.getsize(self.filepath) > max_size_mb * 1024 * 1024:
                    self.file.close()
                    with open(self.filepath, "w", encoding="utf-8") as f:
                        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %X')}] ✨ 日志超过 {max_size_mb}MB，已自动安全清空截断。\n")
                    self.file = open(self.filepath, "a", encoding="utf-8", buffering=1)
            except Exception:
                pass

# 启动安全双工流代理，仅全局实例化
safe_logger = SafeStdoutWrapper(LOG_FILE)

from hardware_worker import HardwareTelemetryWorker
from context_worker import WindowStateTracker
from activity_worker import ProcessActivityWorker
from lifecycle_worker import ProcessLifecycleWorker
from runtime_health import command_line_targets_script, write_telemetry_heartbeat

DB_DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"
WARMUP_INTERVAL_SEC = 43200
# 【数据保留 / 三年可行性】高频明细 fact_process_activity 约 2GB/周，三年约 312GB；E 盘 2.3TB 余量充足，
# 故运行三年完全可行、且无需中途清理。RETENTION_DAYS 仅作超长期 7x24 运行的"防磁盘爆满"兜底底线：
# 自动 DROP 分区上界早于该天数的周/月子分区，并清理两张非分区表(生命周期事件、AHK 工时)的超期行。
# 默认 1200 天(≈3.3 年) > 3 年，故运行三年内绝不触发任何删除；置 0 可彻底禁用保留清理(永久保留全史)。
RETENTION_DAYS = 1200
# 墙钟跨度超过此值即判定刚从系统睡眠/休眠(S3/S4)唤醒。正常节拍约 3s，故 60s 阈值不会被 GC/DB 抖动误触，
# 而真实睡眠必远超之。用墙钟(time.time())而非 asyncio 的 monotonic 时钟——后者在系统睡眠时会暂停，
# 旧的 monotonic 跳变侦测因此是永不触发的死代码(Bug1)。
SLEEP_RESUME_THRESHOLD_SEC = 60

def enforce_singleton():
    mutex_name = "Global\\TimeAuditTelemetryEngineMutex"
    kernel32 = ctypes.windll.kernel32
    
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()
    
    if last_error == 183:
        print(f"[{datetime.datetime.now().strftime('%X')} 🔄 强制重启] 检测到互斥体占用，启动覆盖抢占机制...")
        
        current_pid = os.getpid()
        current_exe = os.path.basename(sys.executable).lower()
        target_script = os.path.abspath(__file__)
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                p_pid = proc.info['pid']
                p_name = proc.info['name']
                p_cmdline = proc.info['cmdline']
                
                if p_pid == current_pid or not p_name:
                    continue
                
                if current_exe != "python.exe" and current_exe != "pythonw.exe":
                    if p_name.lower() == current_exe:
                        proc.kill()
                elif 'python' in p_name.lower():
                    if command_line_targets_script(p_cmdline, target_script):
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

# 【关键】分区边界必须对齐北京时区(UTC+8)本地午夜，与 DDL 既有分区(边界 = 本地午夜 = 16:00 UTC)
# 严格一致。否则自动建表会生成 UTC 午夜(= 北京 08:00)边界，与既有分区错位 8 小时，产生重叠/缝隙，
# 导致预建分区用完后 CREATE 失败、写入丢数据。边界串带 +08 偏移，PostgreSQL 会正确存为 timestamptz。
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))

def get_week_bounds(delta_weeks=0):
    today = datetime.datetime.now(CN_TZ).date() + datetime.timedelta(weeks=delta_weeks)
    monday = today - datetime.timedelta(days=today.weekday())
    next_monday = monday + datetime.timedelta(days=7)
    iso_year, iso_week, _ = monday.isocalendar()
    return iso_year, iso_week, f"{monday} 00:00:00+08", f"{next_monday} 00:00:00+08"

def get_month_bounds(delta_months=0):
    today = datetime.datetime.now(CN_TZ).date()
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
        print("[✅ 预热引擎] 当期及未来分区舱室绑定完毕！")

async def auto_retention_cleanup(pool):
    """【数据保留兜底】超长期 7x24 运行时自动清理超过 RETENTION_DAYS 的历史，防止磁盘被高频明细无限撑爆。
    用分区的真实上界(绝对 timestamptz)与 cutoff 比较，只 DROP 整段早于保留期的旧周/月分区(元数据级瞬时操作、
    不产生表膨胀)，绝不误删当期/未来分区；两张非分区表则按时间戳 DELETE 超期行。RETENTION_DAYS=1200(≈3.3 年)
    时三年内绝不触发。任何异常都吞掉、绝不影响采集主循环。"""
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RETENTION_DAYS)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT child.relname AS name, pg_get_expr(child.relpartbound, child.oid) AS bound
                FROM pg_inherits i
                JOIN pg_class child ON child.oid = i.inhrelid
                JOIN pg_class parent ON parent.oid = i.inhparent
                WHERE parent.relname IN ('fact_process_activity','fact_process_context','fact_system_hardware')
            """)
            dropped = 0
            for r in rows:
                m = re.search(r"TO \('([^']+)'\)", r["bound"] or "")
                if not m:
                    continue
                try:
                    upper = datetime.datetime.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if upper <= cutoff:
                    await conn.execute(f'DROP TABLE IF EXISTS public."{r["name"]}"')
                    dropped += 1
                    print(f"[{datetime.datetime.now().strftime('%X')} 🗑️ 保留策略] 已删除超 {RETENTION_DAYS} 天保留期的旧分区 {r['name']} (上界 {upper})")
            await conn.execute("DELETE FROM public.fact_process_lifecycle_events WHERE event_timestamp < $1", cutoff)
            await conn.execute("DELETE FROM public.app_usage_logs WHERE start_time < $1", cutoff)
            if dropped:
                print(f"[✅ 保留策略] 本轮共回收 {dropped} 个超期分区。")
    except Exception as e:
        print(f"[保留策略] 清理跳过(非致命): {e}")

async def cleanup_orphan_context_sessions(pool):
    """【Bug4 修复】启动时闭合上次"关机/崩溃被强杀"遗留的前台 slice：这些行 end_timestamp 永久为 NULL，
    在 Grafana 里会变成"永不结束"的幽灵会话并不断堆积(开机当下库内已实测有 5 条)。无从得知真实结束时刻，
    故保守地以其自身起始时刻闭合(duration_ms=0)——既消除幽灵、又绝不伪造时长。限定近 90 天以利分区裁剪、
    避免全历史顺序扫描。"""
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval("""
                WITH upd AS (
                    UPDATE public.fact_process_context
                    SET end_timestamp = "timestamp", duration_ms = 0
                    WHERE end_timestamp IS NULL
                      AND "timestamp" >= now() - interval '90 days'
                    RETURNING 1
                )
                SELECT count(*) FROM upd;
            """)
        if n:
            print(f"[{datetime.datetime.now().strftime('%X')} 🧹 冷启清理] 闭合上轮关机遗留的 {n} 条未结束前台会话(幽灵 NULL)。")
    except Exception as e:
        print(f"[启动清理] 幽灵会话闭合跳过: {e}")

async def _run_collector():
    # 写入自身真实 PID 到 time_audit.pid，供外部运维/状态检查脚本读取当前采集进程号(单例由内核互斥体
    # 保证唯一，此文件仅作信息记录)。此前该文件无人维护、内容长期是过期 PID；写失败不影响采集。
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "time_audit.pid"), "w") as _pf:
            _pf.write(str(os.getpid()))
    except Exception:
        pass

    print("====================================================")
    print(f"🚀 Windows 11 Native Telemetry Engine 正在拉起... [PID: {os.getpid()}]")
    print("====================================================")
    
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        if not is_admin:
            print("[⚠️ 主控警告] 当前程序未以管理员权限（Elevation）拉起，开机自启任务可能无法成功启用 PresentMon 探测！")
    except Exception:
        pass

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

    last_warmup_wall = 0.0
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

    # 冷启动闭合上次关机遗留的幽灵前台会话(end_timestamp 永久 NULL)。
    await cleanup_orphan_context_sessions(pool)

    last_known_pid = None
    fg_app_name = ""
    wall_anchor = time.time()   # 上一拍进入休眠前记录的墙钟，用于侦测系统睡眠跨度

    try:
        while True:
            t0 = asyncio.get_event_loop().time()
            now_wall = time.time()

            # 【Bug1/3/5 修复】用墙钟跨度侦测系统睡眠/休眠唤醒(asyncio 的 monotonic 在睡眠时暂停，
            # 旧的 monotonic 跳变侦测永不触发、是死代码)。唤醒后立刻：截断前台 slice 到睡前边界(杜绝把
            # 睡眠时长注水成聚焦时长)、重置每进程速率基线、清零上下文切换基线，并强制本拍预热分区。
            if now_wall - wall_anchor > SLEEP_RESUME_THRESHOLD_SEC:
                gap = now_wall - wall_anchor
                pre_sleep_dt = datetime.datetime.fromtimestamp(wall_anchor, datetime.timezone.utc)
                print(f"[{datetime.datetime.now().strftime('%X')} 🔄 唤醒自愈] 墙钟跳变 {gap:.0f}s，判定系统从睡眠/休眠唤醒，正在截断会话并重置速率基线...")
                try:
                    tracker.mark_sleep_boundary(pre_sleep_dt)
                    activity_worker.reset_on_resume()
                    hardware_worker.last_ctx_switches = None
                except Exception as resume_err:
                    print(f"[唤醒自愈] 状态重置部分失败: {resume_err}")
                last_warmup_wall = 0.0   # 睡眠可能已跨自然周/月边界，强制本拍在写库前补建分区

            # 【Bug2 修复】分区预热按"墙钟"调度：旧版用睡眠中暂停的 monotonic，会把 12h 真实间隔拖成
            # 12 天纯活跃时长，极可能跨过自然周/月物理边界 → PostgreSQL "no partition of relation found"
            # 丢数。预热建"当期+下一档"，墙钟 12h 复检即可长期保证当期与下一档始终就绪。
            if last_warmup_wall == 0.0 or (now_wall - last_warmup_wall) >= WARMUP_INTERVAL_SEC:
                try:
                    await auto_warmup_partitions(pool)
                    last_warmup_wall = now_wall
                except Exception as e:
                    print(f"❌ 警告: 自动分区预热失败, 详情: {e}")

                # 与分区预热同周期(12h)执行数据保留兜底清理(三年内不触发，仅超长期防磁盘爆满)
                await auto_retention_cleanup(pool)

                try:
                    safe_logger.truncate_log(50)
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
                    wall_anchor = time.time()
                    await asyncio.sleep(5)
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
                # 【性能/实时性优化】collect_active_processes 内部 psutil.cmdline() 对启动中/受保护进程会触发
                # ERROR_PARTIAL_COPY 内部重试 sleep(cProfile 实测单拍累计可达 ~0.5-1s)。放到工作线程执行，
                # 避免这段同步 sleep 冻结整个 asyncio 事件循环(否则会阻塞 lifecycle 事件处理与连接自愈)。
                # 其访问的跨线程缓存已由 cache_lock 保护；主线程独占缓存仍按 await 序列访问、无并发风险。
                active_procs = await asyncio.to_thread(activity_worker.collect_active_processes)
                hw_data = hardware_worker.collect_hardware_snapshot(fg_app_name)
                
                await asyncio.gather(
                    hardware_worker.write_to_db(pool, hw_data, batch_timestamp),
                    activity_worker.write_batch_to_db(pool, active_procs, batch_timestamp)
                )
                write_telemetry_heartbeat()
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as db_err:
                print(f"[{datetime.datetime.now().strftime('%X')} 🚨 连接断开] 检测到连接异常: {db_err}")
                if pool:
                    # 【重连健壮性修复】PG 重启/掉线时连接已死，await pool.close() 会等待“未释放连接”
                    # 长达 60s+(实测刷屏 "Pool.close() is taking over 60 seconds")，把整个采集主循环卡死、
                    # 停止写库。改为 5s 超时优雅关闭，超时即 terminate() 强制拔线，确保秒级重连自愈。
                    try:
                        await asyncio.wait_for(pool.close(), timeout=5.0)
                    except Exception:
                        try:
                            pool.terminate()
                        except Exception:
                            pass
                    pool = None
                    if hasattr(lifecycle_worker, 'update_pool'):
                        lifecycle_worker.update_pool(None)
            except Exception as loop_err:
                print(f"⚠️ 并发遥测落库异动: {loop_err}")
                
            elapsed = asyncio.get_event_loop().time() - t0
            wall_anchor = time.time()   # 进入节拍休眠前锚定墙钟；下一拍据此识别系统睡眠跨度
            await asyncio.sleep(max(0.01, 3.0 - elapsed))

    except asyncio.CancelledError:
        print("[主控] 捕获终止信号，停机...")
    finally:
        lifecycle_worker.terminate()
        hardware_worker.terminate()
        if pool:
            try:
                await asyncio.wait_for(pool.close(), timeout=5.0)
            except Exception:
                try:
                    pool.terminate()
                except Exception:
                    pass
        print("[主控] 遥测管线释放，闭舱。")


async def main():
    _singleton_mutex = enforce_singleton()
    try:
        await _run_collector()
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(_singleton_mutex)
        except Exception:
            pass

if __name__ == "__main__":
    # 【修复】：只有当作为主程序直接运行时，才安全接管控制台标准流。
    # 这样可以防止单元测试导入 main 时，控制台输出流被强行劫持导致屏幕没有日志
    sys.stdout = safe_logger
    sys.stderr = safe_logger
    
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
