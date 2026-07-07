from pathlib import Path


ROOT = Path(__file__).resolve().parent
START_SCRIPT = ROOT / "start_all.bat"


def test_start_all_waits_for_docker_daemon_before_compose():
    source = START_SCRIPT.read_text(encoding="utf-8-sig").lower()

    assert "docker desktop.exe" in source
    assert ":wait_docker" in source
    assert "docker info" in source
    assert source.index("docker info") < source.index("docker compose up -d")


if __name__ == "__main__":
    test_start_all_waits_for_docker_daemon_before_compose()
