"""Small payload-free project facts, exported by the existing diagnostic producer."""
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from urllib.parse import quote

from personal_activity_reader import parse_utc, seconds

ROOT = Path(__file__).resolve().parent
ZONE = dt.timezone(dt.timedelta(hours=8))
GIT = shutil.which("git.exe") or r"C:\Program Files\Git\cmd\git.exe"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def query(sql, timeout=12):
    docker = shutil.which("docker.exe") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    result = subprocess.run([docker, "exec", "-i", "-e",
        f"PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout={timeout*1000} -c lock_timeout=500 -c timezone=Asia/Shanghai",
        "audit-postgres", "psql", "-XqAt", "-U", "leyang", "-d", "time_audit", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True, encoding="utf-8", timeout=timeout+5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def daily_records(rows, until):
    midnight = until.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for offset in range(6, -1, -1):
        start = midnight-dt.timedelta(days=offset)
        end = min(start+dt.timedelta(days=1), until)
        parts = [(max(parse_utc(r[0]), start), min(parse_utc(r[1]), end), r[2]) for r in rows
                 if parse_utc(r[0]) < end and parse_utc(r[1]) > start]
        uncovered = (end-start).total_seconds()-seconds([(s, e) for s, e, _ in parts])
        missed = round(uncovered+seconds([(s, e) for s, e, gap in parts if gap]), 6)
        result.append(dict(date=start.date().isoformat(), intervals=len(parts), missed_seconds=missed,
            text=f"{start:%m-%d}：{len(parts)} 段，没记上 {missed/60:.1f} 分钟",
            state_text="截至取数时刻" if offset == 0 else "已保存日记录"))
    return result


def dashboard_records():
    def panels(items):
        return sum((p.get("type") != "row")+panels(p.get("panels", [])) for p in items)
    records, artifacts = [], []
    for path in sorted((ROOT/"grafana_dashboards").glob("*.json")):
        value = read(path)
        if value["uid"].startswith("website-computer-status"):
            continue
        count = sum(p.get("kind") in {"Panel", "LibraryPanel"} for p in value["elements"].values()) if "elements" in value else panels(value.get("panels", []))
        changed = subprocess.check_output([GIT, "-C", str(ROOT), "log", "-1", "--format=%cI", "--", str(path)], text=True).strip()
        records.append(dict(text=value["title"], uid=value["uid"], panels=count, changed=changed, state_text="已有看板定义"))
        artifacts.append(dict(at=changed, kind="dashboard", title="看板定义 JSON · "+value["title"], status="completed", public="same",
            open=dict(source="timeaudit.dashboard."+value["uid"], href="https://github.com/wlyaaaaa/TimeAudit/blob/main/"+quote(path.relative_to(ROOT).as_posix()))))
    return records, artifacts


def source_size():
    files = subprocess.check_output([GIT, "-C", str(ROOT), "ls-files"], text=True).splitlines()
    docs = [ROOT/p for p in files if Path(p).suffix.lower() == ".md"]
    code = [ROOT/p for p in files if Path(p).suffix.lower() in {".py", ".pyw", ".ps1", ".ahk", ".vbs", ".bat", ".yml", ".sql"}]
    return dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), docs_chars=sum(sum(not c.isspace() for c in p.read_text("utf-8-sig")) for p in docs),
        code_lines=sum(len(p.read_text("utf-8-sig").splitlines()) for p in code))


def export_snapshot(aggregate, after, until, output, *, static=False, config_path=None):
    config = read(config_path or ROOT/"live.local.json")
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    snapshot = dict(v=1, at=now(), scope="现有留存记录；不同单位不能相加", metrics={}, times={}, scopes={}, artifacts=[])
    def put(values, scope=None, at=None):
        for key, value in values.items():
            snapshot["metrics"][key] = value
            snapshot["times"][key] = (at or now()) if value is not None else None
            snapshot["scopes"][key] = scope or (snapshot["scope"] if value is not None else "源数据没有给出这个值")
    def attempt(keys, action):
        try:
            action()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, sqlite3.Error) as exc:
            put(dict.fromkeys(keys.split()), "取数失败："+type(exc).__name__+"；未填零")
    if static:
        for key, table in (("hardware", "fact_system_hardware"), ("lifecycle", "fact_process_lifecycle_events"), ("registry", "dim_process_registry")):
            attempt(key, lambda key=key, table=table: put({key: query(f"SELECT count(*) FROM public.{table};", 120)[0]}))
        def earliest():
            row = query("SELECT json_build_array((SELECT to_char(min(start_time) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') FROM public.app_usage_logs),(SELECT to_char(min(timestamp) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') FROM public.fact_process_context WHERE is_foreground=1));")[0]
            put(dict(earliest=f"活动 {row[0]}；窗口 {row[1]}" if all(row) else None))
        attempt("earliest", earliest)
        write(config["retained_output"], snapshot)
        snapshot.update(metrics={}, times={}, scopes={})
        snapshot["size"] = source_size()
        previous = read(output) if Path(output).exists() else {}
        if previous.get("metrics") == snapshot["metrics"] and previous.get("scopes") == snapshot["scopes"] and all(
                previous.get("size", {}).get(k) == snapshot["size"][k] for k in ("docs_chars", "code_lines")):
            snapshot = previous
    else:
        window = f"{after.astimezone(ZONE).isoformat()} 至 {until.astimezone(ZONE).isoformat()}；采样空段可能含睡眠、关机或缺口"
        missed = aggregate["collection_gap_seconds"]+aggregate["uncovered_seconds"]
        put(dict(missed=dict(text=f"{missed/60:.1f} 分钟（明确缺口 {aggregate['collection_gap_seconds']} 秒＋完全没记录 {aggregate['uncovered_seconds']} 秒）", seconds=missed),
            memory=aggregate.get("main_working_set_mib_max"), samples24=aggregate["hardware_sample_count"],
            covered24=aggregate["distinct_sample_seconds"], empty24=aggregate["requested_window_seconds"]-aggregate["distinct_sample_seconds"],
            max_gap=aggregate.get("max_gap_seconds")), window, until.isoformat())
        def activity():
            row = query("SELECT json_build_array((SELECT count(*) FROM public.fact_process_context),(SELECT count(*) FROM public.app_usage_logs),(SELECT max(start_time) FROM public.app_usage_logs));")[0]
            put(dict(context=row[0], activity=row[1], latest=parse_utc(row[2]).astimezone(ZONE).isoformat() if row[2] else None))
        attempt("context activity latest", activity)
        attempt("db_bytes detail_bytes summary_bytes", lambda: put(dict(zip(("db_bytes", "detail_bytes", "summary_bytes"), query("SELECT json_build_array(pg_database_size(current_database()),(SELECT sum(pg_total_relation_size(relid)) FROM pg_partition_tree('public.fact_process_activity')),pg_total_relation_size('public.activity_app_hour')+pg_total_relation_size('public.activity_app_day')+pg_total_relation_size('public.activity_system_hour')+pg_total_relation_size('public.activity_system_day'));")[0]))))
        attempt("raw_since", lambda: put(dict(raw_since=query("SELECT to_json(to_char(raw_since AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD')) FROM public.activity_retention_state WHERE singleton;")[0]), "此日起保留原始明细，更早仍有小时／日统计"))
        def days():
            start = until.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)-dt.timedelta(days=6)
            rows = query(f"SELECT json_build_array(start_time,start_time+duration_seconds*interval '1 second',process_name='System_CollectionGap') FROM public.app_usage_logs WHERE duration_seconds>0 AND start_time<'{until.isoformat()}'::timestamptz AND start_time+duration_seconds*interval '1 second'>'{start.isoformat()}'::timestamptz;")
            put(dict(days=daily_records(rows, until)), "北京时间最近七日；跨日区间裁剪并求并集；段数为与当天相交的记录数，跨日会重复；今天截至取数时刻")
        attempt("days", days)
        owner_values = dict(clipboard_latest=None, viewer=None)
        def clipboard():
            database = Path(config["clipboard_db"])
            with sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=1) as conn:
                conn.execute("PRAGMA query_only=ON")
                row = conn.execute("SELECT count(*),sum(event_kind='observation' AND blob_id IS NOT NULL),sum(event_kind='observation' AND payload_type='text'),sum(event_kind='observation' AND payload_type='url'),sum(event_kind='observation' AND payload_type='file_paths'),min(observed_at_utc),max(observed_at_utc),sum(event_kind='skip'),sum(event_kind='gap'),sum(event_kind='boundary') FROM events").fetchone()
            put(dict(clipboard=dict(text=f"{row[1] or 0} 条（文字 {row[2] or 0}、链接 {row[3] or 0}、文件列表 {row[4] or 0}），最早 {parse_utc(row[5]).astimezone(ZONE):%Y-%m-%d}" if row[5] else "0 条", count=row[1] or 0),
                clipboard_events=dict(text=f"{row[0]} 条（含跳过 {row[7] or 0}、缺口 {row[8] or 0}、边界 {row[9] or 0}）", count=row[0]),
                clipboard_bytes=sum(p.stat().st_size for p in (database, Path(str(database)+"-wal"), Path(str(database)+"-shm")) if p.exists())))
            temporary = list(database.parent.glob("*.tmp"))
            put(dict(clipboard_temporary=dict(text=f"{len(temporary)} 个，{sum(p.stat().st_size for p in temporary)} 字节")))
            owner_values["clipboard_latest"] = parse_utc(row[6]).astimezone(ZONE).isoformat() if row[6] else None
        attempt("clipboard clipboard_events clipboard_bytes clipboard_temporary", clipboard)
        def dashboards():
            records, artifacts = dashboard_records()
            snapshot["artifacts"] = artifacts
            snapshot["dashboard_records"] = records
            put(dict(dashboards=dict(text=f"{len(records)} 份看板、{sum(r['panels'] for r in records)} 张图"), website_dashboards=len(list((ROOT/"grafana_provisioning/dashboards").glob("website-computer-status*.json")))))
        attempt("dashboards website_dashboards", dashboards)
        owner = dict(snapshot, metrics=dict(snapshot["metrics"], **owner_values), times=dict(snapshot["times"]), scopes=dict(snapshot["scopes"]))
        owner["times"].update({key: now() if value is not None else None for key, value in owner_values.items()})
        owner["scopes"]["viewer"] = "本机开始菜单 TimeAudit 剪贴板历史可打开；共享网页尚无受支持的本机启动入口，未提供假链接"
        owner["dashboard_open"] = [dict(r, href=config["grafana_url"].rstrip("/")+"/d/"+r["uid"]) for r in snapshot.get("dashboard_records", [])]
        owner["metrics"]["dashboard_open"] = owner["dashboard_open"]
        owner["times"]["dashboard_open"] = now()
        write(config["owner_output"], owner)
    write(config["static_output" if static else "live_output"], snapshot)
    write(output, snapshot)
