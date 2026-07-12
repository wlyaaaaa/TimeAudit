"""Small, dependency-free helpers shared by the collector and its watchdog tests."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
HEARTBEAT_FILE = ROOT / "log" / "telemetry_heartbeat"


def bounded_backoff_seconds(
    attempt: int,
    base_seconds: int = 60,
    max_seconds: int = 300,
) -> int:
    """Return an exponential retry delay capped at a practical upper bound."""
    exponent = max(0, min(int(attempt) - 1, 30))
    return min(max_seconds, base_seconds * (2**exponent))


def _normalized_path(path: os.PathLike[str] | str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(str(path).strip('"')))
    return os.path.normcase(os.path.abspath(expanded))


def command_line_targets_script(
    command_line: Iterable[str] | None,
    script_path: os.PathLike[str] | str,
) -> bool:
    """Return True only when a command-line argument is this exact script path."""
    target = _normalized_path(script_path)
    for argument in command_line or ():
        if argument and _normalized_path(argument) == target:
            return True
    return False


def write_telemetry_heartbeat(
    path: os.PathLike[str] | str = HEARTBEAT_FILE,
) -> bool:
    """Atomically record the last successful telemetry database batch."""
    target = Path(path)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        temporary.write_text(timestamp + "\n", encoding="ascii")
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
