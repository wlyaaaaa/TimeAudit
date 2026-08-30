import ast
import json
import re
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "grafana_dashboards"


def _walk_titles(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "title" and isinstance(child, str):
                yield child
            yield from _walk_titles(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_titles(child)


def test_dashboard_titles_do_not_hardcode_hardware_skus():
    sku_pattern = re.compile(r"(?:\bRTX\s*\d{4}\b|\b\d{4}X3D\b)", re.IGNORECASE)
    offenders = []

    for path in DASHBOARD_DIR.glob("*.json"):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for title in _walk_titles(dashboard):
            if sku_pattern.search(title):
                offenders.append(f"{path.name}: {title}")

    assert not offenders, "hardware SKU found in dashboard title:\n" + "\n".join(offenders)


def test_heartbeat_is_written_atomically(tmp_path):
    from runtime_health import (
        bounded_backoff_seconds,
        command_line_targets_script,
        write_telemetry_heartbeat,
    )

    heartbeat = tmp_path / "telemetry_heartbeat"
    assert write_telemetry_heartbeat(heartbeat)
    assert heartbeat.exists()
    assert heartbeat.read_text(encoding="ascii").strip()
    assert not list(tmp_path.glob("*.tmp"))

    target = ROOT / "main.py"
    assert command_line_targets_script(["pythonw.exe", str(target)], target)
    assert not command_line_targets_script(
        ["pythonw.exe", str(ROOT.parent / "OtherProject" / "main.py")], target
    )
    assert [bounded_backoff_seconds(attempt) for attempt in range(1, 7)] == [
        60,
        120,
        240,
        300,
        300,
        300,
    ]


def test_main_updates_heartbeat_and_releases_singleton_mutex():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    collector = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_collector"
    )

    assert "CloseHandle(_singleton_mutex)" in source
    assert "activity_worker.terminate()" in source
    assert "faulthandler.enable(_native_crash_log_handle, all_threads=True)" in source
    assert "async def _run_collector():" in source

    heartbeat_calls = [
        node
        for node in ast.walk(collector)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "write_telemetry_heartbeat"
    ]
    assert len(heartbeat_calls) == 1

    parents = {}
    for node in ast.walk(collector):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    current = heartbeat_calls[0]
    while current in parents and not isinstance(current, ast.If):
        current = parents[current]
    assert isinstance(current, ast.If)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_should_refresh_telemetry_heartbeat"
        for node in ast.walk(current.test)
    )


def test_psutil_native_crash_paths_are_contained():
    activity = (ROOT / "activity_worker.py").read_text(encoding="utf-8")
    hardware = (ROOT / "hardware_worker.py").read_text(encoding="utf-8")
    guard = (ROOT / "psutil_native_guard.py").read_text(encoding="utf-8")
    start = (ROOT / "start_all.bat").read_text(encoding="utf-8-sig")
    watchdog = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "psutil.net_connections" not in activity
    assert "IsolatedConnectionSampler" in activity
    assert "psutil.net_connections" in guard
    assert "psutil.cpu_stats" not in hardware
    assert r'"\\System\\Context Switches/sec"' in hardware
    assert ".venv\\Scripts\\pythonw.exe" in start
    assert ".venv\\Scripts\\pythonw.exe" in watchdog
    assert "psutil==7.2.2" in requirements
    assert "068b4bbd" in requirements
    assert "781d8321" in requirements

def test_duplicate_collector_never_kills_the_live_owner():
    """A duplicate launcher must yield; the watchdog owns deliberate replacement."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    singleton = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "enforce_singleton"
    )

    destructive_calls = [
        node
        for node in ast.walk(singleton)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"kill", "terminate"}
    ]
    assert not destructive_calls
    assert "duplicate collector launcher" in ast.unparse(singleton)
    assert "return None" in ast.unparse(singleton)
    assert "if _singleton_mutex is None:" in source


def test_watchdog_checks_exact_script_heartbeat_with_resume_grace():
    source = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")

    assert "function Test-HeartbeatFresh" in source
    assert "[regex]::Escape($script)" in source
    assert "Start-Sleep -Seconds $heartbeatGraceSeconds" in source
    assert "heartbeat stale" in source
    assert "function Find-MainProc" in source
    assert "function Test-AutoStartWithinGrace" in source
    assert "'pythonw.exe','python.exe'" in source
    assert "Start-Sleep -Seconds $startupGraceSeconds" in source
    assert "$autoStartMaxGraceSeconds = 240" in source
    missing_position = source.index("if (-not $mainProc)")
    startup_wait_position = source.index(
        "Start-Sleep -Seconds $startupGraceSeconds", missing_position
    )
    auto_start_grace_position = source.index(
        "Test-AutoStartWithinGrace", startup_wait_position
    )
    assert missing_position < startup_wait_position < auto_start_grace_position
    assert "Find-Proc 'pythonw.exe' 'main\\.py'" not in source
    assert "$ahkHeartbeat" in source
    assert "Restart-Ahk 'heartbeat stale after resume grace'" in source
    assert "$ingesterHeartbeat" in source
    assert "function Test-IngesterRunning" in source
    assert "Restart-Ingester 'heartbeat stale after resume grace'" in source


def test_watchdog_serializes_recovery_and_replaces_main_explicitly():
    """An orphaned scheduled-task child must not overlap another watchdog run."""
    source = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")

    assert "$watchdogMutexName = 'Global\\TimeAuditTelemetryWatchdogMutex'" in source
    assert "$watchdogMutex.WaitOne(0)" in source
    assert "function Invoke-TimeAuditWatchdog" in source
    assert "function Stop-MainForRestart" in source

    restart_start = source.index("function Restart-Main")
    restart_end = source.index("# === 2. LibreHardwareMonitor", restart_start)
    restart_main = source[restart_start:restart_end]
    assert "Stop-MainForRestart $reason" in restart_main
    assert restart_main.index("Stop-MainForRestart $reason") < restart_main.index(
        "Restart-ViaTask 'TimeAudit_WatchdogRestart_tmp'"
    )


def test_watchdog_grants_a_new_collector_bounded_bootstrap_time():
    """Initial live-PID reconciliation can take minutes; it is not a stale owner."""
    source = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")

    assert "$mainStartupHeartbeatGraceSeconds = 180" in source
    assert "function Test-MainWithinStartupGrace" in source
    stale_start = source.index("} elseif (-not (Test-HeartbeatFresh $heartbeat")
    stale_end = source.index("# === 2. LibreHardwareMonitor", stale_start)
    stale_branch = source[stale_start:stale_end]
    assert "Test-MainWithinStartupGrace $mainProc" in stale_branch
    assert stale_branch.index("Test-MainWithinStartupGrace $mainProc") < stale_branch.index(
        "Start-Sleep -Seconds $heartbeatGraceSeconds"
    )


def test_watchdog_defers_restart_while_database_endpoint_is_down():
    source = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")

    assert "$dbHostPort = 45432" in source
    assert "function Test-DatabaseEndpoint" in source
    assert "ConnectAsync($dbHost, $dbHostPort)" in source
    assert "heartbeat stale but PostgreSQL endpoint" in source
    assert "audit-ingester recovery deferred because PostgreSQL endpoint" in source


def test_ahk_emits_payload_free_progress_heartbeat():
    source = (ROOT / "TimeAudit.ahk").read_text(encoding="utf-8-sig")

    assert 'global ahkHeartbeatPath := "E:\\Projects\\Tools\\TimeAudit\\log\\ahk_heartbeat"' in source
    assert "WriteAhkHeartbeat()" in source
    assert 'FileAppend(A_NowUTC, heartbeatTemp, "UTF-8-RAW")' in source


def test_screen_time_dashboard_has_no_grafana_13_style_field_parser():
    dashboard_path = DASHBOARD_DIR / "adfkm96__📊 屏幕使用时间.json"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    serialized = json.dumps(dashboard, ensure_ascii=False)

    assert '"styleField"' not in serialized


def test_backup_payloads_default_to_g_drive():
    db_source = (ROOT / "backup_db.ps1").read_text(encoding="utf-8-sig")
    grafana = runpy.run_path(str(ROOT / "backup_grafana.py"), run_name="backup_grafana_test")

    assert r'G:\80_Backup\TimeAudit\postgresql' in db_source
    assert Path(grafana["DB_BACKUP_DIR"]) == Path(r"G:\80_Backup\TimeAudit\grafana_db")
    assert Path(grafana["DASH_DIR"]) == ROOT / "grafana_dashboards"


def test_lhm_uses_non_excluded_port_and_bounded_restart_backoff():
    config = ET.parse(ROOT / "LibreHardwareMonitor.config")
    settings = {
        node.attrib.get("key"): node.attrib.get("value")
        for node in config.iter("add")
    }
    worker = (ROOT / "hardware_worker.py").read_text(encoding="utf-8")
    health_test = (ROOT / "test_telemetry_health.py").read_text(encoding="utf-8")

    assert settings["listenerPort"] == "18085"
    assert "return 18085" in worker
    assert "lhm_restart_not_before" in worker
    assert "bounded_backoff_seconds" in worker
    assert "psutil.process_iter(['name', 'exe', 'num_threads'])" in worker
    assert "int(proc.info.get('num_threads') or 0) > 0" in worker
    assert re.search(r"lhm_process\.kill\(\)\s+killed = True", worker)
    assert 'schedule_lhm_retry("process exited")' in worker
    assert 'schedule_lhm_retry("launch failed")' in worker
    assert "else 18085" in health_test
    assert "py_cmdline_ok or py_heartbeat_ok" in health_test
    assert 'os.path.join(ROOT, "log", "telemetry_heartbeat")' in health_test


def test_external_watchdog_recovers_lhm_dead_endpoint_and_crash_ghost():
    source = (ROOT / "telemetry_watchdog.ps1").read_text(encoding="utf-8-sig")

    assert "$lhmEndpoint = 'http://127.0.0.1:18085/data.json'" in source
    assert "function Test-LhmEndpoint" in source
    assert "function Restart-Lhm" in source
    assert "Start-Sleep -Seconds $lhmGraceSeconds" in source
    assert "Get-ScheduledTask -TaskName $lhmTaskName" in source
    assert "Start-ScheduledTask -TaskName $lhmTaskName" in source
    assert "num_threads" not in source.lower()
    assert "Threads.Count -gt 0" in source
    assert "Test-LhmEndpoint" in source[source.index("function Restart-Lhm") :]


def test_manual_uses_generic_dashboard_titles():
    manual = (ROOT / "使用手册.md").read_text(encoding="utf-8")

    assert "CPU 频率与双 CCD 负载 (9950X3D)" not in manual
    assert "GPU 温度与功率 (RTX 5090 D)" not in manual
    assert "整机显存总占用 (RTX 5090 D)" not in manual


if __name__ == "__main__":
    test_dashboard_titles_do_not_hardcode_hardware_skus()
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        test_heartbeat_is_written_atomically(Path(directory))
    test_main_updates_heartbeat_and_releases_singleton_mutex()
    test_duplicate_collector_never_kills_the_live_owner()
    test_watchdog_checks_exact_script_heartbeat_with_resume_grace()
    test_watchdog_serializes_recovery_and_replaces_main_explicitly()
    test_watchdog_grants_a_new_collector_bounded_bootstrap_time()
    test_watchdog_defers_restart_while_database_endpoint_is_down()
    test_ahk_emits_payload_free_progress_heartbeat()
    test_screen_time_dashboard_has_no_grafana_13_style_field_parser()
    test_backup_payloads_default_to_g_drive()
    test_lhm_uses_non_excluded_port_and_bounded_restart_backoff()
    test_external_watchdog_recovers_lhm_dead_endpoint_and_crash_ghost()
    test_manual_uses_generic_dashboard_titles()
