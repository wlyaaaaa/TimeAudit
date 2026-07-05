from pathlib import Path


ROOT = Path(__file__).resolve().parent
WATCHDOG_SCRIPT = ROOT / "telemetry_watchdog.ps1"


def test_watchdog_temp_task_uses_short_lived_wrapper():
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8-sig")

    assert "New-ScheduledTaskAction -Execute 'powershell.exe'" in source
    assert "Start-Process" in source
    assert "New-ScheduledTaskAction -Execute $exe -Argument $arg" not in source


def test_watchdog_cleans_non_running_legacy_temp_tasks():
    source = WATCHDOG_SCRIPT.read_text(encoding="utf-8-sig")

    assert "function Remove-StaleTask" in source
    assert "Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue" in source
    assert "State -ne 'Running'" in source
    assert "Remove-StaleTask 'TimeAudit_WatchdogAhkRestart_tmp'" in source


if __name__ == "__main__":
    test_watchdog_temp_task_uses_short_lived_wrapper()
    test_watchdog_cleans_non_running_legacy_temp_tasks()
