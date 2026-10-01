"""Classify narrowly scoped file exceptions without hiding ordinary I/O failures."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import time


def file_warning(exc, relative_path, stage, *, enumerated=False):
    code = getattr(exc, "winerror", None)
    if code is None:
        value = getattr(exc, "hresult", None)
        if value is not None and (value & 0xFFFF0000) == 0x80070000:
            code = value & 0xFFFF
    if code in (225, 226):
        reason = "antivirus_blocked" if code == 225 else "antivirus_removed"
    elif enumerated and isinstance(exc, FileNotFoundError):
        code, reason = getattr(exc, "errno", 2), "source_disappeared"
    else:
        return None
    return {"relative_path": str(relative_path).replace("\\", "/"),
            "reason": reason, "error_code": code, "stage": stage}


def defender_records(paths, started):
    """Read only exact candidate paths; return no unrelated detection metadata."""
    if os.name != "nt":
        return []
    env = os.environ.copy()
    env["BACKUP_AV_PATHS"] = json.dumps([str(Path(p).absolute()) for p in paths])
    env["BACKUP_AV_STARTED"] = str(started)
    script = """$paths=@($env:BACKUP_AV_PATHS|ConvertFrom-Json); $start=[double]::Parse($env:BACKUP_AV_STARTED,[cultureinfo]::InvariantCulture); $rows=@(); Get-MpThreatDetection -ErrorAction Stop | ForEach-Object {$d=$_; if($d.ActionSuccess){$observed=([DateTimeOffset]$d.LastThreatStatusChangeTime).ToUnixTimeMilliseconds()/1000.0; if($observed -ge $start){foreach($r in $d.Resources){$p=$r -replace '^file:_',''; foreach($candidate in $paths){if($p -ieq $candidate){$rows+=@{path=$candidate;success=$true;observed_unix=$observed}}}}}}}; ConvertTo-Json -InputObject $rows -Compress"""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, env=env, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def sqlite_warning(exc, candidates, started, stage, *, records=None):
    """SQLite loses WinError: only this attempt's exact-path remediation proves AV."""
    if not isinstance(exc, (sqlite3.OperationalError, FileNotFoundError)):
        return None
    available = []
    for path, relative in candidates:
        path = Path(path).absolute()
        if not path.parent.is_dir():
            continue  # A missing source/target root is never a file-level warning.
        available.append((path, relative))
    if not available:
        return None
    records = defender_records([p for p, _ in available], started) if records is None else records
    now = time.time()
    for path, relative in available:
        for record in records:
            try:
                exact = os.path.normcase(str(Path(record["path"]).absolute())) == os.path.normcase(str(path))
                current = started <= float(record["observed_unix"]) <= now
            except (KeyError, TypeError, ValueError):
                continue
            if exact and current and record.get("success") is True:
                return {"relative_path": str(relative).replace("\\", "/"), "reason": "antivirus_removed",
                        "error_code": 226, "stage": stage}
    return None
