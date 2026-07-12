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

    assert "write_telemetry_heartbeat()" in source
    assert "CloseHandle(_singleton_mutex)" in source
    gather_position = source.index("await asyncio.gather(")
    heartbeat_position = source.index("write_telemetry_heartbeat()", gather_position)
    assert gather_position < heartbeat_position
    assert "async def _run_collector():" in source


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
    assert "psutil.process_iter(['name', 'exe'])" in worker
    assert re.search(r"lhm_process\.kill\(\)\s+killed = True", worker)
    assert 'schedule_lhm_retry("process exited")' in worker
    assert 'schedule_lhm_retry("launch failed")' in worker
    assert "else 18085" in health_test


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
    test_watchdog_checks_exact_script_heartbeat_with_resume_grace()
    test_backup_payloads_default_to_g_drive()
    test_lhm_uses_non_excluded_port_and_bounded_restart_backoff()
    test_manual_uses_generic_dashboard_titles()
