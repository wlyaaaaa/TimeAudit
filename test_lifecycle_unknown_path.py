import unittest
from pathlib import Path

from lifecycle_worker import unknown_executable_path


class UnknownExecutablePathTests(unittest.TestCase):
    def test_unknown_path_uses_explicit_placeholder(self):
        path = unknown_executable_path("git.exe")

        self.assertEqual(path, r"<unknown>\git.exe")
        self.assertFalse(path.lower().startswith(r"c:\windows\system32"))

    def test_unknown_path_sanitizes_empty_or_path_like_name(self):
        self.assertEqual(unknown_executable_path(""), r"<unknown>\Unknown.exe")
        self.assertEqual(unknown_executable_path(r"C:\Temp\python.exe"), r"<unknown>\python.exe")

    def test_unknown_fallbacks_do_not_fabricate_system32_paths(self):
        root = Path(__file__).resolve().parent
        for filename in ("lifecycle_worker.py", "context_worker.py"):
            with self.subTest(filename=filename):
                source = (root / filename).read_text(encoding="utf-8")
                self.assertNotIn(r"C:\Windows\System32\Unknown.exe", source)
                self.assertNotIn(r"C:\Windows\System32\{name}", source)


if __name__ == "__main__":
    unittest.main()
