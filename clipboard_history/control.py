from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTROL_SCHEMA = "timeaudit.clipboard-control.v1"
STATUS_SCHEMA = "timeaudit.clipboard-status.v1"


@dataclass(frozen=True)
class ControlState:
    paused: bool
    generation: int


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_control_state(path: Path) -> ControlState:
    if not path.exists():
        return ControlState(paused=False, generation=0)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value.get("schema") != CONTROL_SCHEMA
            or type(value.get("paused")) is not bool
            or type(value.get("generation")) is not int
            or value["generation"] < 0
        ):
            raise ValueError("invalid_control_state")
        return ControlState(paused=value["paused"], generation=value["generation"])
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ControlState(paused=True, generation=0)


def write_control_state(path: Path, paused: bool) -> ControlState:
    current = read_control_state(path)
    state = ControlState(paused=paused, generation=current.generation + 1)
    _atomic_json(
        path,
        {
            "schema": CONTROL_SCHEMA,
            "paused": state.paused,
            "generation": state.generation,
            "updated_at_unix_ms": int(time.time() * 1000),
        },
    )
    return state


def write_status(path: Path, **fields: Any) -> None:
    allowed = {
        "state",
        "reason",
        "collector_instance_id",
        "boot_id",
        "session_id",
        "source_instance_id",
        "last_sequence",
        "last_observation_at_utc",
        "paused",
        "pid",
        "schema_version",
    }
    value = {
        "schema": STATUS_SCHEMA,
        "updated_at_unix_ms": int(time.time() * 1000),
    }
    value.update({key: field for key, field in fields.items() if key in allowed})
    _atomic_json(path, value)
