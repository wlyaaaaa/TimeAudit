from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parent
START_SCRIPT = ROOT / "start_all.bat"


def test_start_all_waits_for_docker_daemon_before_compose():
    source = START_SCRIPT.read_text(encoding="utf-8-sig").lower()

    assert "docker desktop.exe" in source
    assert ":wait_docker" in source
    assert "docker info" in source
    assert source.index("docker info") < source.index("docker compose up -d")
    assert "db_wait_seconds" in source
    assert "db_wait_remaining" in source
    assert 'timeaudit_db_host_port=45432' in source
    assert "tcpclient" in source
    assert "netstat -ano" not in source
    assert "exit /b 0" in source
    assert "timeout /t" not in source


def test_start_all_uses_crlf_line_endings_for_cmd_exe():
    content = START_SCRIPT.read_bytes()
    assert b"\r\n" in content
    assert b"\n" not in content.replace(b"\r\n", b"")


def test_postgres_host_port_stays_outside_windows_dynamic_pool():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    db_config = (ROOT / "db_config.py").read_text(encoding="utf-8")

    assert "${TIMEAUDIT_DB_HOST_PORT:-45432}:5432" in compose
    assert "DEFAULT_DB_HOST_PORT = 45432" in db_config
    assert "55432" not in compose
    assert "55432" not in db_config


def test_recovery_bootstrap_preserves_existing_containers_and_exact_ahk():
    source = START_SCRIPT.read_text(encoding="utf-8-sig")
    assert "docker compose up -d --no-recreate" in source
    line = next(line for line in source.splitlines() if "$target=Join-Path" in line)
    command = line.split('-Command "', 1)[1].rsplit('" >nul', 1)[0].replace('\\"', '"')
    environment = dict(os.environ, PROJECT_DIR=str(ROOT))
    for path, expected in ((str(ROOT / "TimeAudit.ahk"), 0),
                           (str(ROOT / "TimeAudit.ahk.backup"), 1),
                           (str(ROOT.parent / "Other" / "TimeAudit.ahk"), 1)):
        commandline = '"C:\\Program Files\\AutoHotkey\\v2\\AutoHotkey64.exe" "' + path + '"'
        fixture = "function Get-CimInstance { param($Filter,$ErrorAction) [pscustomobject]@{CommandLine='" + commandline.replace("'", "''") + "'} }; "
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", fixture + command],
                                env=environment, capture_output=True, timeout=15)
        assert result.returncode == expected, result.stderr.decode(errors="replace")
    probe_position = source.index(line)
    launch_position = source.index('if %errorlevel% neq 0 start "" "%PROJECT_DIR%\\TimeAudit.ahk"')
    assert probe_position < launch_position


def test_grafana_defaults_agree_and_stay_outside_dynamic_pool():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"43000:3000"' in compose
    assert "GF_LIVE_ALLOWED_ORIGINS=http://localhost:43000" in compose
    for filename in ("timeaudit_health.py", "telemetry_watchdog.ps1", "backup_grafana.py", "restore_grafana.py"):
        source = (ROOT / filename).read_text(encoding="utf-8-sig")
        assert "127.0.0.1:43000" in source
        assert "53000" not in source


if __name__ == "__main__":
    test_start_all_waits_for_docker_daemon_before_compose()
    test_start_all_uses_crlf_line_endings_for_cmd_exe()
    test_postgres_host_port_stays_outside_windows_dynamic_pool()
