import unittest
from pathlib import Path
import json
import shutil
import subprocess
import pytest


class BackupAllScriptTests(unittest.TestCase):
    def test_child_output_is_logged_as_utf8_text(self):
        script = Path(__file__).with_name("backup_all.ps1").read_text(encoding="utf-8")

        self.assertNotIn("*>> $log", script)
        self.assertIn("Invoke-LoggedCommand", script)
        self.assertIn("Out-File $log -Append -Encoding utf8", script)

    def test_child_failures_propagate_to_task_result(self):
        script = Path(__file__).with_name("backup_all.ps1").read_text(encoding="utf-8")

        self.assertIn("$exitCode", script)
        self.assertNotIn("exit 0", script)
        self.assertIn("exit $exitCode", script)

    def test_receipt_reports_database_and_dashboard_separately(self):
        script = Path(__file__).with_name("backup_all.ps1").read_text(encoding="utf-8")

        self.assertIn("database_backup =", script)
        self.assertIn("dashboard_configuration =", script)
        self.assertIn("overall_status =", script)

    def test_hidden_wrapper_waits_and_propagates_exit_code(self):
        wrapper = Path(__file__).with_name("backup_all_hidden.vbs").read_text(encoding="utf-8")

        self.assertIn(", 0, True)", wrapper)
        self.assertNotIn(", 0, False)", wrapper)
        self.assertIn("WScript.Quit exitCode", wrapper)


if __name__ == "__main__":
    unittest.main()


def test_daily_receipt_retains_database_diagnostic_and_exit_code(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell is required for the daily entrypoint behavior check")
    script = Path(__file__).with_name("backup_all.ps1").read_text(encoding="utf-8-sig")
    script = script.replace(r"E:\Projects\Tools\TimeAudit", str(tmp_path))
    # Real orchestration/logging, synthetic child commands: no database or GUI.
    fixture = """
function powershell {
    $global:LASTEXITCODE = 2
    '{"status":"failed","mode":"backup","reason":"backup_command_failed","stage":"pg_dump","exit_code":2,"error_summary":"server unavailable"}'
}
function py { $global:LASTEXITCODE = 0; 'dashboard fixture passed' }
"""
    entry = tmp_path / "daily-fixture.ps1"
    entry.write_text(fixture + script, encoding="utf-8-sig")
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(entry)], capture_output=True, text=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 2, result.stderr
    lines = (tmp_path / "log" / "backup.log").read_text(encoding="utf-8-sig").splitlines()
    receipt = next(json.loads(line) for line in lines if line.startswith('{"schema"'))
    failure = receipt["database_backup"]["failure"]
    assert failure["stage"] == "pg_dump" and failure["exit_code"] == 2
    assert failure["error_summary"] == "server unavailable"
    assert receipt["dashboard_configuration"]["status"] == "pass"
    assert receipt["overall_status"] == "failed"
