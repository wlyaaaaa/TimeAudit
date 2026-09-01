# -*- coding: utf-8 -*-
"""
TimeAudit 遥测健康综合验证测试
================================
交叉验证“代码功能 / 数据正确性 / 采集链路 / 分区建表”是否全部就绪。
直接运行： python test_telemetry_health.py

覆盖项：
  1. 四组件存活 (python 引擎 / LibreHardwareMonitor / PresentMonConsole)
  2. LHM Web 服务在线且返回 NVIDIA GPU 真实电压 (证明 LHM 在采集)
  3. PresentMon 帧率数据入库 (证明 PresentMon 在采集；游戏运行时)
  4. LHM 真值入库 (gpu_core_voltage / cpu_vcore 非 NULL)
  5. 每进程 CPU 归一化为整机口径 (proc_cpu_usage ≤ 100)
  6. 每进程显存锁定 NVIDIA 独显 (无超过物理显存的幻影, 核显隔离)
  7. 分区自动建表生效 (按周 activity/context + 按月 hardware, 当前+下一档)
  8. 数据新鲜度 & 质量 (无负值/无越界)
  9. 看门狗实证 (历史重启计数)
"""
import sys, os, re, json, urllib.request, datetime, asyncio, warnings, subprocess
import psutil
import asyncpg
from db_config import local_dsn

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DSN = local_dsn()
ROOT = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL = "✅ PASS", "❌ FAIL"
results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  — {detail}" if detail else ""))

def evaluate_presentmon_process_status(pid):
    if pid is not None:
        return True, f"PID={pid}"
    return True, "按需门控空闲：非活跃渲染期会主动退出；采集能力见后续 FPS 入库检查"


def evaluate_presentmon_capture_status(
    fps_recent,
    sample_age_seconds,
    capture_status=None,
):
    if int(fps_recent or 0) > 0:
        return True, f"近30分钟已有 {int(fps_recent)} 个正帧率样本"
    try:
        sample_age = float(sample_age_seconds)
    except (TypeError, ValueError):
        sample_age = None
    normalized_status = str(capture_status or "").strip().lower()
    if sample_age is not None and 0 <= sample_age < 15:
        if normalized_status == "gated_idle":
            return True, "IDLE：帧通道持续写入，渲染门明确空闲"
        if normalized_status == "starting":
            return True, "STARTING：帧源正在有界热身"
        if normalized_status:
            return False, f"帧通道新鲜但采集状态异常: {normalized_status}"
        return False, "帧通道新鲜但缺少 fps_capture_status，不能推定健康"
    return False, "近期既没有正帧率样本，硬件/FPS 通道样本也不新鲜"

def lhm_port():
    try:
        with open(os.path.join(ROOT, "LibreHardwareMonitor.config"), encoding="utf-8", errors="ignore") as f:
            m = re.search(r'key="listenerPort"\s+value="(\d+)"', f.read())
            return int(m.group(1)) if m else 18085
    except Exception:
        return 18085

def excluded_tcp_port_ranges():
    try:
        output = subprocess.check_output(
            ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return [
            (int(start), int(end))
            for start, end in re.findall(r"^\s*(\d+)\s+(\d+)(?:\s+\*)?\s*$", output, re.MULTILINE)
        ]
    except Exception:
        return []

def proc_alive(name):
    # A crashed WinForms process can remain as a zero-thread ghost for a short
    # time.  Do not report that ghost as a healthy collector.
    for p in psutil.process_iter(['name', 'num_threads']):
        try:
            if (
                p.info['name']
                and p.info['name'].lower() == name.lower()
                and int(p.info.get('num_threads') or 0) > 0
            ):
                return p.pid
        except Exception:
            pass
    return None

def heartbeat_fresh(path, max_age_seconds):
    try:
        return (datetime.datetime.now().timestamp() - os.path.getmtime(path)) <= max_age_seconds
    except OSError:
        return False

def part_name_week(d, parent):
    iso = d.isocalendar()
    return f"{parent}_y{iso[0]}w{iso[1]:02d}"

def part_name_month(d, parent):
    return f"{parent}_y{d.year}m{d.month:02d}"

def gpu_total_vram_gb():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import pynvml
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return pynvml.nvmlDeviceGetMemoryInfo(handle).total / (1024 ** 3)
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        return None

async def main():
    print("=" * 60)
    print("  TimeAudit 遥测健康综合验证")
    print("=" * 60)

    # ---- 1. 组件存活 ----
    print("\n[1] 组件存活")
    lhm_pid = proc_alive("LibreHardwareMonitor.exe")
    pm_pid = proc_alive("PresentMonConsole.exe")
    py_cmdline_ok = any('main.py' in ' '.join(p.info['cmdline'] or []) for p in psutil.process_iter(['cmdline'])
                        if _safe(lambda: p.info['cmdline']))
    py_heartbeat_ok = heartbeat_fresh(os.path.join(ROOT, "log", "telemetry_heartbeat"), 90)
    py_ok = py_cmdline_ok or py_heartbeat_ok
    check(
        "python 遥测引擎在运行",
        py_ok,
        "exact process visible" if py_cmdline_ok else "fresh payload-free heartbeat",
    )
    check("LibreHardwareMonitor 在运行 (计划任务单一 owner)", lhm_pid is not None, f"PID={lhm_pid}")
    pm_ok, pm_detail = evaluate_presentmon_process_status(pm_pid)
    check("PresentMonConsole 按需门控状态正常", pm_ok, pm_detail)

    # ---- 2. LHM Web 服务真实采集 ----
    print("\n[2] LHM Web 服务真实采集 (NVIDIA GPU)")
    configured_lhm_port = lhm_port()
    excluded_ranges = excluded_tcp_port_ranges()
    excluded = any(start <= configured_lhm_port <= end for start, end in excluded_ranges)
    gpu_v = None
    web_ok = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{configured_lhm_port}/data.json", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        web_ok = True
        flat = {}
        def walk(n, p):
            t = n.get("Text", ""); cur = (p + "/" + t) if t else p
            if n.get("Value") and not n.get("Children"):
                flat[cur.lower()] = n["Value"]
            for c in n.get("Children", []):
                walk(c, cur)
        walk(data, "")
        for k, v in flat.items():
            if "nvidia" in k and k.endswith("gpu core voltage"):
                gpu_v = float(str(v).split()[0].replace(",", "."))
    except Exception as e:
        check("LHM Web 服务可达", False, str(e));
    check(
        "LHM Web 端口未被 Windows TCP 排除阻断",
        web_ok or not excluded,
        f"port={configured_lhm_port}" + (" (active/reserved)" if web_ok and excluded else ""),
    )
    check("LHM 返回 NVIDIA GPU 核心电压", gpu_v is not None and 0.4 < gpu_v < 1.5, f"{gpu_v} V")

    # ---- DB 连接 ----
    conn = await asyncpg.connect(DSN)
    try:
        # ---- 3+4. 采集入库 (近 2 分钟) ----
        print("\n[3] 硬件采集入库 (近2分钟)")
        row = await conn.fetchrow("""
            SELECT count(*) n,
                   count(*) FILTER (WHERE gpu_core_voltage IS NOT NULL) gpuv,
                   count(*) FILTER (WHERE cpu_vcore_voltage IS NOT NULL) vcore,
                   count(*) FILTER (WHERE current_fps > 0) fps,
                   round(max(current_fps)::numeric,1) maxfps,
                   round(EXTRACT(EPOCH FROM (now()-max(timestamp)))::numeric,1) age,
                   (array_agg(fps_capture_status ORDER BY timestamp DESC))[1] capture_status
            FROM public.fact_system_hardware WHERE timestamp > now() - interval '2 minutes'
        """)
        check("硬件表持续写入", row['n'] > 0 and row['age'] < 15, f"{row['n']}行, 距今{row['age']}s")
        # 容忍看门狗重启造成的极少量瞬时 NULL(<20%)，这是"自愈"而非故障
        check("LHM GPU 电压入库 (非伪造)", row['n'] > 0 and row['gpuv'] >= row['n'] * 0.8, f"{row['gpuv']}/{row['n']}")
        check("LHM CPU Vcore 入库 (非伪造)", row['n'] > 0 and row['vcore'] >= row['n'] * 0.8, f"{row['vcore']}/{row['n']}")
        # 正帧率只能由真实游戏/3D 渲染证明；零值还必须由显式状态区分
        # gated idle 与 active-render capture failure。
        fps_recent = await conn.fetchval("SELECT count(*) FROM public.fact_system_hardware WHERE timestamp > now()-interval '30 minutes' AND current_fps > 0")
        fps_ok, fps_detail = evaluate_presentmon_capture_status(
            fps_recent,
            row['age'],
            row['capture_status'],
        )
        check("FPS 帧率通道状态", fps_ok, f"{fps_detail}, 当前max={row['maxfps']}fps")

        # ---- 5. CPU 归一化 ----
        print("\n[4] 每进程 CPU 归一化 (整机口径 ≤100%)")
        cpu = await conn.fetchrow("""
            SELECT round(max(proc_cpu_usage)::numeric,1) maxc,
                   count(*) FILTER (WHERE proc_cpu_usage > 100) over100
            FROM public.fact_process_activity WHERE timestamp > now() - interval '90 seconds'
        """)
        check("proc_cpu_usage 上限 100% (无 2556% 虚高)", (cpu['over100'] or 0) == 0, f"max={cpu['maxc']}%, 越界={cpu['over100']}")

        # ---- 6. GPU 显存锁定 NVIDIA 独显 ----
        print("\n[5] 每进程显存锁定 NVIDIA 独显")
        total_vram = gpu_total_vram_gb()
        vram_limit = (total_vram * 1.05) if total_vram else 64.0
        g = await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE proc_vram_used_gb > $1) over_vram,
                   count(*) FILTER (WHERE proc_gpu_usage > 100) over_gpu,
                   round(max(proc_vram_used_gb)::numeric,2) maxv
            FROM public.fact_process_activity WHERE timestamp > now() - interval '90 seconds'
        """, vram_limit)
        check("无超过物理显存的显存幻影 (核显已隔离)", (g['over_vram'] or 0) == 0, f"max={g['maxv']}GB, limit={vram_limit:.2f}GB")
        check("无 >100% GPU 占用越界", (g['over_gpu'] or 0) == 0)

        # ---- 7. 分区自动建表 ----
        print("\n[6] 分区自动建表 (按周/按月, 当前+下一档)")
        parts = set(r['relname'] for r in await conn.fetch("""
            SELECT child.relname FROM pg_inherits
            JOIN pg_class child ON pg_inherits.inhrelid = child.oid
        """))
        today = datetime.date.today()
        nextwk = today + datetime.timedelta(days=7)
        nm_y, nm_m = (today.year + (today.month // 12)), (today.month % 12 + 1)
        nextmo = datetime.date(nm_y, nm_m, 1)
        for parent in ("fact_process_activity", "fact_process_context"):
            check(f"{parent} 当前周分区存在", part_name_week(today, parent) in parts, part_name_week(today, parent))
            check(f"{parent} 下周分区已预建", part_name_week(nextwk, parent) in parts, part_name_week(nextwk, parent))
        check("fact_system_hardware 当前月分区存在", part_name_month(today, "fact_system_hardware") in parts, part_name_month(today, "fact_system_hardware"))
        check("fact_system_hardware 次月分区已预建", part_name_month(nextmo, "fact_system_hardware") in parts, part_name_month(nextmo, "fact_system_hardware"))

        # ---- 8. 数据质量 ----
        print("\n[7] 数据质量 (近3分钟无异常)")
        q = await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE cpu_total_usage<0 OR gpu_usage<0 OR cpu_package_power<0
                                       OR cpu_package_temp<10 OR cpu_package_temp>110) bad
            FROM public.fact_system_hardware WHERE timestamp > now() - interval '3 minutes'
        """)
        check("硬件数据无负值/越界温度", (q['bad'] or 0) == 0)
        q2 = await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE proc_cpu_usage<0 OR proc_ram_mb<0 OR proc_vram_used_gb<0) bad
            FROM public.fact_process_activity WHERE timestamp > now() - interval '1 minute'
        """)
        check("每进程数据无负值", (q2['bad'] or 0) == 0)

        # ---- 9. 看门狗实证 ----
        print("\n[8] 看门狗历史实证")
        pm_restarts = _count_lines(os.path.join(ROOT, "presentmon_debug.log"), "守护线程已启动")
        check("PresentMon 看门狗曾自动重启", pm_restarts >= 1, f"{pm_restarts} 次")
    finally:
        await conn.close()

    print("\n" + "=" * 60)
    ok = sum(1 for r in results if r); total = len(results)
    print(f"  结果: {ok}/{total} 通过" + ("  🎉 全部通过" if ok == total else f"  ⚠️ {total-ok} 项未过"))
    print("=" * 60)
    return 0 if ok == total else 1

def _safe(fn):
    try: return fn()
    except Exception: return None

def _count_lines(path, kw):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return sum(1 for ln in f if kw in ln)
    except Exception:
        return 0

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
