# -*- coding: utf-8 -*-
"""
TimeAudit — Grafana 仪表盘恢复
==============================
把 grafana_dashboards/ 里备份的仪表盘 JSON，通过 Grafana API 重新导入回 Grafana。
用于：换电脑迁移、灾后恢复，或不小心改坏了仪表盘想回滚。

手动跑：
    python E:\\TimeAudit\\restore_grafana.py            # 把备份里的所有仪表盘导入(覆盖同 uid)
    python E:\\TimeAudit\\restore_grafana.py --dry-run  # 只看会导入哪些，不实际写入
参数：
    --url / --user                连接参数；密码只从 GRAFANA_PASSWORD 私密环境变量读取
    --file 路径                    只恢复指定的一个 JSON 文件

说明：导入按仪表盘的 uid 匹配，overwrite=true 会覆盖 Grafana 中同 uid 的仪表盘。
      只接受扩展名精确为 .json 且通过 datasource/matcher 恢复合同的快照；编辑器 .bak 不可导入。
      如果你只是想完整恢复整个 Grafana(含用户/数据源/偏好)，直接用 grafana.db 二进制备份更省事，
      方法见《快速部署.md》。
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

from grafana_dashboard_contract import (
    DashboardContractError,
    RECOVERY_DATASOURCE_UID,
    discover_dashboard_files,
    validate_dashboard_document,
)

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


def api_get(base_url, path, auth_header, timeout=15):
    req = urllib.request.Request(base_url.rstrip("/") + path)
    req.add_header("Authorization", auth_header)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def validate_recovery_datasource(base_url, auth_header):
    datasource = api_get(
        base_url,
        f"/api/datasources/uid/{RECOVERY_DATASOURCE_UID}",
        auth_header,
    )
    if (
        not isinstance(datasource, dict)
        or datasource.get("uid") != RECOVERY_DATASOURCE_UID
        or datasource.get("type") != "postgres"
    ):
        raise RuntimeError(
            "recovery datasource UID/type does not match the dashboard contract"
        )


def validate_dashboard_readback(base_url, auth_header, expected_uid):
    readback = api_get(
        base_url,
        f"/api/dashboards/uid/{expected_uid}",
        auth_header,
    )
    dashboard = readback.get("dashboard") if isinstance(readback, dict) else None
    if not isinstance(dashboard, dict) or dashboard.get("uid") != expected_uid:
        raise RuntimeError(f"dashboard UID readback failed for {expected_uid}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL", "http://127.0.0.1:53000"))
    ap.add_argument("--user", default=os.environ.get("GRAFANA_USER"))
    ap.add_argument("--file", default=None, help="只恢复这一个 JSON 文件")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        files = discover_dashboard_files(DASH_DIR, args.file)
    except DashboardContractError as exc:
        log(f"拒绝恢复候选: {exc}")
        return 2

    if not files:
        log(f"在 {DASH_DIR} 没找到任何仪表盘备份 JSON。")
        return 1

    documents = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                dash = json.load(f)
            validate_dashboard_document(dash, source=os.path.basename(fp))
            dash.pop("id", None)  # 让目标实例重新分配内部 id
            documents.append((fp, dash))
        except Exception as exc:
            log(f"拒绝恢复候选 {os.path.basename(fp)}: {exc}")
            return 1

    log(f"准备恢复 {len(files)} 个仪表盘" + ("（DRY-RUN，不实际写入）" if args.dry_run else ""))
    if args.dry_run:
        for _, dash in documents:
            log(f"  · 会导入: {dash.get('title', '?')}  (uid={dash['uid']})")
        log(f"完成：成功 {len(documents)}，失败 0。")
        return 0

    password = os.environ.get("GRAFANA_PASSWORD")
    if not args.user or not password:
        log("GRAFANA_USER / GRAFANA_PASSWORD 未在私密运行环境中配置。")
        return 2
    auth_header = "Basic " + base64.b64encode(
        f"{args.user}:{password}".encode()
    ).decode()
    try:
        validate_recovery_datasource(args.url, auth_header)
        log(f"已验证恢复数据源 UID/type: {RECOVERY_DATASOURCE_UID}/postgres")
    except Exception as exc:
        log(f"恢复前置检查失败: {exc}")
        return 1

    ok = fail = 0
    for fp, dash in documents:
        try:
            title = dash.get("title", "?")
            uid = dash["uid"]
            res = api_post(args.url, "/api/dashboards/db", auth_header,
                           {"dashboard": dash, "overwrite": True, "folderUid": ""})
            if res.get("status") == "success":
                validate_dashboard_readback(args.url, auth_header, uid)
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
