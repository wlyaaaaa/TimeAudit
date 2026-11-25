"""Pending samples owned by the existing TimeAudit collector."""

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4


class PendingTelemetry:
    MAX_DIRECT_PENDING = 4

    def __init__(self, root):
        self.root = Path(root)
        # The collector already holds its singleton. Any .tmp left by the
        # previous process never reached atomic publication.
        for temporary in self.root.glob("*.tmp"):
            temporary.unlink()
        self.pending = deque(sorted(self.root.glob("*.json")))
        self._direct = False
        self._buffered = 0

    def record(self, kind, timestamp, rows):
        sample = (kind, timestamp, rows)
        if self._direct and self._buffered < self.MAX_DIRECT_PENDING:
            self.pending.append(sample)
            self._buffered += 1
            return
        self.defer()
        self.pending.append(self._save(sample))

    def allow_direct(self):
        if not self.pending:
            self._direct = True

    def defer(self):
        """Protect the unconfirmed head as well as samples behind it."""
        self._direct = False
        if self._buffered:
            for index, sample in enumerate(self.pending):
                if isinstance(sample, tuple):
                    self.pending[index] = self._save(sample)
                    self._buffered -= 1

    def _save(self, sample):
        kind, timestamp, rows = sample
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{timestamp:%Y%m%dT%H%M%S%f}-{uuid4().hex}.json"
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump([kind, timestamp.isoformat(), rows], handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def first(self):
        item = self.pending[0]
        if isinstance(item, tuple):
            return item
        kind, stamp, rows = json.loads(item.read_text(encoding="utf-8"))
        return kind, datetime.fromisoformat(stamp), rows

    def acknowledge(self):
        item = self.pending[0]
        if isinstance(item, tuple):
            self._buffered -= 1
        else:
            item.unlink()
        self.pending.popleft()
