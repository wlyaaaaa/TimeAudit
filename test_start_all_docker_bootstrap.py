from pathlib import Path


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


if __name__ == "__main__":
    test_start_all_waits_for_docker_daemon_before_compose()
    test_start_all_uses_crlf_line_endings_for_cmd_exe()
    test_postgres_host_port_stays_outside_windows_dynamic_pool()
