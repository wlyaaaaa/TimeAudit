"""Validate typed, payload-free query results before interpreting them."""
from __future__ import annotations
import datetime as dt
import math

FPS_STATES = frozenset("active gated_idle starting waiting_frames error source_unavailable legacy_missing unknown".split())

def validate_aggregate(value, *, fields, count_key, after, until):
    def invalid():
        raise RuntimeError("query_output_invalid")
    if not isinstance(value, dict) or set(value) != set(fields):
        invalid()
    count = value[count_key]
    if type(count) is not int or count < 0:
        invalid()
    for key, item in value.items():
        if key == "fps_state_counts":
            if not isinstance(item, dict) or not set(item).issubset(FPS_STATES):
                invalid()
            if any(type(n) is not int or n < 0 for n in item.values()):
                invalid()
            if sum(item.values()) != count:
                invalid()
        elif key.endswith(("_utc", "_first", "_last")):
            if item is not None:
                try:
                    stamp = dt.datetime.fromisoformat(item.replace("Z", "+00:00"))
                    if stamp.tzinfo is None or stamp <= after or stamp > until:
                        invalid()
                except (ValueError, TypeError, AttributeError):
                    invalid()
        elif item is not None:
            if type(item) not in (int, float) or not math.isfinite(item):
                invalid()
            if key.endswith(("_count", "_samples", "_seconds")) and item < 0:
                invalid()
            if key.endswith(("_count", "_samples")) and type(item) is not int:
                invalid()
    first, last = value["first_sample_utc"], value["last_sample_utc"]
    if count == 0:
        if first is not None or last is not None:
            invalid()
    elif first is None or last is None:
        invalid()
    else:
        if dt.datetime.fromisoformat(first.replace("Z", "+00:00")) > dt.datetime.fromisoformat(last.replace("Z", "+00:00")):
            invalid()
    if "requested_window_seconds" in value:
        span = value["requested_window_seconds"]
        if span is None or abs(span - (until-after).total_seconds()) > 1:
            invalid()
    for key in fields:
        if key.endswith("_samples") or key in ("fps_sample_count", "fps_positive_sample_count", "rapid_sample_count", "collector_instance_count"):
            if value[key] is None or value[key] > count:
                invalid()
    return value