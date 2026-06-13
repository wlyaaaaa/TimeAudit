# -*- coding: utf-8 -*-
"""
TimeAudit — Grafana 仪表盘恢复
==============================
把 grafana_backups/dashboards/ 里备份的仪表盘 JSON，通过 Grafana API 重新导入回 Grafana。
用于：换电脑迁移、灾后恢复，或不小心改坏了仪表盘想回滚。

手动跑：
    python E:\\TimeAudit\\restore_grafana.py            # 把备份里的所有仪表盘导入(覆盖同 uid)
    python E:\\TimeAudit\\restore_grafana.py --dry-run  # 只看会导入哪些，不实际写入
参数：
    --url / --user / --password   同 backup_grafana.py
    --file 路径                    只恢复指定的一个 JSON 文件

说明：导入按仪表盘的 uid 匹配，overwrite=true 会覆盖 Grafana 中同 uid 的仪表盘。
      如果你只是想完整恢复整个 Grafana(含用户/数据源/偏好)，直接用 grafana.db 二进制备份更省事，
      方法见《快速部署.md》。
"""
import argparse
import base64
import glob
import json
import os
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DASH_DIR = os.path.join(ROOT, "grafana_dashboards")


def log(msg):
    print(f"[grafana-restore] {msg}", flush=True)


def api_post(base_url, path, auth_header, body, timeout=15):
    req = urllib.request.Request(base_url.rstrip("/") + path, method="POST")
    req.add_header("Authorization", auth_header)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode("utf-8")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL", "http://127.0.0.1:53000"))
    ap.add_argument("--user", default=os.environ.get("GRAFANA_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("GRAFANA_PASSWORD", "admin"))
    ap.add_argument("--file", default=None, help="只恢复这一个 JSON 文件")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    auth_header = "Basic " + base64.b64encode(f"{args.user}:{args.password}".encode()).decode()

    if args.file:
        files = [args.file]
    else:
        files = sorted(glob.glob(os.path.join(DASH_DIR, "*.json")))

    if not files:
        log(f"在 {DASH_DIR} 没找到任何仪表盘备份 JSON。")
        return 1

    log(f"准备恢复 {len(files)} 个仪表盘" + ("（DRY-RUN，不实际写入）" if args.dry_run else ""))
    ok = fail = 0
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                dash = json.load(f)
            dash.pop("id", None)  # 让目标实例重新分配内部 id
            title = dash.get("title", "?")
            uid = dash.get("uid", "?")
            if args.dry_run:
                log(f"  · 会导入: {title}  (uid={uid})")
                ok += 1
                continue
            res = api_post(args.url, "/api/dashboards/db", auth_header,
                           {"dashboard": dash, "overwrite": True, "folderUid": ""})
            if res.get("status") == "success":
                log(f"  ✓ 已恢复: {title}  (uid={uid}, version={res.get('version')})")
                ok += 1
            else:
                log(f"  ✗ 失败: {title}  -> {res}")
                fail += 1
        except Exception as e:
            log(f"  ✗ 失败: {os.path.basename(fp)}  -> {e}")
            fail += 1

    log(f"完成：成功 {ok}，失败 {fail}。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
