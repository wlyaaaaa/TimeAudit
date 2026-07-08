# -*- coding: utf-8 -*-
import sys
import os
import json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DASHBOARD_DIR = r"E:\Projects\Tools\TimeAudit\grafana_dashboards"

def optimize_sql(sql):
    # 替换所有的物理换行，确保只用 \n，方便匹配
    sql = sql.replace('\r\n', '\n')

    # 1. 🚀 前台交互与流畅度诊断舱 优化
    sql = sql.replace(
        "ctx.timestamp >= b.t_from - interval '1 day' AND ctx.timestamp <= b.t_to AND COALESCE(ctx.end_timestamp, now()) >= b.t_from",
        "ctx.timestamp >= $__timeFrom()::timestamptz - interval '1 day' AND ctx.timestamp <= $__timeTo()::timestamptz AND COALESCE(ctx.end_timestamp, now()) >= $__timeFrom()::timestamptz"
    )
    sql = sql.replace(
        "a.start_time >= b.t_from - interval '1 day' AND a.start_time <= b.t_to AND a.start_time + (a.duration_seconds || ' seconds')::interval >= b.t_from",
        "a.start_time >= $__timeFrom()::timestamptz - interval '1 day' AND a.start_time <= $__timeTo()::timestamptz AND a.start_time + (a.duration_seconds || ' seconds')::interval >= $__timeFrom()::timestamptz"
    )
    sql = sql.replace(
        "hw.timestamp >= b.t_from AND hw.timestamp <= b.t_to",
        "hw.timestamp >= $__timeFrom()::timestamptz AND hw.timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "hw.timestamp >= t_from AND hw.timestamp <= t_to",
        "hw.timestamp >= $__timeFrom()::timestamptz AND hw.timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "ctx.timestamp >= b.t_from - interval '1 hour' AND ctx.timestamp <= b.t_to",
        "ctx.timestamp >= $__timeFrom()::timestamptz - interval '1 hour' AND ctx.timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "ctx.timestamp >= b.t_from\n    AND ctx.timestamp <= b.t_to",
        "ctx.timestamp >= $__timeFrom()::timestamptz\n    AND ctx.timestamp <= $__timeTo()::timestamptz"
    )

    # 2. 🔌 功耗与电费诊断舱 优化
    sql = sql.replace(
        "WHERE timestamp >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai')",
        "WHERE timestamp >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AND timestamp <= now()"
    )
    sql = sql.replace(
        "WHERE timestamp >= now() - interval '1 hour' ORDER BY timestamp DESC LIMIT 1",
        "WHERE timestamp >= now() - interval '1 hour' AND timestamp <= now() ORDER BY timestamp DESC LIMIT 1"
    )
    sql = sql.replace(
        "WHERE timestamp >= b.range_start AND timestamp <= b.range_end",
        "WHERE timestamp >= $__timeFrom()::timestamptz AND timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "WHERE timestamp >= t_from AND timestamp <= t_to",
        "WHERE timestamp >= $__timeFrom()::timestamptz AND timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "WHERE ctx.is_foreground = 1 AND ctx.timestamp >= b.t_from - interval '1 day' AND ctx.timestamp <= b.t_to AND COALESCE(ctx.end_timestamp, now()) >= b.t_from",
        "WHERE ctx.is_foreground = 1 AND ctx.timestamp >= $__timeFrom()::timestamptz - interval '1 day' AND ctx.timestamp <= $__timeTo()::timestamptz AND COALESCE(ctx.end_timestamp, now()) >= $__timeFrom()::timestamptz"
    )

    # 3. 🐀 资源大户与后台内鬼 优化
    sql = sql.replace(
        'WHERE a."timestamp" BETWEEN b.t_from AND b.t_to',
        'WHERE a."timestamp" BETWEEN $__timeFrom()::timestamptz AND $__timeTo()::timestamptz'
    )
    sql = sql.replace(
        "AND l.start_time >= b.t_from - interval '1 day' AND l.start_time <= b.t_to AND l.start_time + (l.duration_seconds || ' seconds')::interval >= b.t_from",
        "AND l.start_time >= $__timeFrom()::timestamptz - interval '1 day' AND l.start_time <= $__timeTo()::timestamptz AND l.start_time + (l.duration_seconds || ' seconds')::interval >= $__timeFrom()::timestamptz"
    )
    sql = sql.replace(
        "ctx.timestamp >= (now() - interval '1 hour') - interval '1 day' AND ctx.timestamp <= now()",
        "ctx.timestamp >= $__timeFrom()::timestamptz - interval '1 day' AND ctx.timestamp <= $__timeTo()::timestamptz"
    )
    sql = sql.replace(
        "WHERE ctx.is_foreground = 1\n    AND ctx.timestamp >= b.t_from - interval '1 hour'\n    AND ctx.timestamp <= b.t_to",
        "WHERE ctx.is_foreground = 1\n    AND ctx.timestamp >= $__timeFrom()::timestamptz - interval '1 hour'\n    AND ctx.timestamp <= $__timeTo()::timestamptz"
    )

    return sql

def optimize_file(fp):
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)

    changed = False

    # 遍历 JSON 对象里的所有 rawSql
    def process_node(node):
        nonlocal changed
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "rawSql" and isinstance(v, str):
                    new_sql = optimize_sql(v)
                    if new_sql != v:
                        node[k] = new_sql
                        changed = True
                else:
                    process_node(v)
        elif isinstance(node, list):
            for item in node:
                process_node(item)

    process_node(data)

    if changed:
        with open(fp, 'w', encoding='utf-8') as f:
            # 保持 Grafana 大盘 JSON 格式紧凑/漂亮，使用 indent=2 且不用 ASCII 码转义 unicode 字符
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[OK] Optimized: {os.path.basename(fp)}")
    else:
        print(f"No changes made for: {os.path.basename(fp)}")

def main():
    files = [
        "addmc8x__🚀 前台交互与流畅度诊断舱.json",
        "addpc9x__🔌 功耗与电费诊断舱.json",
        "addrd7x__🐀 资源大户与后台内鬼.json"
    ]
    for fn in files:
        fp = os.path.join(DASHBOARD_DIR, fn)
        if os.path.exists(fp):
            optimize_file(fp)
        else:
            print(f"File not found: {fp}")

if __name__ == "__main__":
    main()
