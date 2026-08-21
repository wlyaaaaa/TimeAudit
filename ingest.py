"""Lossless CSV-to-PostgreSQL sidecar for TimeAudit screen-time events.

The AHK collector owns the source CSV.  This process only rotates immutable
spool segments, validates a whole segment, and removes it after a committed
database transaction.  Heartbeats contain operational metadata only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from pathlib import Path


DB_HOST = os.environ.get("TIMEAUDIT_DB_HOST", "audit-db")
DB_USER = os.environ.get("TIMEAUDIT_DB_USER", "leyang")
DB_PASS = os.environ["TIMEAUDIT_DB_PASSWORD"]
DB_NAME = os.environ.get("TIMEAUDIT_DB_NAME", "time_audit")
CSV_PATH = Path(os.environ.get("TIMEAUDIT_CSV_PATH", "/log/buffer.csv"))
HEARTBEAT_PATH = Path(
    os.environ.get("TIMEAUDIT_INGEST_HEARTBEAT", "/log/ingester_heartbeat.json")
)
LOOP_SECONDS = 10


class CsvPayloadError(ValueError):
    """A spool segment is not fully parseable and must remain untouched."""


def source_event_id(row: tuple[str, int, str, str]) -> str:
    canonical = json.dumps(
        [row[0], row[1], row[2], row[3]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def connect_db():
    import psycopg

    return psycopg.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        dbname=DB_NAME,
        connect_timeout=5,
        options="-c statement_timeout=15000 -c lock_timeout=5000",
        application_name="timeaudit_screen_ingester",
    )


def init_db() -> None:
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_usage_logs (
                    id BIGSERIAL PRIMARY KEY,
                    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    duration_seconds INT NOT NULL,
                    process_name VARCHAR(100) NOT NULL,
                    window_title TEXT,
                    source_event_id CHAR(64)
                );
                ALTER TABLE app_usage_logs
                    ALTER COLUMN window_title TYPE TEXT;
                ALTER TABLE app_usage_logs
                    ADD COLUMN IF NOT EXISTS source_event_id CHAR(64);
                CREATE INDEX IF NOT EXISTS idx_logs_start_time
                    ON app_usage_logs (start_time);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_logs_source_event_id
                    ON app_usage_logs (source_event_id)
                    WHERE source_event_id IS NOT NULL;
                """
            )


def processing_path(csv_path: Path = CSV_PATH) -> Path:
    return csv_path.with_name(
        f"{csv_path.name}.{time.time_ns()}.{os.getpid()}.processing"
    )


def rotate_source(csv_path: Path = CSV_PATH) -> Path | None:
    try:
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            return None
        target = processing_path(csv_path)
        csv_path.rename(target)
        return target
    except (FileNotFoundError, PermissionError, OSError):
        return None


def pending_segments(csv_path: Path = CSV_PATH) -> list[Path]:
    legacy = csv_path.with_name(f"{csv_path.name}.processing")
    pending = list(csv_path.parent.glob(f"{csv_path.name}.*.processing"))
    if legacy.exists():
        pending.append(legacy)
    return sorted(set(pending), key=lambda path: path.name)


def parse_segment(path: Path) -> list[tuple[str, int, str, str]]:
    rows: list[tuple[str, int, str, str]] = []
    try:
        raw = path.read_bytes()
        # Legacy spool segments can contain NUL padding around otherwise valid
        # UTF-8 CSV. PostgreSQL text cannot store U+0000, so remove only those
        # invalid padding bytes and keep the immutable segment until the whole
        # sanitized payload validates and commits successfully.
        cleaned = raw.replace(b"\x00", b"")
        if raw and not cleaned:
            raise CsvPayloadError("nul_only_segment")
        text = cleaned.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""))
        for row_number, row in enumerate(reader, start=1):
            if len(row) != 4:
                raise CsvPayloadError(f"invalid_column_count_at_row_{row_number}")
            try:
                duration = int(row[1])
            except ValueError as exc:
                raise CsvPayloadError(
                    f"invalid_duration_at_row_{row_number}"
                ) from exc
            if duration < 0:
                raise CsvPayloadError(f"negative_duration_at_row_{row_number}")
            rows.append((row[0], duration, row[2], row[3]))
    except UnicodeDecodeError as exc:
        raise CsvPayloadError("invalid_utf8") from exc
    except csv.Error as exc:
        raise CsvPayloadError("invalid_csv") from exc
    return rows


def insert_segment(path: Path) -> int:
    rows = parse_segment(path)
    if not rows:
        path.unlink()
        return 0

    values = [(*row, source_event_id(row)) for row in rows]
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO app_usage_logs (
                    start_time,
                    duration_seconds,
                    process_name,
                    window_title,
                    source_event_id
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source_event_id)
                    WHERE source_event_id IS NOT NULL
                    DO NOTHING
                """,
                values,
            )
    path.unlink()
    return len(rows)


def pending_metadata(csv_path: Path = CSV_PATH) -> tuple[int, int]:
    files = pending_segments(csv_path)
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
    try:
        if csv_path.exists():
            total_bytes += csv_path.stat().st_size
    except OSError:
        pass
    return len(files), total_bytes


def write_heartbeat(
    *,
    state: str,
    last_batch_rows: int,
    error_kind: str | None = None,
    heartbeat_path: Path = HEARTBEAT_PATH,
    csv_path: Path = CSV_PATH,
) -> None:
    pending_files, pending_bytes = pending_metadata(csv_path)
    payload = {
        "schema": "timeaudit.ingester-heartbeat.v1",
        "updated_at_unix_ms": int(time.time() * 1000),
        "state": state,
        "pending_files": pending_files,
        "pending_bytes": pending_bytes,
        "last_batch_rows": last_batch_rows,
        "error_kind": error_kind,
    }
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = heartbeat_path.with_name(
        f"{heartbeat_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="ascii",
    )
    os.replace(temporary, heartbeat_path)


def sync_pipeline(csv_path: Path = CSV_PATH) -> tuple[int, str | None]:
    rotate_source(csv_path)
    inserted = 0
    first_error: str | None = None
    for path in pending_segments(csv_path):
        try:
            inserted += insert_segment(path)
        except Exception as exc:
            if first_error is None:
                first_error = type(exc).__name__
            # Keep the immutable segment for a later bounded retry.  Do not
            # print exception text because drivers can include private fields.
    return inserted, first_error


def run_once(*, initialized: bool) -> bool:
    state = "healthy"
    error_kind = None
    inserted = 0
    try:
        if not initialized:
            init_db()
            initialized = True
        inserted, error_kind = sync_pipeline()
        if error_kind:
            state = "degraded"
    except Exception as exc:
        state = "degraded"
        error_kind = type(exc).__name__
        initialized = False
    write_heartbeat(
        state=state,
        last_batch_rows=inserted,
        error_kind=error_kind,
    )
    print(
        json.dumps(
            {
                "component": "timeaudit_screen_ingester",
                "state": state,
                "rows": inserted,
                "error_kind": error_kind,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return initialized


def main() -> None:
    initialized = False
    while True:
        try:
            initialized = run_once(initialized=initialized)
        except Exception as exc:
            # Heartbeat write failures must not terminate the sidecar.
            print(
                json.dumps(
                    {
                        "component": "timeaudit_screen_ingester",
                        "state": "heartbeat_error",
                        "error_kind": type(exc).__name__,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
