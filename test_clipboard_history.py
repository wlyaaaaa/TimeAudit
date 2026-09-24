import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clipboard_history.control import ControlState, read_control_state, write_control_state
from clipboard_history.backup import create_backup, restore_backup, verify_backup
from clipboard_history.model import (
    CAPTURE_LIMIT_BYTES,
    CaptureDecision,
    classify_text,
    content_kind,
    fts_literal_query,
    looks_like_link,
    looks_like_secret,
)
from clipboard_history.storage import ClipboardStore, ReadOnlyClipboardStore


class ModelTests(unittest.TestCase):
    def test_classifies_chinese_emoji_multiline_and_url(self):
        text = "中文🙂\n第二行"
        self.assertEqual(classify_text(text), CaptureDecision("text", text, None))
        url = "https://example.invalid/路径?q=1"
        self.assertEqual(classify_text(url).payload_type, "url")

    def test_empty_and_oversize_are_skipped_without_payload(self):
        self.assertEqual(classify_text("").reason, "empty_text")
        oversized = "x" * (CAPTURE_LIMIT_BYTES // 2 + 1)
        decision = classify_text(oversized)
        self.assertEqual(decision.reason, "payload_too_large")
        self.assertIsNone(decision.text)

    def test_fts_query_escapes_syntax(self):
        self.assertEqual(
            fts_literal_query('中文 "a:b" emoji🙂'),
            '"中文" AND "a" AND "b" AND "emoji🙂"',
        )
        self.assertIsNone(fts_literal_query("  :*  "))

    def test_local_content_hints_use_synthetic_positive_and_negative_examples(self):
        self.assertTrue(looks_like_secret('API_KEY="Ab3!cD9$eF4@gH8#"'))
        self.assertTrue(looks_like_secret("V7@qM2!nR8#pL5$z"))
        for ordinary in (
            "This is ordinary English copied from a page.",
            "def process_request(user_id): return user_id",
            "123e4567-e89b-12d3-a456-426614174000",
            "a" * 64,
            "API_KEY=your_api_key_here",
            '备注 API_KEY="Ab3!cD9$eF4@gH8#"',
            'Bearer V7qM2nR8pL5zAb3cD9eF4gH8 使用说明',
            "密钥V7@qM2!nR8#pL5$z",
            "\U00020000V7@qM2!nR8#pL5$z",
        ):
            self.assertFalse(looks_like_secret(ordinary))
        self.assertTrue(looks_like_link("codex://threads/synthetic-thread"))
        self.assertTrue(looks_like_link("https://example.invalid/docs"))
        self.assertFalse(looks_like_link("codex://"))
        self.assertEqual(content_kind("codex://threads/synthetic-thread", "text"), "link")

    def test_short_secret_and_recovery_hints_do_not_classify_prose_or_file_lists(self):
        for value in ("aB3$xY7!", "mR8!jT2#vQ5@", "123456-234567-345678-456789-567890-678901-789012-890123"):
            self.assertTrue(looks_like_secret(value))
        self.assertFalse(looks_like_secret("Version2024Release_Candidate_Build7"))
        self.assertFalse(looks_like_secret("Hello123"))
        self.assertFalse(looks_like_secret("key中文aB3$xY7!"))
        self.assertEqual(content_kind("API_KEY=V7@qM2!nR8#pL5$z", "file_paths"), "file_paths")
        self.assertEqual(content_kind("mailto:example@example.invalid", "text"), "link")
        self.assertFalse(looks_like_link("mailto:"))

    def test_filenames_versions_and_model_identifiers_remain_text(self):
        for value in (
            "report-2026-09-24.pdf", "IMG_20260924_123456.jpg",
            "node-v22.1.0-x64.msi", "PasswordCenter-d117c05-windows-x64.zip",
            "Release-v2.0.1", "Qwen2.5-7B-Instruct", "gpt-4o-mini-2024-07-18",
            "README.md", "python3.14.exe", "claude-opus-5-5", "v1.2.3",
            "meeting_notes-20260924.DOCX", "archive-20260924.tar.gz",
            "Tool-v12.34.5-rc.1", "Model3.2-70B-Instruct",
            "Install-PasswordCenterIndependent.ps1", "Module-v2.1.psm1",
            "clipboard-model-v2.1.ts", "app-bundle-20260924.js",
            "session-2026-09-24.log", "Backup-2026-09-24.vbk",
            "settings-v2.1.toml", "driver-2026-09.dll", "Windows11-2026-09.iso",
            "SystemBackup-20260924.vhdx",
        ):
            with self.subTest(value=value):
                self.assertEqual(content_kind(value, "text"), "text")

    def test_identifier_exceptions_preserve_secret_shapes(self):
        # Dots or a familiar extension alone must not exempt a mixed password.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmno"
        for value in (
            "Abc123!x", "P@ssw0rd2024", "Abc123!x.pdf", "P!ssw0rd2024.zip",
            "V7!qM2!nR8#pL5$z.txt", "AbC123.xYz789", "V7qM2nR8.pL5zAb3c",
            "Abc123!x.ps1", "P!ssw0rd2024.vbk", "aB3$xY7!.VHDX",
            "sk-" + "aB3dE5fG7hJ9kL2mN4pQ6rS8",
            "sk-" + "aB3dE5fG7hJ9kL2mN4pQ6rS8" + "-2024-07-18",
            "ghp_" + "aB3dE5fG7hJ9kL2mN4pQ6rS8", jwt,
            "123456-234567-345678-456789-567890-678901-789012-890123",
            'password="Abc123!x.pdf"',
        ):
            with self.subTest(value=value):
                self.assertEqual(content_kind(value, "text"), "secret_like")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "clipboard.sqlite3"
        self.store = ClipboardStore(self.db)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def capture(self, text, sequence, payload_type="text", restore=None):
        return self.store.record_capture(
            collector_instance_id="collector-a",
            boot_id="boot-a",
            session_id="session-a",
            source_instance_id="windows:test",
            clipboard_sequence=sequence,
            payload_type=payload_type,
            text=text,
            observation_kind="history_restore" if restore else "copy",
            restored_from_event_id=restore,
            restore_request_id="request-a" if restore else None,
        )

    def test_duplicate_payloads_keep_distinct_events_and_reuse_blob(self):
        first = self.capture("重复🙂", 10)
        second = self.capture("重复🙂", 11)
        self.assertNotEqual(first, second)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("select count(*) from events").fetchone()[0], 2)
            self.assertEqual(conn.execute("select count(*) from blobs").fetchone()[0], 1)
        finally:
            conn.close()

    def test_fts_pagination_restore_lineage_and_read_only_viewer(self):
        original = self.capture("第一条 中文🙂", 20)
        self.capture("第二条 中文🙂", 21)
        restored = self.capture("第一条 中文🙂", 22, restore=original)

        viewer = ReadOnlyClipboardStore(self.db)
        try:
            page = viewer.search(query='中文🙂', limit=1, offset=0)
            self.assertEqual(len(page), 1)
            next_page = viewer.search(query='中文🙂', limit=1, offset=1)
            self.assertEqual(len(next_page), 1)
            detail = viewer.get_event(restored)
            self.assertEqual(detail["restored_from_event_id"], original)
            self.assertEqual(detail["text"], "第一条 中文🙂")
            with self.assertRaises(sqlite3.OperationalError):
                viewer.connection.execute("delete from events")
        finally:
            viewer.close()

    def test_grouped_history_filters_before_count_and_pages_all_content(self):
        http = "https://example.invalid/docs"
        codex = "codex://threads/synthetic-thread"
        self.capture(http, 40, "url")
        self.capture(http, 40, "url")  # repeated notification, one sequence
        repeat = self.capture(http, 41, "url")
        self.capture(codex, 42)
        self.capture("alpha", 43)
        self.capture("alpha ", 44)
        for index in range(51):
            self.capture(f"item {index:03d}", index + 45)
        repeat_at = self.store.connection.execute(
            "SELECT observed_at_utc FROM events WHERE event_id=?", (repeat,)
        ).fetchone()[0]
        viewer = ReadOnlyClipboardStore(self.db)
        try:
            first, total = viewer.search_grouped(limit=50)
            last, last_total = viewer.search_grouped(limit=50, offset=50)
            self.assertEqual((total, last_total, len(first), len(last)), (55, 55, 50, 5))
            self.assertEqual(len({row["event_id"] for row in first + last}), 55)
            links, link_total = viewer.search_grouped(content_filter="link", limit=10)
            self.assertEqual(link_total, 2)
            by_preview = {row["preview"]: row for row in links}
            self.assertEqual((by_preview[http]["payload_type"],
                              by_preview[http]["copy_count"],
                              by_preview[http]["content_kind"]), ("url", 2, "link"))
            self.assertEqual((by_preview[codex]["payload_type"],
                              by_preview[codex]["content_kind"]), ("text", "link"))
            dated, dated_total = viewer.search_grouped(
                content_filter="link", payload_type="url", date_from=repeat_at,
                limit=10,
            )
            self.assertEqual((dated_total, dated[0]["copy_count"]), (1, 1))
            alpha, alpha_total = viewer.search_grouped(query="alpha", limit=10)
            self.assertEqual((alpha_total, {row["preview"] for row in alpha}),
                             (2, {"alpha", "alpha "}))
        finally:
            viewer.close()

    def test_secret_filter_counts_distinct_sequences_and_history_recopy(self):
        secret = "V7@qM2!nR8#pL5$z"
        original = self.capture(secret, 70)
        self.capture(secret, 70)  # same clipboard state observed twice
        self.capture(secret, 71, restore=original)
        self.capture("ordinary English sentence", 72)
        viewer = ReadOnlyClipboardStore(self.db)
        try:
            rows, total = viewer.search_grouped(content_filter="secret_like", limit=10)
            self.assertEqual(total, 1)
            self.assertEqual((rows[0]["copy_count"], rows[0]["content_kind"]),
                             (2, "secret_like"))
            self.assertEqual(rows[0]["observation_kind"], "history_restore")
        finally:
            viewer.close()

    def test_grouped_categories_partition_latest_groups_before_counting_and_paging(self):
        self.capture("plain text", 80)
        self.capture("https://example.invalid", 81, "url")
        self.capture("mailto:example@example.invalid", 82)
        self.capture("aB3$xY7!", 83)
        self.capture("E:\\synthetic\\file.txt", 84, "file_paths")
        # Same bytes, different clipboard format: the newest group presentation
        # determines one category, but both real copy events remain counted.
        self.capture("E:\\synthetic\\file.txt", 85, "text")
        viewer = ReadOnlyClipboardStore(self.db)
        try:
            all_rows, all_total = viewer.search_grouped()
            group_ids = []
            for kind, expected in (("text", 2), ("link", 2), ("secret_like", 1), ("file_paths", 0)):
                pages = []
                for offset in range(expected + 1):
                    rows, total = viewer.search_grouped(content_filter=kind, offset=offset, limit=1)
                    self.assertEqual(total, expected)
                    self.assertTrue(all(row["content_kind"] == kind for row in rows))
                    pages += rows
                self.assertEqual(len(pages), expected)
                group_ids += [row["event_id"] for row in pages]
            self.assertEqual(len(set(group_ids)), all_total)
            self.assertEqual(set(group_ids), {row["event_id"] for row in all_rows})
            file_row = next(row for row in all_rows if row["preview"].startswith("E:"))
            self.assertEqual(file_row["copy_count"], 2)
        finally:
            viewer.close()

    def test_identifier_and_secret_filters_partition_synthetic_history(self):
        identifiers = ("report-2026-09-24.pdf", "Qwen2.5-7B-Instruct", "Release-v2.0.1")
        secrets = ("Abc123!x.pdf", "P@ssw0rd2024")
        for sequence, value in enumerate(identifiers + secrets, start=90):
            self.capture(value, sequence)
        self.capture(identifiers[0], 95)
        viewer = ReadOnlyClipboardStore(self.db)
        try:
            for kind, expected in (("text", identifiers), ("secret_like", secrets)):
                rows, total = viewer.search_grouped(content_filter=kind)
                self.assertEqual(total, len(expected))
                self.assertEqual({row["preview"] for row in rows}, set(expected))
                if kind == "text":
                    repeated = next(row for row in rows if row["preview"] == identifiers[0])
                    self.assertEqual(repeated["copy_count"], 2)
        finally:
            viewer.close()

    def test_gap_unsupported_and_schema_upgrade_noop(self):
        self.store.record_gap(
            collector_instance_id="collector-a",
            boot_id="boot-a",
            session_id="session-a",
            source_instance_id="windows:test",
            clipboard_sequence=30,
            reason="sequence_gap",
            gap_count=2,
        )
        self.store.record_skip(
            collector_instance_id="collector-a",
            boot_id="boot-a",
            session_id="session-a",
            source_instance_id="windows:test",
            clipboard_sequence=31,
            reason="unsupported_format",
        )
        before = self.db.stat().st_size
        self.store.initialize()
        self.assertGreaterEqual(self.db.stat().st_size, before)
        self.assertEqual(self.store.schema_version(), 1)
        exported = self.store.connection.execute(
            """
            select event_kind,reason
            from adapter_events_v1
            order by observed_at_utc,event_id
            """
        ).fetchall()
        self.assertCountEqual(
            exported,
            [("gap", "sequence_gap"), ("skip", "unsupported_format")],
        )

    def test_schema_zero_upgrades_to_v1(self):
        legacy = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        try:
            connection.execute("create table meta(key text primary key,value text not null)")
            connection.execute("insert into meta values('schema_version','0')")
            connection.commit()
        finally:
            connection.close()
        upgraded = ClipboardStore(legacy)
        try:
            upgraded.initialize()
            self.assertEqual(upgraded.schema_version(), 1)
            self.assertIsNotNone(
                upgraded.connection.execute(
                    "select 1 from sqlite_master where name='adapter_events_v1'"
                ).fetchone()
            )
        finally:
            upgraded.close()


class ControlTests(unittest.TestCase):
    def test_pause_state_is_atomic_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.json"
            self.assertEqual(read_control_state(path), ControlState(paused=False, generation=0))
            state = write_control_state(path, paused=True)
            self.assertTrue(state.paused)
            persisted = read_control_state(path)
            self.assertEqual(persisted, state)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], "timeaudit.clipboard-control.v1")


class BackupTests(unittest.TestCase):
    def test_online_backup_verify_and_restore_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            backup = base / "backup"
            restored = base / "restored"
            store = ClipboardStore(source / "clipboard_history.sqlite3")
            store.initialize()
            try:
                store.record_capture(
                    collector_instance_id="collector-a",
                    boot_id="boot-a",
                    session_id="session-a",
                    source_instance_id="windows:test",
                    clipboard_sequence=1,
                    payload_type="text",
                    text="private synthetic payload",
                )
                manifest = create_backup(source, backup)
            finally:
                store.close()
            self.assertEqual(manifest["event_count"], 1)
            self.assertTrue(verify_backup(backup)["valid"])
            receipt = restore_backup(backup, restored)
            self.assertTrue(receipt["valid"])
            self.assertEqual(receipt["event_count"], 1)


class AdapterStdioTests(unittest.TestCase):
    def test_versioned_json_stdio_checkpoint_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ClipboardStore(root / "clipboard_history.sqlite3")
            store.initialize()
            store.register_source_instance("windows:adapter-test")
            try:
                first = store.record_capture(
                    collector_instance_id="collector-a",
                    boot_id="boot-a",
                    session_id="session-a",
                    source_instance_id="windows:adapter-test",
                    clipboard_sequence=10,
                    payload_type="text",
                    text="adapter synthetic payload",
                )
                second = store.record_capture(
                    collector_instance_id="collector-a",
                    boot_id="boot-a",
                    session_id="session-a",
                    source_instance_id="windows:adapter-test",
                    clipboard_sequence=11,
                    payload_type="text",
                    text="adapter synthetic payload",
                )
                store.record_gap(
                    collector_instance_id="collector-a",
                    boot_id="boot-a",
                    session_id="session-a",
                    source_instance_id="windows:adapter-test",
                    clipboard_sequence=12,
                    reason="synthetic_gap",
                    gap_count=None,
                )
            finally:
                store.close()

            request = {
                "schema": "timeaudit.clipboard-export.request.v1",
                "action": "export",
                "checkpoint": None,
                "limit": 2,
                "include_payload": True,
            }
            result = self._invoke(root, request)
            self.assertEqual(result.returncode, 0)
            response = json.loads(result.stdout)
            self.assertEqual(response["schema"], "timeaudit.clipboard-export.response.v1")
            self.assertEqual(response["source_profile_key"], "src.timeaudit.windows_clipboard")
            self.assertNotEqual(response["source_profile_key"], "src.timeaudit.pc_activity")
            self.assertEqual(response["source_instance_id"], "windows:adapter-test")
            self.assertEqual([event["event_id"] for event in response["events"]], [first, second])
            self.assertTrue(response["has_more"])
            self.assertEqual(
                response["events"][0]["collector_instance_id"], "collector-a"
            )
            self.assertEqual(response["events"][0]["session_id"], "session-a")
            self.assertEqual(response["events"][0]["clipboard_sequence"], 10)
            self.assertEqual(
                response["events"][0]["payload"]["text"], "adapter synthetic payload"
            )

            next_request = {
                **request,
                "checkpoint": response["next_checkpoint"],
                "include_payload": False,
            }
            next_result = self._invoke(root, next_request)
            self.assertEqual(next_result.returncode, 0)
            next_response = json.loads(next_result.stdout)
            self.assertEqual(len(next_response["events"]), 1)
            self.assertEqual(next_response["events"][0]["event_kind"], "gap")
            self.assertNotIn("payload", next_response["events"][0])
            self.assertFalse(next_response["has_more"])

    def test_invalid_stdio_request_has_stable_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ClipboardStore(root / "clipboard_history.sqlite3")
            store.initialize()
            store.close()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "clipboard_history.adapter_stdio",
                    "--data-root",
                    str(root),
                ],
                input=b'{"schema":"wrong"}\n',
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            response = json.loads(result.stdout)
            self.assertEqual(response["schema"], "timeaudit.clipboard-export.error.v1")
            self.assertNotIn(b"Traceback", result.stderr)

    @staticmethod
    def _invoke(root: Path, request: dict):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "clipboard_history.adapter_stdio",
                "--data-root",
                str(root),
            ],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
