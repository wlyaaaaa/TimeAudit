# -*- coding: utf-8 -*-
"""
TimeAudit 优化与修复回归测试
=============================
覆盖近期对“后端采集 / 数据库调优 / Grafana 前端 / 重连健壮性”的修复，防回归。
直接运行： python test_optimizations.py

覆盖项：
  1. is_not_responding 真·无响应检测 (IsHungAppWindow，端到端起一个真卡死窗口验证命中)
  2. Grafana 数据源连接池封顶 (maxOpenConns ≤ 20，防耗尽 PG 的 max_connections=100)
  3. main.py 重连健壮性 (pool.close() 超时 + terminate() 回退，PG 重启不再卡死采集)
  4. 前端回归 (仪表盘 JSON 合法 / 空 WebShell 宏面板已删)
  5. PostgreSQL 性能调优生效 (shared_buffers/work_mem/effective_cache_size/NVMe planner)
  6. 采集器存活 & is_not_responding 正常落库 (反证未被 pool.close 卡死)
"""
import sys, os, json, glob, time, base64, subprocess, urllib.request, asyncio
import asyncpg

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"
ROOT = os.path.dirname(os.path.abspath(__file__))
GRAFANA = "http://127.0.0.1:53000"

results = []
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def test_hung_detection():
    print("\n[1] is_not_responding 真·无响应检测 (IsHungAppWindow)")
    try:
        from activity_worker import ProcessActivityWorker
    except Exception as e:
        check("导入 ProcessActivityWorker", False, str(e)); return
    code = ("import tkinter,time; r=tkinter.Tk(); r.title('HUNGTEST_TA'); "
            "r.geometry('240x120'); r.update(); time.sleep(40)")
    pyw = sys.executable.replace('python.exe', 'pythonw.exe')
    proc = subprocess.Popen([pyw, '-c', code])
    try:
        time.sleep(9)  # 超过 IsHungAppWindow ~5s 卡死阈值
        hung = ProcessActivityWorker._scan_hung_pids()
        check("_scan_hung_pids 返回集合且不抛异常", isinstance(hung, set))
        check("真·卡死窗口被精准命中", proc.pid in hung,
              f"target pid={proc.pid}, detected={sorted(hung)[:6]}")
    finally:
        try: proc.kill()
        except Exception: pass


def test_grafana_pool():
    print("\n[2] Grafana 数据源连接池封顶")
    try:
        req = urllib.request.Request(GRAFANA + "/api/datasources/uid/bfoc1vymtgni8a")
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:admin").decode())
        with urllib.request.urlopen(req, timeout=10) as r:
            jd = json.loads(r.read().decode("utf-8", "replace")).get("jsonData", {})
        moc = jd.get("maxOpenConns")
        check("maxOpenConns ≤ 20 (防耗尽 PG 连接)", moc is not None and moc <= 20, f"maxOpenConns={moc}")
    except Exception as e:
        check("Grafana 数据源可查询", False, str(e))


def test_main_reconnect_guard():
    print("\n[3] main.py 重连健壮性 (pool.close 超时 + terminate 回退)")
    try:
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    except Exception as e:
        check("读取 main.py", False, str(e)); return
    check("pool.close() 已加超时保护 (asyncio.wait_for)", "wait_for(pool.close()" in src)
    check("超时回退 pool.terminate() 强制拔线", "pool.terminate()" in src)


def test_dashboards():
    print("\n[4] 前端仪表盘回归")
    files = glob.glob(os.path.join(ROOT, "grafana_dashboards", "*.json"))
    bad, webshell = [], False
    for fp in files:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            bad.append(os.path.basename(fp)); continue
        for p in d.get("panels", []):
            if "WebShell" in (p.get("title") or ""):
                webshell = True
    check("所有仪表盘 JSON 合法", (not bad) and len(files) > 0, f"坏文件={bad}" if bad else f"{len(files)} 个全部合法")
    check("空 WebShell/宏面板已删除", not webshell)


async def test_async():
    conn = await asyncpg.connect(DSN)
    try:
        print("\n[5] PostgreSQL 性能调优生效")
        rows = {r['name']: r['setting'] for r in await conn.fetch(
            "SELECT name, setting FROM pg_settings WHERE name IN "
            "('shared_buffers','work_mem','effective_cache_size','random_page_cost','effective_io_concurrency')")}
        sb = int(rows['shared_buffers']) * 8 // 1024          # MB
        wm = int(rows['work_mem']) // 1024                    # MB
        ecs = int(rows['effective_cache_size']) * 8 // 1024 // 1024  # GB
        rpc = float(rows['random_page_cost'])
        eio = int(rows['effective_io_concurrency'])
        check("shared_buffers ≥ 2GB (默认仅 128MB)", sb >= 2048, f"{sb}MB")
        check("work_mem ≥ 16MB (默认仅 4MB)", wm >= 16, f"{wm}MB")
        check("effective_cache_size ≥ 8GB", ecs >= 8, f"{ecs}GB")
        check("random_page_cost ≤ 1.1 (NVMe)", rpc <= 1.1, f"{rpc}")
        check("effective_io_concurrency ≥ 200 (NVMe)", eio >= 200, f"{eio}")

        print("\n[6] 采集器存活 & is_not_responding 落库 (反证未被 pool.close 卡死)")
        row = await conn.fetchrow("""
            SELECT count(*) n,
                   round(EXTRACT(EPOCH FROM (now()-max(timestamp)))::numeric,1) age,
                   count(*) FILTER (WHERE is_not_responding IN (0,1)) valid_flag
            FROM public.fact_process_activity WHERE timestamp > now() - interval '90 seconds'""")
        check("采集器持续写入 (未被 pool.close 卡死)", row['n'] > 0 and row['age'] < 15,
              f"{row['n']}行, 距今{row['age']}s")
        check("is_not_responding 字段正常落库 (0/1)", row['n'] > 0 and row['valid_flag'] == row['n'])
    finally:
        await conn.close()


def main():
    print("=" * 60); print("  TimeAudit 优化与修复回归测试"); print("=" * 60)
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    test_hung_detection()
    test_grafana_pool()
    test_main_reconnect_guard()
    test_dashboards()
    asyncio.run(test_async())
    print("\n" + "=" * 60)
    ok, total = sum(results), len(results)
    print(f"  结果: {ok}/{total} 通过" + ("  🎉 全部通过" if ok == total else f"  ⚠️ {total-ok} 项未过"))
    print("=" * 60)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
