import csv
import json
from pathlib import Path

import ingest
from ingester_healthcheck import heartbeat_status


ROOT = Path(__file__).resolve().parent


def _write_segment(path: Path, rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)


def test_long_unicode_title_is_lossless_and_hash_is_observation_stable(tmp_path):
    title = "中文🙂\n" + ("x" * 1685)
    path = tmp_path / "buffer.csv.1.1.processing"
    _write_segment(path, [["2026-07-26 06:00:00+0800", 7, "example.exe", title]])

    row = ingest.parse_segment(path)[0]
    assert row[3] == title
    assert len(row[3]) > 500
    assert ingest.source_event_id(row) == ingest.source_event_id(row)

    later = ("2026-07-26 06:00:01+0800", row[1], row[2], row[3])
    assert ingest.source_event_id(later) != ingest.source_event_id(row)


def test_invalid_segment_is_rejected_as_a_whole(tmp_path):
    path = tmp_path / "buffer.csv.1.1.processing"
    _write_segment(
        path,
        [
            ["2026-07-26 06:00:00+0800", 7, "example.exe", "valid"],
            ["bad", "not-an-int", "example.exe", "private"],
        ],
    )

    try:
        ingest.parse_segment(path)
    except ingest.CsvPayloadError as exc:
        assert "row_2" in str(exc)
    else:
        raise AssertionError("malformed segment was accepted")
    assert path.exists()


def test_rotation_never_overwrites_existing_processing_segment(tmp_path):
    source = tmp_path / "buffer.csv"
    source.write_text("new", encoding="utf-8")
    legacy = tmp_path / "buffer.csv.processing"
    legacy.write_text("old", encoding="utf-8")

    rotated = ingest.rotate_source(source)

    assert rotated is not None and rotated.exists()
    assert rotated != legacy
    assert legacy.read_text(encoding="utf-8") == "old"
    assert len(ingest.pending_segments(source)) == 2


def test_ingester_heartbeat_is_atomic_and_health_is_bounded(tmp_path):
    heartbeat = tmp_path / "ingester_heartbeat.json"
    source = tmp_path / "buffer.csv"
    ingest.write_heartbeat(
        state="healthy",
        last_batch_rows=3,
        heartbeat_path=heartbeat,
        csv_path=source,
    )
    payload = json.loads(heartbeat.read_text(encoding="ascii"))
    now_ms = payload["updated_at_unix_ms"]

    assert heartbeat_status(heartbeat, now_ms=now_ms) == (True, "healthy")
    assert heartbeat_status(
        heartbeat, now_ms=now_ms + 46_000, max_age_seconds=45
    ) == (False, "stale")
    assert not list(tmp_path.glob("*.tmp"))


def test_ingester_contract_has_bounded_io_idempotency_and_text_schema():
    source = (ROOT / "ingest.py").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "connect_timeout=5" in source
    assert "statement_timeout=15000" in source
    assert "lock_timeout=5000" in source
    assert "window_title TEXT" in source
    assert "source_event_id CHAR(64)" in source
    assert "ON CONFLICT (source_event_id)" in source
    assert "ingester_healthcheck.py:/app/ingester_healthcheck.py:ro" in compose
    assert "grafana/grafana-oss:13.0.2" in compose
    assert "healthcheck:" in compose
    assert "TIMEAUDIT_DB_PASSWORD is required" in compose
    assert 'os.environ["TIMEAUDIT_DB_PASSWORD"]' in source


if __name__ == "__main__":
    test_long_unicode_title_is_lossless_and_hash_is_observation_stable(
        Path.cwd() / ".test-ingest-tmp"
    )
