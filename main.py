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
from db_config import local_dsn

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

DB_DSN = local_dsn()
WARMUP_INTERVAL_SEC = 43200
TELEMETRY_INTERVAL_SEC = 1.0
ACTIVITY_INTERVAL_SEC = 3.0
# Partition warmup/retention is maintenance work, not part of the 1 Hz fast
# lane.  A transient DDL/DB failure must therefore retry on a bounded backoff
# instead of re-entering both maintenance paths on every telemetry slot.
WARMUP_RETRY_INITIAL_SEC = 30.0
WARMUP_RETRY_MAX_SEC = 300.0
# The external watchdog first requires a 90-second stale heartbeat and then a
# 45-second resume grace. Stop renewing the aggregate heartbeat well before
# that boundary when an internal lane has made no successful progress.
ACTIVITY_HEALTH_INITIAL_GRACE_SEC = 30.0
ACTIVITY_HEALTH_MAX_AGE_SEC = 30.0
CONTEXT_HEALTH_INITIAL_GRACE_SEC = 15.0
CONTEXT_HEALTH_MAX_AGE_SEC = 15.0
# 【数据保留 / 三年可行性】高频明细 fact_process_activity 约 2GB/周，三年约 312GB；E 盘 2.3TB 余量充足，
# 故运行三年完全可行、且无需中途清理。RETENTION_DAYS 仅作超长期 7x24 运行的"防磁盘爆满"兜底底线：
# 自动 DROP 分区上界早于该天数的周/月子分区，并清理两张非分区表(生命周期事件、AHK 工时)的超期行。
# 默认 1200 天(≈3.3 年) > 3 年，故运行三年内绝不触发任何删除；置 0 可彻底禁用保留清理(永久保留全史)。
RETENTION_DAYS = 1200
# 墙钟跨度超过此值即判定刚从系统睡眠/休眠(S3/S4)唤醒。快速遥测节拍为 1s，故 60s 阈值不会被 GC/DB 抖动误触，
# 而真实睡眠必远超之。用墙钟(time.time())而非 asyncio 的 monotonic 时钟——后者在系统睡眠时会暂停，
# 旧的 monotonic 跳变侦测因此是永不触发的死代码(Bug1)。
SLEEP_RESUME_THRESHOLD_SEC = 60


def _advance_periodic_deadline(previous_deadline, interval, now):
    """Advance an anchored cadence past ``now`` without replaying missed slots."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    if now < previous_deadline:
        return previous_deadline
    elapsed_slots = int((now - previous_deadline) // interval) + 1
    return previous_deadline + elapsed_slots * interval


def _warmup_due(last_success_wall, retry_deadline_wall, now_wall, interval):
    """Return whether maintenance is due without spinning after a failure."""
    if retry_deadline_wall > 0:
        return now_wall >= retry_deadline_wall
    return last_success_wall == 0 or now_wall - last_success_wall >= interval


def _schedule_warmup_retry(now_wall, current_delay, initial_delay, max_delay):
    """Return the next bounded retry deadline and exponential delay."""
    delay = min(max(float(current_delay), float(initial_delay)), float(max_delay))
    next_delay = min(delay * 2.0, float(max_delay))
    return now_wall + delay, next_delay


async def _run_maintenance_cycle(pool, warmup_runner, retention_runner):
    """Run one serialized warmup/retention cycle in the maintenance lane."""
    warmup_ok = False
    try:
        await warmup_runner(pool)
        warmup_ok = True
    except Exception as exc:
        print(f"❌ 警告: 自动分区预热失败, 详情: {exc}")

    # Keep retention in the same single-flight task: it may issue DROP/DELETE
    # and must never overlap a second maintenance cycle after a slow warmup.
    retention_ok = await retention_runner(pool)
    return warmup_ok and retention_ok


def _start_maintenance_task_if_due(
    maintenance_task,
    maintenance_due,
    lane_available,
    task_factory,
):
    """Start at most one slow maintenance task for an eligible slot."""
    if not maintenance_due or maintenance_task is not None or not lane_available:
        return maintenance_task
    return task_factory()


def _reap_finished_maintenance_task(maintenance_task):
    """Consume a finished maintenance task without waiting on a slow one."""
    if maintenance_task is None or not maintenance_task.done():
        return maintenance_task, None, False, False
    try:
        succeeded = bool(maintenance_task.result())
    except asyncio.CancelledError:
        return None, None, False, True
    except Exception as exc:
        return None, exc, False, True
    return None, None, succeeded, True


async def _cancel_and_reap_maintenance_task(maintenance_task):
    """Cancel one maintenance task and consume its terminal state."""
    if maintenance_task is None:
        return
    if not maintenance_task.done():
        maintenance_task.cancel()
    await asyncio.gather(maintenance_task, return_exceptions=True)


def _observe_sleep_resume(
    last_observed_wall,
    on_resume,
    *,
    wall_clock=time.time,
    sleep_threshold=SLEEP_RESUME_THRESHOLD_SEC,
):
    """Advance the wall-clock anchor and synchronously report a suspend-sized gap."""
    observed_wall = wall_clock()
    detected = observed_wall - last_observed_wall > sleep_threshold
    if detected:
        on_resume(last_observed_wall, observed_wall)
    return observed_wall, detected


def _health_lease_is_current(
    started_at,
    last_success_at,
    now,
    initial_grace,
    max_age,
):
    """Return whether a lane has made recent successful progress."""
    anchor = started_at if last_success_at is None else last_success_at
    allowed_age = initial_grace if last_success_at is None else max_age
    return max(0.0, now - anchor) <= allowed_age


def _should_refresh_telemetry_heartbeat(
    hardware_succeeded,
    context_lease_healthy,
    activity_lease_healthy,
):
    """Only advertise whole-pipeline health when every required lane is healthy."""
    return bool(
        hardware_succeeded
        and context_lease_healthy
        and activity_lease_healthy
    )


def _update_health_warning(lane_label, lease_healthy, warning_active):
    """Log one payload-free message per health transition."""
    if lease_healthy:
        if warning_active:
            print(f"✅ [运行健康] {lane_label}健康租约已恢复。")
        return False
    if not warning_active:
        print(f"⚠️ [运行健康] {lane_label}健康租约已过期，暂停总心跳。")
    return True


def _sample_current_foreground_identity(tracker, process_name_resolver):
    """Read the foreground PID/name immediately before hardware sampling."""
    fast_info = tracker.check_foreground_window_fast()
    if not fast_info:
        return None, ""
    hardware_pid = fast_info.get("os_pid")
    if hardware_pid is None:
        return None, ""
    try:
        return hardware_pid, process_name_resolver(hardware_pid)
    except Exception:
        return hardware_pid, ""


async def _collect_and_write_activity_snapshot(
    activity_worker,
    pool,
    sample_timestamp,
    hardware_committed,
    *,
    wall_clock=time.time,
    sleep_threshold=SLEEP_RESUME_THRESHOLD_SEC,
):
    """Run one expensive process snapshot in the non-overlapping slow lane."""
    collection_started_wall = wall_clock()
    active_procs = await asyncio.to_thread(activity_worker.collect_active_processes)
    if wall_clock() - collection_started_wall > sleep_threshold:
        # The native scan crossed a suspend/resume boundary (or became so stale
        # that its delta baselines are no longer meaningful). Never persist that
        # mixed snapshot; the main loop will reset baselines on its next turn.
        return False
    await hardware_committed.wait()
    if wall_clock() - collection_started_wall > sleep_threshold:
        return False
    await activity_worker.write_batch_to_db(
        pool,
        active_procs,
        sample_timestamp,
    )
    return True


def _start_activity_task_if_due(
    activity_task,
    activity_due,
    lane_available,
    task_factory,
):
    """Start one slow-lane task only when its anchored slot is due and free."""
    if not activity_due or activity_task is not None or not lane_available:
        return activity_task
    return task_factory()


def _reap_finished_activity_task(activity_task):
    """Consume a finished task without ever waiting on a running slow lane."""
    if activity_task is None or not activity_task.done():
        return activity_task, None, False
    try:
        activity_succeeded = bool(activity_task.result())
    except asyncio.CancelledError:
        return None, None, False
    except Exception as exc:
        return None, exc, False
    return None, None, activity_succeeded


async def _cancel_and_reap_activity_task(activity_task):
    """Cancel the single slow-lane task and consume its terminal state."""
    if activity_task is None:
        return
    if not activity_task.done():
        activity_task.cancel()
    await asyncio.gather(activity_task, return_exceptions=True)

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
        return True
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
        return True
    except Exception as e:
        print(f"[保留策略] 清理跳过(非致命): {e}")
        return False

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

    warmup_last_success_wall = 0.0
    warmup_retry_deadline_wall = 0.0
    warmup_retry_delay = WARMUP_RETRY_INITIAL_SEC
    maintenance_task = None
    maintenance_resume_reset_pending = False
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

    wall_anchor = time.time()   # 最近一次墙钟观测；每个潜在长等待后复核，避免漏掉本轮内睡眠
    loop = asyncio.get_running_loop()
    next_telemetry_deadline = loop.time()
    next_activity_deadline = next_telemetry_deadline
    activity_task = None
    activity_resume_reset_pending = False
    health_started_at = loop.time()
    activity_last_success_at = None
    context_last_success_at = None
    activity_health_warning_active = False
    context_health_warning_active = False

    def _handle_sleep_resume(pre_sleep_wall, observed_wall):
        nonlocal activity_task
        nonlocal activity_resume_reset_pending
        nonlocal health_started_at
        nonlocal activity_last_success_at
        nonlocal context_last_success_at
        nonlocal warmup_last_success_wall
        nonlocal warmup_retry_deadline_wall
        nonlocal warmup_retry_delay
        nonlocal maintenance_task
        nonlocal maintenance_resume_reset_pending

        gap = observed_wall - pre_sleep_wall
        pre_sleep_dt = datetime.datetime.fromtimestamp(
            pre_sleep_wall,
            datetime.timezone.utc,
        )
        print(
            f"[{datetime.datetime.now().strftime('%X')} 🔄 唤醒自愈] "
            f"墙钟跳变 {gap:.0f}s，判定系统从睡眠/休眠唤醒，正在截断会话并重置速率基线..."
        )
        if activity_task is not None and not activity_task.done():
            # asyncio.to_thread itself is not cancellable. Cancel the coroutine
            # immediately; the collection-state lock prevents replacement work
            # from racing its residual native thread.
            activity_task.cancel()
        if maintenance_task is not None and not maintenance_task.done():
            # Maintenance uses asyncpg awaits, so cancellation is cooperative;
            # the next slot reaps it before allowing a replacement cycle.
            maintenance_task.cancel()
        maintenance_resume_reset_pending = True
        health_started_at = loop.time()
        activity_last_success_at = None
        context_last_success_at = None
        activity_resume_reset_pending = True
        try:
            tracker.mark_sleep_boundary(pre_sleep_dt)
        except Exception as resume_err:
            print(f"[唤醒自愈] 前台会话截断失败: {resume_err}")
        try:
            hardware_worker.last_ctx_switches = None
        except Exception as resume_err:
            print(f"[唤醒自愈] 硬件速率基线重置失败: {resume_err}")
        # Sleep can cross a natural partition boundary. Force the next slot to
        # re-check partitions without delaying this recovery path.
        warmup_last_success_wall = 0.0
        warmup_retry_deadline_wall = 0.0
        warmup_retry_delay = WARMUP_RETRY_INITIAL_SEC

    try:
        while True:
            now_monotonic = loop.time()
            if now_monotonic < next_telemetry_deadline:
                await asyncio.sleep(next_telemetry_deadline - now_monotonic)

            slot_started = loop.time()
            activity_due = slot_started >= next_activity_deadline
            resume_detected_this_slot = False
            wall_anchor, detected_at_slot_start = _observe_sleep_resume(
                wall_anchor,
                _handle_sleep_resume,
            )
            resume_detected_this_slot |= detected_at_slot_start
            now_wall = wall_anchor

            # 【Bug2 修复】分区预热按"墙钟"调度：旧版用睡眠中暂停的 monotonic，会把 12h 真实间隔拖成
            # 12 天纯活跃时长，极可能跨过自然周/月物理边界 → PostgreSQL "no partition of relation found"
            # 丢数。预热建"当期+下一档"，墙钟 12h 复检即可长期保证当期与下一档始终就绪。
            # Maintenance runs as a single-flight background task.  A slow DDL,
            # retention DELETE, or a bounded retry must never occupy this 1 Hz
            # collector slot or create a backlog of overlapping maintenance jobs.
            (
                maintenance_task,
                maintenance_error,
                maintenance_succeeded,
                maintenance_finished,
            ) = _reap_finished_maintenance_task(maintenance_task)
            if maintenance_resume_reset_pending and maintenance_task is None:
                # A task that completed concurrently with resume is discarded;
                # its pre-sleep result must not renew the post-resume schedule.
                maintenance_resume_reset_pending = False
                maintenance_error = None
                maintenance_succeeded = False
                maintenance_finished = False

            if maintenance_finished and maintenance_error is not None:
                print(f"⚠️ 维护车道异常，进入退避重试: {maintenance_error}")
                maintenance_succeeded = False
            if maintenance_finished:
                if maintenance_succeeded:
                    warmup_last_success_wall = now_wall
                    warmup_retry_deadline_wall = 0.0
                    warmup_retry_delay = WARMUP_RETRY_INITIAL_SEC
                elif not maintenance_resume_reset_pending:
                    (
                        warmup_retry_deadline_wall,
                        warmup_retry_delay,
                    ) = _schedule_warmup_retry(
                        now_wall,
                        warmup_retry_delay,
                        WARMUP_RETRY_INITIAL_SEC,
                        WARMUP_RETRY_MAX_SEC,
                    )

            if (
                maintenance_task is None
                and not maintenance_resume_reset_pending
                and pool is not None
                and _warmup_due(
                    warmup_last_success_wall,
                    warmup_retry_deadline_wall,
                    now_wall,
                    WARMUP_INTERVAL_SEC,
                )
            ):
                maintenance_task = _start_maintenance_task_if_due(
                    maintenance_task,
                    True,
                    True,
                    lambda: asyncio.create_task(
                        _run_maintenance_cycle(
                            pool,
                            auto_warmup_partitions,
                            auto_retention_cleanup,
                        )
                    ),
                )

            if maintenance_finished:
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
                    wall_anchor, detected_after_reconnect = _observe_sleep_resume(
                        wall_anchor,
                        _handle_sleep_resume,
                    )
                    resume_detected_this_slot |= detected_after_reconnect
                    await asyncio.sleep(5)
                    continue

            context_poll_timestamp = datetime.datetime.now(datetime.timezone.utc)
            context_poll_healthy = False
            try:
                context_poll_healthy = bool(
                    await tracker.poll_heartbeat(pool, context_poll_timestamp)
                )
            except Exception:
                print("⚠️ 切窗监测工作异常；等待健康租约复核。")
            if context_poll_healthy:
                context_last_success_at = loop.time()

            try:
                (
                    activity_task,
                    activity_error,
                    activity_succeeded,
                ) = _reap_finished_activity_task(activity_task)
                if activity_succeeded:
                    activity_last_success_at = loop.time()
                collection_state_idle = activity_worker.is_collection_state_idle()
                if (
                    activity_resume_reset_pending
                    and activity_task is None
                    and collection_state_idle
                ):
                    activity_worker.reset_on_resume()
                    activity_resume_reset_pending = False

                # Context persistence can await metadata/DB work. Re-read focus
                # immediately before the hardware/FPS sample so an Alt-Tab during
                # that await cannot leave PresentMon bound to the previous PID.
                hardware_pid, fg_app_name = _sample_current_foreground_identity(
                    tracker,
                    lambda pid: psutil.Process(pid).name(),
                )
                sample_timestamp = datetime.datetime.now(
                    datetime.timezone.utc
                )
                hw_data = hardware_worker.collect_hardware_snapshot(
                    fg_app_name,
                    hardware_pid,
                )

                # Start the 1-second hardware/FPS write immediately. On every third
                # slot it progresses concurrently with the expensive process scan, so
                # that slow process metadata cannot unnecessarily delay fresh display
                # telemetry reaching the database.
                hardware_write_task = asyncio.create_task(
                    hardware_worker.write_to_db(
                        pool,
                        hw_data,
                        sample_timestamp,
                    )
                )
                hardware_committed = asyncio.Event()
                if activity_error is None and not activity_resume_reset_pending:
                    activity_task = _start_activity_task_if_due(
                        activity_task,
                        activity_due,
                        activity_worker.is_collection_state_idle(),
                        lambda: asyncio.create_task(
                            _collect_and_write_activity_snapshot(
                                activity_worker,
                                pool,
                                sample_timestamp,
                                hardware_committed,
                            )
                        ),
                    )
                hardware_succeeded = False
                try:
                    await hardware_write_task
                    hardware_succeeded = True
                    wall_anchor, detected_after_hardware = _observe_sleep_resume(
                        wall_anchor,
                        _handle_sleep_resume,
                    )
                    resume_detected_this_slot |= detected_after_hardware
                    if not detected_after_hardware:
                        hardware_committed.set()
                finally:
                    if not hardware_write_task.done():
                        hardware_write_task.cancel()
                    await asyncio.gather(
                        hardware_write_task,
                        return_exceptions=True,
                    )

                (
                    activity_task,
                    completed_activity_error,
                    completed_activity_succeeded,
                ) = _reap_finished_activity_task(activity_task)
                if completed_activity_succeeded:
                    activity_last_success_at = loop.time()
                if activity_error is None:
                    activity_error = completed_activity_error

                health_now = loop.time()
                context_lease_healthy = _health_lease_is_current(
                    health_started_at,
                    context_last_success_at,
                    health_now,
                    CONTEXT_HEALTH_INITIAL_GRACE_SEC,
                    CONTEXT_HEALTH_MAX_AGE_SEC,
                )
                activity_lease_healthy = _health_lease_is_current(
                    health_started_at,
                    activity_last_success_at,
                    health_now,
                    ACTIVITY_HEALTH_INITIAL_GRACE_SEC,
                    ACTIVITY_HEALTH_MAX_AGE_SEC,
                )
                context_health_warning_active = _update_health_warning(
                    "上下文车道",
                    context_lease_healthy,
                    context_health_warning_active,
                )
                activity_health_warning_active = _update_health_warning(
                    "活动慢车道",
                    activity_lease_healthy,
                    activity_health_warning_active,
                )
                if (
                    not resume_detected_this_slot
                    and _should_refresh_telemetry_heartbeat(
                        hardware_succeeded,
                        context_lease_healthy,
                        activity_lease_healthy,
                    )
                ):
                    write_telemetry_heartbeat()

                if activity_error is not None:
                    raise activity_error
            except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as db_err:
                print(f"[{datetime.datetime.now().strftime('%X')} 🚨 连接断开] 检测到连接异常: {db_err}")
                await _cancel_and_reap_activity_task(activity_task)
                activity_task = None
                await _cancel_and_reap_maintenance_task(maintenance_task)
                maintenance_task = None
                warmup_last_success_wall = 0.0
                warmup_retry_deadline_wall = now_wall + WARMUP_RETRY_INITIAL_SEC
                warmup_retry_delay = WARMUP_RETRY_INITIAL_SEC
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
                
            finished = loop.time()
            next_telemetry_deadline = _advance_periodic_deadline(
                next_telemetry_deadline,
                TELEMETRY_INTERVAL_SEC,
                finished,
            )
            if activity_due:
                next_activity_deadline = _advance_periodic_deadline(
                    next_activity_deadline,
                    ACTIVITY_INTERVAL_SEC,
                    finished,
                )
            wall_anchor, detected_at_slot_end = _observe_sleep_resume(
                wall_anchor,
                _handle_sleep_resume,
            )
            resume_detected_this_slot |= detected_at_slot_end
            await asyncio.sleep(max(0.0, next_telemetry_deadline - loop.time()))

    except asyncio.CancelledError:
        print("[主控] 捕获终止信号，停机...")
    finally:
        await _cancel_and_reap_activity_task(activity_task)
        await _cancel_and_reap_maintenance_task(maintenance_task)
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
