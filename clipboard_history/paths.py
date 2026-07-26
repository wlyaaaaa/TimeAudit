from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "PersonalOS"
    / "ClipboardHistory"
)
DEFAULT_BACKUP_ROOT = (
    Path(os.environ["PERSONALOS_CLIPBOARD_BACKUP_ROOT"])
    if os.environ.get("PERSONALOS_CLIPBOARD_BACKUP_ROOT")
    else None
)


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    database: Path
    control: Path
    heartbeat: Path
    collector_state: Path


def runtime_paths(root: str | os.PathLike[str] | None = None) -> RuntimePaths:
    resolved = Path(root) if root else Path(
        os.environ.get("PERSONALOS_CLIPBOARD_DATA_ROOT", DEFAULT_DATA_ROOT)
    )
    return RuntimePaths(
        root=resolved,
        database=resolved / "clipboard_history.sqlite3",
        control=resolved / "control.json",
        heartbeat=resolved / "heartbeat.json",
        collector_state=resolved / "collector-state.json",
    )
