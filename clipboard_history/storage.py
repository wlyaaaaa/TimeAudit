from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CONTRACT_VERSION, SCHEMA_VERSION
from .model import fts_literal_query, payload_sha256


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS blobs (
    blob_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    content_text TEXT NOT NULL,
    utf8_bytes INTEGER NOT NULL CHECK (utf8_bytes >= 0),
    created_at_utc TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    observed_at_utc TEXT NOT NULL,
    collector_instance_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_instance_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    clipboard_sequence INTEGER,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN ('observation', 'skip', 'gap', 'boundary')
    ),
    observation_kind TEXT,
    payload_type TEXT,
    blob_id TEXT REFERENCES blobs(blob_id),
    reason TEXT,
    gap_count INTEGER,
    restored_from_event_id TEXT,
    restore_request_id TEXT
);

CREATE INDEX IF NOT EXISTS events_observed_desc
ON events(observed_at_utc DESC, event_id DESC);

CREATE INDEX IF NOT EXISTS events_type_observed_desc
ON events(payload_type, observed_at_utc DESC, event_id DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    event_id UNINDEXED,
    content,
    tokenize='unicode61'
);

CREATE VIEW IF NOT EXISTS adapter_events_v1 AS
SELECT
    e.event_id,
    e.observed_at_utc,
    e.collector_instance_id,
    e.boot_id,
    e.session_id,
    e.source_instance_id,
    e.contract_version,
    e.clipboard_sequence,
    e.event_kind,
    e.observation_kind,
    e.payload_type,
    e.reason,
    e.gap_count,
    e.restored_from_event_id,
    e.restore_request_id,
    b.sha256 AS payload_sha256,
    b.content_text AS payload_text,
    b.utf8_bytes AS payload_utf8_bytes
FROM events e
LEFT JOIN blobs b ON b.blob_id=e.blob_id;

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events_append_only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events_append_only');
END;

CREATE TRIGGER IF NOT EXISTS blobs_no_update
BEFORE UPDATE ON blobs BEGIN
    SELECT RAISE(ABORT, 'blobs_append_only');
END;

CREATE TRIGGER IF NOT EXISTS blobs_no_delete
BEFORE DELETE ON blobs BEGIN
    SELECT RAISE(ABORT, 'blobs_append_only');
END;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class ClipboardStore:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.connection is None:
            self.connection = sqlite3.connect(self.database, timeout=5.0)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
        connection = self._connection()
        try:
            existing = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            existing = None
        if existing is not None and int(existing[0]) > SCHEMA_VERSION:
            raise RuntimeError("schema_too_new")
        with connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('contract_version',?)",
                (CONTRACT_VERSION,),
            )

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("store_not_initialized")
        return self.connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def schema_version(self) -> int:
        row = self._connection().execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0])

    def register_source_instance(self, source_instance_id: str) -> None:
        if not source_instance_id.startswith("windows:"):
            raise ValueError("invalid_source_instance")
        with self._connection():
            self._connection().execute(
                """
                INSERT OR REPLACE INTO meta(key,value)
                VALUES('source_instance_id',?)
                """,
                (source_instance_id,),
            )

    def record_capture(
        self,
        *,
        collector_instance_id: str,
        boot_id: str,
        session_id: str,
        source_instance_id: str,
        clipboard_sequence: int,
        payload_type: str,
        text: str,
        observation_kind: str = "copy",
        restored_from_event_id: str | None = None,
        restore_request_id: str | None = None,
    ) -> str:
        connection = self._connection()
        event_id = f"evt_{uuid.uuid4().hex}"
        digest = payload_sha256(text)
        blob_id = f"sha256:{digest}"
        observed = _utc_now()
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO blobs(
                    blob_id,sha256,content_text,utf8_bytes,created_at_utc
                ) VALUES(?,?,?,?,?)
                """,
                (blob_id, digest, text, len(text.encode("utf-8")), observed),
            )
            connection.execute(
                """
                INSERT INTO events(
                    event_id,observed_at_utc,collector_instance_id,boot_id,
                    session_id,source_instance_id,contract_version,
                    clipboard_sequence,event_kind,observation_kind,payload_type,
                    blob_id,restored_from_event_id,restore_request_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    observed,
                    collector_instance_id,
                    boot_id,
                    session_id,
                    source_instance_id,
                    CONTRACT_VERSION,
                    clipboard_sequence,
                    "observation",
                    observation_kind,
                    payload_type,
                    blob_id,
                    restored_from_event_id,
                    restore_request_id,
                ),
            )
            connection.execute(
                "INSERT INTO content_fts(event_id,content) VALUES(?,?)",
                (event_id, text),
            )
        return event_id

    def _record_nonpayload(
        self,
        *,
        event_kind: str,
        collector_instance_id: str,
        boot_id: str,
        session_id: str,
        source_instance_id: str,
        clipboard_sequence: int | None,
        reason: str,
        gap_count: int | None = None,
    ) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        with self._connection():
            self._connection().execute(
                """
                INSERT INTO events(
                    event_id,observed_at_utc,collector_instance_id,boot_id,
                    session_id,source_instance_id,contract_version,
                    clipboard_sequence,event_kind,reason,gap_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    _utc_now(),
                    collector_instance_id,
                    boot_id,
                    session_id,
                    source_instance_id,
                    CONTRACT_VERSION,
                    clipboard_sequence,
                    event_kind,
                    reason,
                    gap_count,
                ),
            )
        return event_id

    def record_skip(self, **kwargs: Any) -> str:
        return self._record_nonpayload(event_kind="skip", **kwargs)

    def record_gap(self, **kwargs: Any) -> str:
        return self._record_nonpayload(event_kind="gap", **kwargs)

    def record_boundary(self, **kwargs: Any) -> str:
        return self._record_nonpayload(event_kind="boundary", **kwargs)

    def validate_restore(
        self, event_id: str, expected_text: str
    ) -> bool:
        row = self._connection().execute(
            """
            SELECT b.sha256
            FROM events e JOIN blobs b ON b.blob_id=e.blob_id
            WHERE e.event_id=?
            """,
            (event_id,),
        ).fetchone()
        return row is not None and row[0] == payload_sha256(expected_text)


class ReadOnlyClipboardStore:
    def __init__(self, database: Path):
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=3.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("PRAGMA busy_timeout=3000")
        version = self.connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if version is None or int(version[0]) != SCHEMA_VERSION:
            self.close()
            raise RuntimeError("unsupported_schema")
        fts = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone()
        if fts is None:
            self.close()
            raise RuntimeError("fts5_required")

    def close(self) -> None:
        self.connection.close()

    def search(
        self,
        *,
        query: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        payload_type: str | None = None,
        include_restores: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200 or offset < 0:
            raise ValueError("invalid_pagination")
        clauses = ["e.event_kind='observation'"]
        params: list[Any] = []
        join = ""
        fts_query = fts_literal_query(query)
        if query and fts_query is None:
            return []
        if fts_query:
            join = "JOIN content_fts f ON f.event_id=e.event_id"
            clauses.append("content_fts MATCH ?")
            params.append(fts_query)
        if date_from:
            clauses.append("e.observed_at_utc>=?")
            params.append(date_from)
        if date_to:
            clauses.append("e.observed_at_utc<?")
            params.append(date_to)
        if payload_type:
            clauses.append("e.payload_type=?")
            params.append(payload_type)
        if not include_restores:
            clauses.append("e.observation_kind<>'history_restore'")
        params.extend((limit, offset))
        sql = f"""
            SELECT e.event_id,e.observed_at_utc,e.clipboard_sequence,
                   e.observation_kind,e.payload_type,e.restored_from_event_id,
                   substr(replace(b.content_text,char(10),' '),1,160) AS preview
            FROM events e
            JOIN blobs b ON b.blob_id=e.blob_id
            {join}
            WHERE {' AND '.join(clauses)}
            ORDER BY e.observed_at_utc DESC,e.event_id DESC
            LIMIT ? OFFSET ?
        """
        return [dict(row) for row in self.connection.execute(sql, params)]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT e.*,b.content_text AS text,b.sha256,b.utf8_bytes
            FROM events e LEFT JOIN blobs b ON b.blob_id=e.blob_id
            WHERE e.event_id=?
            """,
            (event_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def status_counts(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE event_kind='observation') AS observations,
                count(*) FILTER (WHERE event_kind='gap') AS gaps,
                count(*) FILTER (WHERE event_kind='skip') AS skips
            FROM events
            """
        ).fetchone()
        return dict(row)

    def source_instance_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key='source_instance_id'"
        ).fetchone()
        return str(row[0]) if row is not None else None

    def export_after(
        self,
        *,
        checkpoint_observed_at_utc: str | None,
        checkpoint_event_id: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if limit < 1 or limit > 500:
            raise ValueError("invalid_limit")
        if (checkpoint_observed_at_utc is None) != (checkpoint_event_id is None):
            raise ValueError("invalid_checkpoint")
        clauses: list[str] = []
        params: list[Any] = []
        if checkpoint_observed_at_utc is not None:
            clauses.append("(observed_at_utc,event_id)>(?,?)")
            params.extend((checkpoint_observed_at_utc, checkpoint_event_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)
        rows = [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT *
                FROM adapter_events_v1
                {where}
                ORDER BY observed_at_utc,event_id
                LIMIT ?
                """,
                params,
            )
        ]
        return rows[:limit], len(rows) > limit
