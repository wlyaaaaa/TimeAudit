"""Pending samples owned by the existing TimeAudit collector."""

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import uuid4


class PendingTelemetry:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # The collector already holds its singleton. Any .tmp left by the
        # previous process never reached atomic publication.
        for temporary in self.root.glob("*.tmp"):
            temporary.unlink()
        self.pending = deque(sorted(self.root.glob("*.json")))

    def record(self, kind, timestamp, rows):
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
        self.pending.append(path)

    def first(self):
        path = self.pending[0]
        kind, stamp, rows = json.loads(path.read_text(encoding="utf-8"))
        return kind, datetime.fromisoformat(stamp), rows

    def acknowledge(self):
        self.pending[0].unlink()
        self.pending.popleft()
