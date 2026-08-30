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
    --source sqlite     默认；只读本机 grafana.db，不需要无人值守凭据
    --source api        远程/手动 API 模式；认证从本机私密环境注入
    GRAFANA_USER / GRAFANA_PASSWORD                     仅 API 模式使用
    --keep-db 14       grafana.db 二进制备份保留份数(默认 14)

恢复方法见《快速部署.md》"Grafana 仪表盘备份与恢复"一节。
"""
import argparse
import base64
import contextlib
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from grafana_dashboard_contract import validate_dashboard_document

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


def resolve_proxy_settings(proxy_server):
    if not proxy_server or not proxy_server.strip():
        return None
    raw = proxy_server.strip()
    by_scheme = {}
    for part in raw.split(";"):
        if "=" in part:
            scheme, endpoint = part.split("=", 1)
            by_scheme[scheme.strip().lower()] = endpoint.strip()

    if by_scheme:
        http_endpoint = by_scheme.get("http") or by_scheme.get("https")
        https_endpoint = by_scheme.get("https") or by_scheme.get("http")
    else:
        http_endpoint = raw
        https_endpoint = raw

    def normalize(endpoint):
        if not endpoint:
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*://", endpoint, re.IGNORECASE):
            return endpoint
        return "http://" + endpoint

    return {"http": normalize(http_endpoint), "https": normalize(https_endpoint)}


def initialize_windows_user_proxy():
    """Apply the currently enabled HKCU proxy to this scheduled process."""
    if os.name != "nt":
        return False
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
        if enabled != 1:
            return False
        settings = resolve_proxy_settings(server)
        if not settings:
            return False
        for name in ("HTTP_PROXY", "http_proxy"):
            os.environ[name] = settings["http"]
        for name in ("HTTPS_PROXY", "https_proxy"):
            os.environ[name] = settings["https"]
        existing_no_proxy = os.environ.get("NO_PROXY", "")
        no_proxy = [part.strip() for part in existing_no_proxy.split(",") if part.strip()]
        for local in ("127.0.0.1", "localhost"):
            if local not in no_proxy:
                no_proxy.append(local)
        os.environ["NO_PROXY"] = ",".join(no_proxy)
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
        return True
    except (OSError, ValueError):
        return False


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


HARDWARE_SKU_IN_TITLE = re.compile(
    r"\s*(?:\((?:RTX\s*\d{4}|\d{4}X3D)\)|RTX\s*\d{4}|\d{4}X3D)",
    re.IGNORECASE,
)


def _normalize_dashboard_value(value, parent_key=None):
    if isinstance(value, list):
        return [_normalize_dashboard_value(item, parent_key) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {}
    for key, child in value.items():
        # Numeric dashboard/panel instance ids are machine-local. Matcher and
        # transformation ids, however, are semantic Grafana configuration
        # (for example ``byName`` and ``organize``); Grafana 13 rejects either
        # object when its id is missing.
        if key == "id" and parent_key not in {"matcher", "transformations"}:
            continue
        if key == "title" and isinstance(child, str):
            child = HARDWARE_SKU_IN_TITLE.sub("", child).strip()
        normalized[key] = _normalize_dashboard_value(child, key)
    return normalized


def normalize_dashboard_for_public_backup(dashboard):
    """Remove machine-local ids/SKUs while preserving semantic config ids."""
    return _normalize_dashboard_value(dashboard)


def _dashboard_version(dashboard):
    """Return Grafana's monotonic dashboard generation, or None if invalid."""
    value = dashboard.get("version") if isinstance(dashboard, dict) else None
    if type(value) is int and value >= 0:
        return value
    return None


def _assert_dashboard_export_is_not_stale(path, previous, incoming, content):
    """Never let an older or ambiguous live copy replace repository work."""
    if previous is None or previous == content:
        return
    try:
        repository = json.loads(previous)
    except (TypeError, ValueError) as exc:
        raise GitSyncError(
            f"repository dashboard snapshot is invalid JSON: {os.path.basename(path)}"
        ) from exc

    repository_version = _dashboard_version(repository)
    incoming_version = _dashboard_version(incoming)
    if repository_version is None or incoming_version is None:
        raise GitSyncError(
            "dashboard version is missing or invalid; refusing ambiguous overwrite: "
            f"{os.path.basename(path)}"
        )
    if incoming_version < repository_version:
        raise GitSyncError(
            "live dashboard is older than repository snapshot; refusing rollback: "
            f"{os.path.basename(path)} "
            f"(live={incoming_version}, repository={repository_version})"
        )
    if incoming_version == repository_version:
        raise GitSyncError(
            "dashboard same-version divergence detected; refusing ambiguous overwrite: "
            f"{os.path.basename(path)} (version={incoming_version})"
        )


def write_dashboard_documents(documents, changed_paths=None, before_write=None):
    """Write normalized dashboard documents and remove obsolete snapshots safely."""
    if before_write is None:
        before_write = assert_dashboard_worktree_clean
    before_write()
    normalized_documents = []
    for uid, dash in documents:
        if not uid or not isinstance(dash, dict):
            continue
        dash = normalize_dashboard_for_public_backup(dash)
        validate_dashboard_document(
            dash,
            source=f"live dashboard {uid}",
            expected_uid=uid,
        )
        normalized_documents.append((uid, dash))

    os.makedirs(DASH_DIR, exist_ok=True)
    written = set()
    for uid, dash in normalized_documents:
        title = dash.get("title", uid)
        fname = f"{uid}__{safe_filename(title)}.json"
        fpath = os.path.join(DASH_DIR, fname)
        # sort_keys + indent + ensure_ascii=False：稳定排序、缩进、保留中文 —— git diff 干净可读。
        content = json.dumps(dash, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        previous = None
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8", newline="") as f:
                previous = f.read()
        if previous != content:
            _assert_dashboard_export_is_not_stale(
                fpath,
                previous,
                dash,
                content,
            )
            _write_dashboard_json_atomically(fpath, content)
            _record_dashboard_change(changed_paths, fpath)
        written.add(fname)
        log(f"  ✓ {fname}  (面板 {len(dash.get('panels', []))} 个)")

    # 清理已在 Grafana 中删除的仪表盘对应的旧 JSON（保持备份目录与现状一致）。
    for old in glob.glob(os.path.join(DASH_DIR, "*.json")):
        if os.path.basename(old) not in written:
            os.remove(old)
            _record_dashboard_change(changed_paths, old)
            log(f"  ✗ 删除已不存在的仪表盘备份: {os.path.basename(old)}")
    return written


def export_dashboards(base_url, auth_header, changed_paths=None, before_write=None):
    """Export dashboards through the Grafana API for remote/manual use."""
    items = api_get(base_url, "/api/search?type=dash-db", auth_header)
    if not isinstance(items, list):
        raise RuntimeError(f"/api/search 返回异常: {items}")
    log(f"通过 API 发现 {len(items)} 个仪表盘，开始导出 ...")

    documents = []
    for item in items:
        uid = item.get("uid")
        if not uid:
            continue
        full = api_get(base_url, f"/api/dashboards/uid/{uid}", auth_header)
        dash = full.get("dashboard")
        if dash:
            documents.append((uid, dash))
    return write_dashboard_documents(
        documents,
        changed_paths=changed_paths,
        before_write=before_write,
    )


def _sqlite_table_exists(connection, table):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _dashboard_documents_from_sqlite(connection):
    """Read both Grafana legacy and unified-storage dashboard schemas."""
    documents = []
    if _sqlite_table_exists(connection, "resource"):
        rows = connection.execute(
            """
            SELECT name, value
              FROM resource
             WHERE "group" = ? AND resource = ?
             ORDER BY name
            """,
            ("dashboard.grafana.app", "dashboards"),
        ).fetchall()
        for uid, value in rows:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            resource = json.loads(value)
            dash = dict(resource.get("spec") or {})
            metadata = resource.get("metadata") or {}
            dash["uid"] = uid
            dash["version"] = int(metadata.get("generation") or 0)
            documents.append((uid, dash))
        if documents:
            return documents

    if _sqlite_table_exists(connection, "dashboard"):
        rows = connection.execute(
            """
            SELECT uid, data
              FROM dashboard
             WHERE is_folder = 0 AND deleted IS NULL
             ORDER BY uid
            """
        ).fetchall()
        for uid, value in rows:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            dash = json.loads(value)
            dash["uid"] = uid
            documents.append((uid, dash))
    return documents


def export_dashboards_from_db(database_path=None, changed_paths=None, before_write=None):
    """Export a consistent read-only snapshot without unattended credentials."""
    if database_path is None:
        database_path = GRAFANA_DB
    absolute = os.path.abspath(database_path)
    connection = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        documents = _dashboard_documents_from_sqlite(connection)
        connection.commit()
    finally:
        connection.close()
    log(f"从本机 Grafana SQLite 一致快照发现 {len(documents)} 个仪表盘，开始导出 ...")
    return write_dashboard_documents(
        documents,
        changed_paths=changed_paths,
        before_write=before_write,
    )


def backup_grafana_db(keep):
    """Create a consistent SQLite backup and rotate old snapshots."""
    if not os.path.exists(GRAFANA_DB):
        log(f"未找到 {GRAFANA_DB}，跳过二进制库备份。")
        return
    os.makedirs(DB_BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(DB_BACKUP_DIR, f"grafana_{ts}.db")
    source = sqlite3.connect(f"file:{os.path.abspath(GRAFANA_DB)}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("grafana.db 快照 quick_check 未通过")
    finally:
        target.close()
        source.close()
    shutil.copystat(GRAFANA_DB, dst)
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


def _managed_dashboard_directory():
    """Return the repository-relative dashboard directory when it is in this checkout."""
    expected = os.path.normcase(os.path.abspath(os.path.join(ROOT, "grafana_dashboards")))
    actual = os.path.normcase(os.path.abspath(DASH_DIR))
    if actual != expected:
        # Tests and callers may deliberately export to a temporary directory.
        # Only the checkout's managed directory participates in Git safety checks.
        return None
    return "grafana_dashboards"


def _normalize_dashboard_paths(paths):
    """Reject anything outside the generated JSON snapshot allowlist."""
    normalized = set()
    prefix = "grafana_dashboards/"
    for raw_path in paths:
        if not isinstance(raw_path, str):
            raise GitSyncError("dashboard staging allowlist contains a non-string path")
        path = raw_path.replace("\\", "/")
        parts = path.split("/")
        if (
            not path.startswith(prefix)
            or not path.endswith(".json")
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise GitSyncError(
                "dashboard staging allowlist must contain only grafana_dashboards/*.json"
            )
        normalized.add(path)
    return normalized


def _dashboard_relative_path(path):
    relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
    return next(iter(_normalize_dashboard_paths([relative])))


def _record_dashboard_change(changed_paths, path):
    if changed_paths is not None:
        changed_paths.add(_dashboard_relative_path(path))


def _write_dashboard_json_atomically(path, content):
    """Replace one snapshot atomically, leaving no half-written JSON on failure."""
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def assert_dashboard_worktree_clean():
    """Fail before an unattended export can replace or delete manual dashboard work."""
    relative = _managed_dashboard_directory()
    if relative is None:
        return
    status = git(
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            relative,
        ],
        check=True,
    )
    if status.stdout:
        raise GitSyncError(
            "uncommitted dashboard changes detected; refusing automatic export/deletion"
        )


def _git_nul_paths(args):
    output = git(args, check=True).stdout
    return {path.replace("\\", "/") for path in output.split("\0") if path}


def dashboard_json_paths():
    """Return every dashboard change Git currently sees, rejecting non-JSON entries."""
    relative = _managed_dashboard_directory()
    if relative is None:
        return set()
    observed = set()
    for args in (
        ["diff", "--no-renames", "--name-only", "-z", "--", relative],
        ["diff", "--cached", "--no-renames", "--name-only", "-z", "--", relative],
        ["ls-files", "--others", "--exclude-standard", "-z", "--", relative],
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            relative,
        ],
    ):
        observed.update(_git_nul_paths(args))
    return _normalize_dashboard_paths(observed)


def assert_dashboard_change_allowlist(expected_paths):
    """Ensure post-export Git changes are exactly the files this run generated/deleted."""
    expected = _normalize_dashboard_paths(expected_paths)
    observed = dashboard_json_paths()
    if observed != expected:
        raise GitSyncError(
            "dashboard changes no longer match this export's JSON allowlist; "
            "refusing Git staging"
        )
    return expected


@contextlib.contextmanager
def grafana_backup_lock():
    """Use a non-blocking checkout-local lock so scheduled backups cannot overlap."""
    lock_path = git(
        ["rev-parse", "--git-path", "timeaudit-grafana-backup.lock"], check=True
    ).stdout.strip()
    if not lock_path:
        raise GitSyncError("could not resolve the Grafana backup lock path")
    if not os.path.isabs(lock_path):
        lock_path = os.path.join(ROOT, lock_path)

    handle = open(lock_path, "a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise GitSyncError(
                "another Grafana backup process is running; refusing concurrent export"
            ) from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def retryable_git_network_failure(result):
    if result.returncode == 0:
        return False
    detail = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    markers = (
        "could not connect",
        "failed to connect",
        "tls connect error",
        "ssl routines",
        "unexpected eof",
        "connection reset",
        "connection timed out",
        "operation timed out",
        "the requested url returned error: 502",
        "the requested url returned error: 503",
        "the requested url returned error: 504",
    )
    return any(marker in detail for marker in markers)


def git_network(args, delays=(5, 15, 30)):
    """Retry only transport failures; repository-state failures stay fail-closed."""
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        result = git(args)
        if result.returncode == 0:
            return result
        if not retryable_git_network_failure(result) or attempt == attempts:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise GitSyncError(
                f"git {' '.join(args)} failed with exit code {result.returncode}{suffix}"
            )
        log(f"Git 网络瞬态失败，{delays[attempt - 1]} 秒后重试 ({attempt}/{attempts})。")
        time.sleep(delays[attempt - 1])
    raise AssertionError("unreachable")


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
    git_network(["fetch", "--quiet", "--prune", remote, fetch_refspec])

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
    result = git_network(["ls-remote", "--exit-code", remote, remote_ref])
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
        git_network(
            [
                "push",
                "--quiet",
                state["remote"],
                f"HEAD:{state['remote_ref']}",
            ],
        )
        pushed = True
    remote_oid = assert_fresh_remote_oid(state["remote"], state["remote_branch"])
    return {**state, "pushed": pushed, "verified": True, "remote_oid": remote_oid}


def git_commit_and_push(do_push, dashboard_paths):
    """Stage and commit only this run's explicit dashboard JSON allowlist."""
    paths = sorted(_normalize_dashboard_paths(dashboard_paths))
    # Fetch before committing so a remote-newer/diverged branch is never
    # extended by unattended automation.
    if do_push:
        git_remote_state()

    if not paths:
        log("仪表盘 JSON 无暂存变化，不创建提交。")
    else:
        git(["add", "--all", "--", *paths], check=True)
        staged_paths = _git_nul_paths(
            [
                "diff",
                "--cached",
                "--no-renames",
                "--name-only",
                "-z",
                "--",
                "grafana_dashboards",
            ]
        )
        if staged_paths != set(paths):
            raise GitSyncError(
                "staged dashboard paths no longer match this export's JSON allowlist; "
                "refusing commit"
            )
        staged = git(["diff", "--cached", "--quiet", "--exit-code", "--", *paths])
        if staged.returncode not in (0, 1):
            detail = (staged.stderr or staged.stdout).strip()
            raise GitSyncError(f"git staged-diff check failed: {detail}")
        if staged.returncode != 1:
            raise GitSyncError(
                "dashboard allowlist had no staged changes after export; refusing ambiguous commit"
            )
        msg = "chore(grafana): 自动备份仪表盘快照 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        git(["commit", "-m", msg, "--", *paths], check=True)
        log("已本地提交仪表盘变更。")

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
    ap.add_argument("--source", choices=("sqlite", "api"), default="sqlite")
    ap.add_argument("--user", default=os.environ.get("GRAFANA_USER"))
    ap.add_argument("--keep-db", type=int, default=14)
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    if initialize_windows_user_proxy():
        log("已应用当前用户 Windows 代理设置。")

    try:
        with grafana_backup_lock():
            changed_paths = set()
            try:
                # Check before source extraction, then once more immediately before writes.
                # A scheduled job must never overwrite or delete a human's dashboard work.
                assert_dashboard_worktree_clean()
                if args.source == "sqlite":
                    export_dashboards_from_db(
                        changed_paths=changed_paths,
                        before_write=assert_dashboard_worktree_clean,
                    )
                else:
                    password = os.environ.get("GRAFANA_PASSWORD")
                    if not args.user or not password:
                        log("❌ API 模式需要私密 GRAFANA_USER / GRAFANA_PASSWORD。")
                        return 2
                    auth_header = "Basic " + base64.b64encode(
                        f"{args.user}:{password}".encode()
                    ).decode()
                    export_dashboards(
                        args.url,
                        auth_header,
                        changed_paths=changed_paths,
                        before_write=assert_dashboard_worktree_clean,
                    )
                dashboard_paths = assert_dashboard_change_allowlist(changed_paths)
            except Exception as e:
                log(f"❌ 仪表盘导出失败: {e}")
                log("  SQLite 模式请检查本机 grafana.db；API 模式请检查服务与认证。")
                return 1

            try:
                backup_grafana_db(args.keep_db)
            except Exception as e:
                log(f"⚠️ grafana.db 复制失败(不影响 JSON 备份): {e}")

            if not args.no_git:
                try:
                    git_commit_and_push(
                        do_push=not args.no_push,
                        dashboard_paths=dashboard_paths,
                    )
                except Exception as e:
                    log(f"❌ git 云备份失败，任务返回非零以触发调度重试: {e}")
                    return 1
    except Exception as e:
        log(f"❌ Grafana 备份互斥锁不可用: {e}")
        return 1

    log("✅ 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
