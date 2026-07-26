# -*- coding: utf-8 -*-
"""
TimeAudit — Grafana 仪表盘自动备份
==================================
把 Grafana 里的【所有仪表盘】通过官方 API 导出成可读、可 diff 的 JSON 文件，
存到 grafana_dashboards/，并且【只在内容有变化时】自动 git commit + push。
同时把 grafana.db（SQLite 全量库）复制到 G:\\80_Backup\\TimeAudit\\grafana_db 做二进制兜底（带轮转、不进 git）。

为什么这样设计：
  - JSON 文件人类可读、git 能看出每次改了哪个面板 —— 这就是你要的"自动备份到 GitHub"。
  - grafana.db 是 Grafana 的完整存储（仪表盘+用户+数据源+偏好），换机/灾备时直接拿它恢复最省事。
  - 全程【不动】你现有的 provisioning，所以你在网页上照常自由编辑、保存仪表盘，毫无干扰。

手动跑：
    python E:\\TimeAudit\\backup_grafana.py
常用参数：
    --no-push          只本地 commit，不 push 到 GitHub
    --no-git           只导出文件，不碰 git
    --url   http://127.0.0.1:53000     Grafana 地址
    GRAFANA_USER / GRAFANA_PASSWORD                     从本机私密环境注入 API 认证
    --keep-db 14       grafana.db 二进制备份保留份数(默认 14)

恢复方法见《快速部署.md》"Grafana 仪表盘备份与恢复"一节。
"""
import argparse
import base64
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

# 非交互/计划任务环境里 stdout 默认 GBK，打印 ✓/❌ 等字符会 UnicodeEncodeError 崩溃。强制切 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
# 仪表盘 JSON = 前端"代码"，放进 git 跟踪的 grafana_dashboards/。
# grafana.db 二进制 = "数据"备份，放到 G 盘备份区。两者刻意分开。
DASH_DIR = os.path.join(ROOT, "grafana_dashboards")
DB_BACKUP_DIR = r"G:\80_Backup\TimeAudit\grafana_db"
GRAFANA_DB = os.path.join(ROOT, "grafana_data", "grafana.db")


def log(msg):
    print(f"[grafana-backup] {msg}", flush=True)


def api_get(base_url, path, auth_header, timeout=15):
    """调 Grafana REST API，返回解析后的 JSON。"""
    req = urllib.request.Request(base_url.rstrip("/") + path)
    req.add_header("Authorization", auth_header)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def safe_filename(name):
    """把仪表盘标题清洗成安全的文件名片段（保留中文，去掉非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    return (name or "untitled")[:80]


def export_dashboards(base_url, auth_header):
    """导出所有仪表盘为 JSON 文件。返回写出的文件名集合。"""
    os.makedirs(DASH_DIR, exist_ok=True)
    items = api_get(base_url, "/api/search?type=dash-db", auth_header)
    if not isinstance(items, list):
        raise RuntimeError(f"/api/search 返回异常: {items}")
    log(f"发现 {len(items)} 个仪表盘，开始导出 ...")

    written = set()
    for it in items:
        uid = it.get("uid")
        if not uid:
            continue
        full = api_get(base_url, f"/api/dashboards/uid/{uid}", auth_header)
        dash = full.get("dashboard")
        if not dash:
            continue
        # 去掉实例相关的内部数字 id，让备份可移植（换机重导入时由新实例重新分配）。
        dash.pop("id", None)
        title = dash.get("title", uid)
        fname = f"{uid}__{safe_filename(title)}.json"
        fpath = os.path.join(DASH_DIR, fname)
        # sort_keys + indent + ensure_ascii=False：稳定排序、缩进、保留中文 —— git diff 干净可读。
        with open(fpath, "w", encoding="utf-8", newline="\n") as f:
            json.dump(dash, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        written.add(fname)
        log(f"  ✓ {fname}  (面板 {len(dash.get('panels', []))} 个)")

    # 清理已在 Grafana 中删除的仪表盘对应的旧 JSON（保持备份目录与现状一致）。
    for old in glob.glob(os.path.join(DASH_DIR, "*.json")):
        if os.path.basename(old) not in written:
            os.remove(old)
            log(f"  ✗ 删除已不存在的仪表盘备份: {os.path.basename(old)}")
    return written


def backup_grafana_db(keep):
    """复制 grafana.db（完整二进制库）并按份数轮转。"""
    if not os.path.exists(GRAFANA_DB):
        log(f"未找到 {GRAFANA_DB}，跳过二进制库备份。")
        return
    os.makedirs(DB_BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(DB_BACKUP_DIR, f"grafana_{ts}.db")
    shutil.copy2(GRAFANA_DB, dst)
    size_mb = round(os.path.getsize(dst) / (1024 * 1024), 2)
    log(f"已复制 grafana.db → {os.path.basename(dst)} ({size_mb} MB)")
    backups = sorted(glob.glob(os.path.join(DB_BACKUP_DIR, "grafana_*.db")))
    for old in backups[:-keep] if keep > 0 else []:
        os.remove(old)
        log(f"清理过期二进制备份: {os.path.basename(old)}")


class GitSyncError(RuntimeError):
    """Git backup state is unsafe or could not be verified."""


def git(args, check=False):
    env = os.environ.copy()
    # A hidden scheduled task must fail rather than wait for an invisible
    # Git Credential Manager or terminal prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    result = subprocess.run(
        ["git", "-C", ROOT] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise GitSyncError(
            f"git {' '.join(args)} failed with exit code {result.returncode}{suffix}"
        )
    return result


def current_branch():
    branch = git(["branch", "--show-current"], check=True).stdout.strip()
    if not branch:
        raise GitSyncError("detached HEAD or unborn branch cannot be synchronized")
    return branch


def git_remote_state():
    """Fetch the configured upstream and reject remote-newer/diverged history."""
    branch = current_branch()
    remote = git(["config", "--get", f"branch.{branch}.remote"], check=True).stdout.strip()
    merge_ref = git(["config", "--get", f"branch.{branch}.merge"], check=True).stdout.strip()
    if not remote or not merge_ref.startswith("refs/heads/"):
        raise GitSyncError(f"branch {branch} has no usable upstream configuration")

    remote_branch = merge_ref.removeprefix("refs/heads/")
    tracking_ref = f"refs/remotes/{remote}/{remote_branch}"
    fetch_refspec = f"+{merge_ref}:{tracking_ref}"
    git(["fetch", "--quiet", "--prune", remote, fetch_refspec], check=True)

    counts = git(
        ["rev-list", "--left-right", "--count", f"HEAD...{tracking_ref}"],
        check=True,
    ).stdout.split()
    if len(counts) != 2:
        raise GitSyncError(f"unexpected git divergence output: {' '.join(counts)}")
    ahead, behind = (int(value) for value in counts)
    if behind:
        kind = "diverged" if ahead else "behind"
        raise GitSyncError(
            f"local branch {branch} is {kind} relative to "
            f"{remote}/{remote_branch} (ahead={ahead}, behind={behind}); "
            "refusing automatic backup commit/push"
        )
    return {
        "branch": branch,
        "remote": remote,
        "remote_branch": remote_branch,
        "remote_ref": merge_ref,
        "tracking_ref": tracking_ref,
        "ahead": ahead,
        "behind": behind,
    }


def assert_fresh_remote_oid(remote, remote_branch):
    """Read the remote directly and require its branch OID to equal local HEAD."""
    local_oid = git(["rev-parse", "HEAD"], check=True).stdout.strip()
    remote_ref = f"refs/heads/{remote_branch}"
    result = git(["ls-remote", "--exit-code", remote, remote_ref], check=True)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    matches = [row for row in rows if len(row) >= 2 and row[1] == remote_ref]
    if not matches:
        raise GitSyncError(f"fresh remote readback did not return {remote}/{remote_branch}")
    remote_oid = matches[0][0]
    if local_oid.lower() != remote_oid.lower():
        raise GitSyncError(
            f"fresh remote OID mismatch for {remote}/{remote_branch} "
            f"(local={local_oid}, remote={remote_oid})"
        )
    return remote_oid


def verified_git_push():
    """Push only when ahead, then independently verify the remote branch OID."""
    state = git_remote_state()
    pushed = False
    if state["ahead"] > 0:
        git(
            [
                "push",
                "--quiet",
                state["remote"],
                f"HEAD:{state['remote_ref']}",
            ],
            check=True,
        )
        pushed = True
    remote_oid = assert_fresh_remote_oid(state["remote"], state["remote_branch"])
    return {**state, "pushed": pushed, "verified": True, "remote_oid": remote_oid}


def git_commit_and_push(do_push):
    """只提交仪表盘暂存变化；默认还会补推并回读验证远端。"""
    rel = "grafana_dashboards"
    # Fetch before committing so a remote-newer/diverged branch is never
    # extended by unattended automation.
    if do_push:
        git_remote_state()

    git(["add", "--", rel], check=True)
    staged = git(["diff", "--cached", "--quiet", "--exit-code", "--", rel])
    if staged.returncode not in (0, 1):
        detail = (staged.stderr or staged.stdout).strip()
        raise GitSyncError(f"git staged-diff check failed: {detail}")

    if staged.returncode == 1:
        msg = "chore(grafana): 自动备份仪表盘快照 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        git(["commit", "-m", msg, "--", rel], check=True)
        log("已本地提交仪表盘变更。")
    else:
        log("仪表盘 JSON 无暂存变化，不创建提交。")

    # This still runs for a clean worktree, repairing a prior unpushed commit.
    if do_push:
        sync = verified_git_push()
        if sync["pushed"]:
            log("已 push 到 GitHub，远端 OID 回读一致。")
        else:
            log("GitHub 已是最新，远端 OID 回读一致。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL", "http://127.0.0.1:53000"))
    ap.add_argument("--user", default=os.environ.get("GRAFANA_USER"))
    ap.add_argument("--keep-db", type=int, default=14)
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    password = os.environ.get("GRAFANA_PASSWORD")
    if not args.user or not password:
        log("❌ GRAFANA_USER / GRAFANA_PASSWORD 未在私密运行环境中配置。")
        return 2

    auth_header = "Basic " + base64.b64encode(f"{args.user}:{password}".encode()).decode()

    try:
        export_dashboards(args.url, auth_header)
    except Exception as e:
        log(f"❌ 仪表盘导出失败: {e}")
        log("  常见原因: Grafana 没起，或私密 Grafana API 认证已失效。")
        return 1

    try:
        backup_grafana_db(args.keep_db)
    except Exception as e:
        log(f"⚠️ grafana.db 复制失败(不影响 JSON 备份): {e}")

    if not args.no_git:
        try:
            git_commit_and_push(do_push=not args.no_push)
        except Exception as e:
            log(f"❌ git 云备份失败，任务返回非零以触发调度重试: {e}")
            return 1

    log("✅ 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
