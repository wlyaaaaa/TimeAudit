import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clipboard_history.control import ControlState, read_control_state, write_control_state
from clipboard_history.backup import create_backup, restore_backup, verify_backup
from clipboard_history.model import (
    CAPTURE_LIMIT_BYTES,
    CaptureDecision,
    classify_text,
    fts_literal_query,
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
        self.assertEqual(exported, [("gap", "sequence_gap"), ("skip", "unsupported_format")])

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


if __name__ == "__main__":
    unittest.main()
