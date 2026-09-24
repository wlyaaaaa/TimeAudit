"""Bounded, read-only health shared by the UI, diagnostic skill and watchdog."""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import json
import math
import re
import math
import re
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SCHEMA = "timeaudit.runtime-health.v1"
PRIVACY = dict.fromkeys(("raw_samples_included", "process_names_included", "process_paths_included", "window_titles_included", "command_lines_included", "credentials_included", "machine_identifiers_included"), False)


def heartbeat(path: Path, max_age: float, schema: str | None = None) -> dict:
    try:
        metadata = path.stat()
        age = time.time() - metadata.st_mtime
        if metadata.st_size > 65536 or age < -5:
            return {"status": "degraded", "reason": "invalid_heartbeat"}
        result = {"status": "healthy" if age <= max_age else "stale", "age_seconds": round(max(0, age), 2)}
        if schema:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if value.get("schema") != schema or value.get("state") not in {"healthy", "degraded"}:
                return {"status": "degraded", "reason": "invalid_heartbeat"}
            result["reported_state"] = value["state"]
            if value["state"] != "healthy":
                result["status"] = "degraded"
            for key in ("pending_files", "pending_bytes", "last_batch_rows", "pending_events", "pending_chars", "write_failures", "overflow_events", "capture_errors", "clock_adjustments", "published_segments"):
                if key in value:
                    if type(value[key]) is not int or value[key] < 0:
                        return {"status": "degraded", "reason": "invalid_heartbeat"}
                    result[key] = value[key]
            if value.get("pending_events", 0) or value.get("pending_chars", 0):
                result["status"] = "degraded"
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable", "reason": "heartbeat_unreadable"}


def _run(command: list[str], *, sql: str | None = None, timeout: int = 5) -> dict:
    try:
        result = subprocess.run(command, input=sql, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError()
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "reason": "probe_failed_or_timed_out"}


def database_health() -> dict:
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if not docker:
        return {"status": "unavailable", "reason": "docker_unavailable"}
    sql = """SELECT json_build_object('age_seconds', EXTRACT(EPOCH FROM (clock_timestamp()-timestamp)),
      'fps_state', CASE WHEN fps_capture_status IN ('active','gated_idle','starting','waiting_frames','error','source_unavailable') THEN fps_capture_status ELSE 'unknown' END,
      'quality_contract', measurement_quality->>'contract',
      'cpu_temperature_available', cpu_package_temp IS NOT NULL,
      'cpu_power_available', cpu_package_power IS NOT NULL,
      'gpu_hotspot_available', gpu_hotspot_temp IS NOT NULL,
      'disk_latency_available', disk_max_latency_ms IS NOT NULL)
      FROM public.fact_system_hardware ORDER BY timestamp DESC LIMIT 1;"""
    value = _run([docker, "exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=2500 -c lock_timeout=500", "audit-postgres", "psql", "-X", "-U", "leyang", "-d", "time_audit", "-At", "-v", "ON_ERROR_STOP=1"], sql=sql)
    fields = {"age_seconds", "fps_state", "quality_contract", "cpu_temperature_available", "cpu_power_available", "gpu_hotspot_available", "disk_latency_available"}
    if not isinstance(value, dict) or set(value) != fields or type(value["age_seconds"]) not in (int, float) or not math.isfinite(value["age_seconds"]):
        return {"status": "unavailable", "reason": "database_evidence_unavailable"}
    if not math.isfinite(value["age_seconds"]):
        return {"status": "degraded", "reason": "invalid_database_evidence"}
    if value["fps_state"] not in {"active", "gated_idle", "starting", "waiting_frames", "error", "source_unavailable", "unknown"} or value["quality_contract"] not in {None, "2"}:
        return {"status": "degraded", "reason": "invalid_database_evidence"}
    if any(type(value[key]) is not bool for key in fields if key.endswith("available")):
        return {"status": "degraded", "reason": "invalid_database_evidence"}
    value["status"] = "healthy" if 0 <= value["age_seconds"] <= 15 else "stale"
    value["age_seconds"] = round(value["age_seconds"], 2)
    value["fps_capture_degraded"] = value["fps_state"] in {"error", "source_unavailable", "unknown"}
    if value["fps_capture_degraded"] and value["status"] == "healthy":
        value["status"] = "degraded"
    return value


def lhm_health() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18085/data.json", timeout=2) as response:
            data = response.read(1048577)
        value = json.loads(data)
        if len(data) > 1048576 or not isinstance(value, dict) or not isinstance(value.get("Children"), list):
            raise ValueError()
        return {"status": "healthy"}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "sensor_endpoint_unavailable"}


def grafana_health() -> dict:
    """Read only Grafana's unauthenticated health metadata, never dashboards."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:43000/api/health", timeout=3) as response:
            data = response.read(4097)
        value = json.loads(data)
        if len(data) > 4096 or not isinstance(value, dict) or value.get("database") != "ok":
            raise ValueError()
        return {"status": "healthy"}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "grafana_endpoint_unavailable"}


def backup_health(directory: Path) -> dict:
    try:
        files = list(directory.glob("time_audit_*.dump"))
        if not files:
            return {"status": "unavailable", "reason": "no_completed_archive"}
        latest = max(files, key=lambda p: p.stat().st_mtime)
        stat = latest.stat()
        age = max(0, time.time()-stat.st_mtime)
        manifest = latest.with_suffix(latest.suffix + ".json")
        verified = False
        full_restore = False
        if manifest.exists() and manifest.stat().st_size <= 65536:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            verified = (isinstance(metadata, dict) and metadata.get("schema") == "timeaudit.backup-manifest.v1" and metadata.get("bytes") == stat.st_size and metadata.get("archive_list_verified") is True and isinstance(metadata.get("sha256"), str) and re.fullmatch(r"[0-9a-fA-F]{64}", metadata["sha256"]) is not None)
            full_restore = verified and metadata.get("restore_check_verified") is True
        return {"full_restore_verified": full_restore, "status": "healthy" if age <= 48*3600 and verified else "degraded", "age_hours": round(age/3600, 2), "size_mib": round(stat.st_size/1048576, 2), "completed_archives": len(files), "manifest_present_and_size_matches": verified, "hash_recomputed_by_health_probe": False, "partial_archives": len(list(directory.glob("*.partial")))}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "backup_metadata_unavailable"}


def blackbox_health() -> dict:
    """Ask the owning reader for closed-log bounds, without sampling payloads."""
    executable = shutil.which("pwsh.exe") or r"C:\Program Files\PowerShell\7\pwsh.exe"
    value = _run([executable, "-NoProfile", "-NonInteractive", "-File", r"E:\PCConfig\tools\Get-MemoryFreezeEvidence.ps1", "-CoverageOnly", "-Json"], timeout=6)
    try:
        if not isinstance(value, dict) or value.get("schema") != "pcconfig.memory-freeze-evidence.v1" or value.get("owner_ref") != "pcconfig:memory-freeze-diagnostics":
            raise ValueError()
        privacy = value.get("privacy")
        if not isinstance(privacy, dict) or set(privacy) != {"raw_samples_included", "process_names_included", "machine_identifiers_included"} or any(item is not False for item in privacy.values()):
            raise ValueError()
        rolling = value["rolling_coverage"]
        newest = dt.datetime.fromisoformat(rolling["newest_closed_record_utc"].replace("Z", "+00:00"))
        if newest.tzinfo is None:
            raise ValueError()
        age = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds()
        span = rolling["span_hours"]
        if type(span) not in (int, float) or not math.isfinite(span) or span < 0:
            raise ValueError()
        return {"status": "healthy" if value["status"] == "ok" and value["coverage"]["collector_state"] == "running" and 0 <= age <= 180 else "degraded",
                "age_seconds": round(age, 2), "rolling_span_hours": span,
                "continuity_verified_for_entire_span": False,
                "previous_session_tails_are_discontinuous": True}
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"status": "unavailable", "reason": "blackbox_coverage_unavailable"}


def watchdog_health(path: Path) -> dict:
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or value.get("schema") != "timeaudit.watchdog-outcome.v1" or value.get("status") not in {"healthy", "degraded", "unavailable"}:
            raise ValueError()
        checked = dt.datetime.fromisoformat(value["checked_at_utc"].replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise ValueError()
        age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
        return {"status": "healthy" if value["status"] == "healthy" and 0 <= age <= 300 else "degraded",
                "last_status": value["status"], "age_seconds": round(age, 2)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable", "reason": "watchdog_outcome_unavailable"}

def telemetry_overhead(seconds: float = 2.0) -> dict:
    """On-demand cost of the exact collector process tree, not all PC workloads."""
    try:
        import psutil
        from runtime_health import command_line_targets_script
        pid_text = (ROOT / "time_audit.pid").read_text(encoding="ascii").strip()
        if not pid_text.isdigit() or not 0.1 <= seconds <= 5:
            raise ValueError()
        parent = psutil.Process(int(pid_text))
        if not command_line_targets_script(parent.cmdline(), ROOT / "main.py"):
            raise ValueError()
        parent_identity = parent.create_time()
        def read_tree():
            observed = {}
            for process in [parent] + parent.children(recursive=True):
                try:
                    identity = (process.pid, process.create_time())
                    cpu = process.cpu_times()
                    observed[identity] = (cpu.user + cpu.system, process.memory_info().rss)
                except (psutil.Error, OSError):
                    continue
            return observed
        before = read_tree()
        started = time.monotonic()
        time.sleep(seconds)
        after = read_tree()
        elapsed = time.monotonic() - started
        if psutil.Process(parent.pid).create_time() != parent_identity or (parent.pid, parent_identity) not in before or (parent.pid, parent_identity) not in after or elapsed <= 0:
            raise ValueError()
        stable = before.keys() & after.keys()
        cpu_seconds = sum(max(0, after[key][0] - before[key][0]) for key in stable)
        core_percent = 100 * cpu_seconds / elapsed
        return {"status": "ok" if before.keys() == after.keys() else "partial",
                "elapsed_seconds": round(elapsed, 3), "compared_process_count": len(stable),
                "cpu_core_percent": round(core_percent, 3),
                "cpu_machine_percent": round(core_percent / (psutil.cpu_count() or 1), 3),
                "working_set_sum_mib": round(sum(item[1] for item in after.values()) / 1048576, 3),
                "scope": "exact_main_collector_and_children_only",
                "excludes": ["standalone_sensors", "docker_ingester", "native_memory_collector"],
                "memory_semantics": "RSS sum includes shared pages; not unique physical allocation."}
    except Exception:
        return {"status": "unavailable", "reason": "collector_cost_not_observable"}

def build_health(*, core_only: bool = False) -> dict:
    started = time.monotonic()
    jobs = {
        "telemetry": lambda: heartbeat(ROOT/"log"/"telemetry_heartbeat", 90),
        "activity_heartbeat": lambda: heartbeat(ROOT/"log"/"ahk_heartbeat", 20),
        "activity_persistence": lambda: heartbeat(ROOT/"log"/"ahk_health.json", 20, "timeaudit.ahk-health.v1"),
        "ingester": lambda: heartbeat(ROOT/"log"/"ingester_heartbeat.json", 45, "timeaudit.ingester-heartbeat.v1"),
        "database": database_health,
        "sensors": lhm_health,
        "grafana": grafana_health,
    }
    if not core_only:
        jobs["backup"] = lambda: backup_health(Path(r"G:\80_Backup\TimeAudit\postgresql"))
        jobs["memory_blackbox"] = blackbox_health
        jobs["watchdog_last_outcome"] = lambda: watchdog_health(ROOT/"log"/"watchdog_outcome.json")
    def safe_probe(job):
        try:
            return job()
        except Exception:
            return {"status": "unavailable", "reason": "probe_failed"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {key: executor.submit(safe_probe, job) for key, job in jobs.items()}
        components = {}
        for key, future in futures.items():
            try:
                value = future.result()
                if not isinstance(value, dict) or value.get("status") not in {"healthy", "degraded", "unavailable", "stale"}:
                    raise ValueError("invalid_component_result")
                components[key] = value
            except Exception:
                # One broken sensor must not discard the other components.
                components[key] = {"status": "unavailable", "reason": "component_probe_failed"}
    failed = [key for key, value in components.items() if value.get("status") != "healthy"]
    return {"schema": SCHEMA, "owner_ref": "timeaudit:runtime-health", "status": "healthy" if not failed else "degraded", "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "components": components, "degraded_components": failed, "elapsed_seconds": round(time.monotonic()-started, 3), "scope": "watchdog_core" if core_only else "full", "privacy": PRIVACY.copy()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--include-overhead", action="store_true", help="Sample the exact collector tree for two seconds; no persistent recording.")
    args = parser.parse_args(argv)
    value = build_health(core_only=args.core_only)
    if args.include_overhead:
        value["collector_overhead"] = telemetry_overhead()
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0 if value["status"] == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(main())
