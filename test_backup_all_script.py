import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
