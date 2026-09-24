"""Health remains truthful on unavailable sensors, corrupt metadata and partial failure."""
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import timeaudit_health as health


def database_row(**changes):
    value = dict(age_seconds=1, fps_state="active", quality_contract="2", cpu_temperature_available=True,
                 cpu_power_available=True, gpu_hotspot_available=False, disk_latency_available=True)
    value.update(changes)
    return value


def test_database_requires_finite_age_and_strict_booleans():
    for changes in ({"age_seconds": float("nan")}, {"age_seconds": float("inf")}, {"age_seconds": True}, {"cpu_power_available": 1}, {"fps_state": "made_up"}):
        with patch.object(health, "_run", return_value=database_row(**changes)), patch.object(health.shutil, "which", return_value="docker"):
            assert health.database_health()["status"] != "healthy"
    with patch.object(health, "_run", return_value=database_row()), patch.object(health.shutil, "which", return_value="docker"):
        assert health.database_health()["status"] == "healthy"


def test_lhm_requires_sensor_payload_not_arbitrary_http_200():
    for payload in ({}, {"error": "unavailable"}, {"Children": None}, []):
        with patch.object(health.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            assert health.lhm_health()["status"] == "unavailable"
    with patch.object(health.urllib.request, "urlopen", return_value=io.BytesIO(b'{"Children":[]}')):
        assert health.lhm_health()["status"] == "healthy"


def test_heartbeat_requires_recent_valid_state_and_bounded_counters(tmp_path):
    target = tmp_path / "health.json"
    valid = dict(schema="timeaudit.ahk-health.v1", state="healthy", pending_events=0, pending_chars=0)
    target.write_text(json.dumps(valid))
    assert health.heartbeat(target, 20, valid["schema"])["status"] == "healthy"
    for changes in ({"pending_events": True}, {"pending_chars": -1}, {"state": "fiction"}):
        target.write_text(json.dumps({**valid, **changes}))
        assert health.heartbeat(target, 20, valid["schema"])["status"] != "healthy"
    target.write_text(json.dumps({**valid, "pending_events": 2}))
    assert health.heartbeat(target, 20, valid["schema"])["status"] == "degraded"
    target.write_text(json.dumps(valid)); os.utime(target, (1, 1))
    assert health.heartbeat(target, 20, valid["schema"])["status"] == "stale"
    target.write_text("private malformed payload")
    value = health.heartbeat(target, 20, valid["schema"])
    assert "private" not in json.dumps(value)


def test_grafana_requires_health_database_ok_and_does_not_expose_response():
    for payload in ({}, {"database": "failing"}, [], {"database": "ok", "padding": "x"*4096}):
        with patch.object(health.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            assert health.grafana_health()["status"] == "unavailable"
    with patch.object(health.urllib.request, "urlopen", return_value=io.BytesIO(b'{"database":"ok","version":"private"}')):
        assert health.grafana_health() == {"status": "healthy"}
    with patch.object(health.urllib.request, "urlopen", side_effect=OSError("private")):
        assert health.grafana_health() == {"status": "unavailable", "reason": "grafana_endpoint_unavailable"}


def test_grafana_failure_is_in_watchdog_core_health():
    with patch.object(health, "database_health", return_value={"status": "healthy"}), \
         patch.object(health, "heartbeat", return_value={"status": "healthy"}), \
         patch.object(health, "lhm_health", return_value={"status": "healthy"}), \
         patch.object(health, "grafana_health", return_value={"status": "unavailable"}):
        report = health.build_health(core_only=True)
    assert report["status"] == "degraded"
    assert report["degraded_components"] == ["grafana"]


def test_bad_backup_manifest_cannot_crash_other_health_probes(tmp_path):
    archive = tmp_path / "time_audit_20260917_120000.dump"
    archive.write_bytes(b"PGDMP" + bytes(1024))
    manifest = archive.with_suffix(".dump.json")
    for payload in ([], None, {"sha256": "not a hash"}):
        manifest.write_text(json.dumps(payload))
        assert health.backup_health(tmp_path)["status"] != "healthy"
    manifest.write_text(json.dumps(dict(schema="timeaudit.backup-manifest.v1", bytes=archive.stat().st_size,
        archive_list_verified=True, sha256="a"*64)))
    value=health.backup_health(tmp_path)
    assert value["status"] == "healthy"
    assert value["hash_recomputed_by_health_probe"] is False


def test_one_failed_probe_does_not_erase_available_evidence():
    with patch.object(health, "database_health", side_effect=RuntimeError("private failure")), \
         patch.object(health, "heartbeat", return_value={"status": "healthy"}), \
         patch.object(health, "lhm_health", return_value={"status": "healthy"}):
        value=health.build_health(core_only=True)
    assert value["status"] == "degraded"
    assert value["components"]["sensors"]["status"] == "healthy"
    assert value["components"]["database"]["status"] == "unavailable"
    assert "private failure" not in json.dumps(value)
    assert not any(value["privacy"].values())

def test_exact_collector_overhead_is_bounded_and_uses_cpu_deltas(tmp_path):
    import types
    from unittest.mock import Mock
    import psutil
    (tmp_path / "time_audit.pid").write_text("123")
    process = Mock(pid=123)
    process.cmdline.return_value=["python.exe", str(tmp_path / "main.py")]
    process.create_time.return_value=10.0
    process.children.return_value=[]
    process.cpu_times.side_effect=[types.SimpleNamespace(user=1.0, system=.1), types.SimpleNamespace(user=2.0, system=.1)]
    process.memory_info.return_value=types.SimpleNamespace(rss=1048576)
    with patch.object(health, "ROOT", tmp_path), patch.object(psutil, "Process", return_value=process), \
         patch.object(psutil, "cpu_count", return_value=8), patch.object(health.time, "sleep"), \
         patch.object(health.time, "monotonic", side_effect=[100.0, 102.0]):
        value=health.telemetry_overhead()
    assert value["status"] == "ok"
    assert value["cpu_core_percent"] == 50
    assert value["cpu_machine_percent"] == 6.25
    assert value["working_set_sum_mib"] == 1
    assert value["scope"] == "exact_main_collector_and_children_only"
    assert "pid" not in value


def test_foreign_or_unreadable_collector_is_not_zero_cost(tmp_path):
    from unittest.mock import Mock
    import psutil
    (tmp_path / "time_audit.pid").write_text("123")
    process = Mock(pid=123)
    process.cmdline.return_value=["python.exe", "not-the-collector.py"]
    with patch.object(health, "ROOT", tmp_path), patch.object(psutil, "Process", return_value=process):
        assert health.telemetry_overhead()["status"] == "unavailable"
    process.cmdline.return_value=["python.exe", str(tmp_path / "main.py")]
    process.create_time.return_value=10.0; process.children.return_value=[]
    process.cpu_times.side_effect=psutil.AccessDenied(123)
    with patch.object(health, "ROOT", tmp_path), patch.object(psutil, "Process", return_value=process), \
         patch.object(health.time, "sleep"), patch.object(health.time, "monotonic", side_effect=[100.,102.]):
        assert health.telemetry_overhead()["status"] == "unavailable"


def test_blackbox_health_uses_rolling_bounds_not_old_tail_span():
    import datetime as dt
    value=dict(schema="pcconfig.memory-freeze-evidence.v1",owner_ref="pcconfig:memory-freeze-diagnostics",status="ok",
               privacy=dict(raw_samples_included=False,process_names_included=False,machine_identifiers_included=False),
               rolling_coverage=dict(span_hours=14,newest_closed_record_utc=dt.datetime.now(dt.timezone.utc).isoformat()),
               coverage=dict(collector_state="running",retained_span_hours=82))
    with patch.object(health, "_run", return_value=value):
        report=health.blackbox_health()
    assert report["status"] == "healthy" and report["rolling_span_hours"] == 14
    assert report["continuity_verified_for_entire_span"] is False
    value["privacy"]["raw_samples_included"]=True
    with patch.object(health,"_run",return_value=value):
        assert health.blackbox_health()["status"] == "unavailable"


def test_stale_watchdog_receipt_cannot_claim_current_health(tmp_path):
    path=tmp_path / "watchdog.json"
    path.write_text(json.dumps(dict(schema="timeaudit.watchdog-outcome.v1",status="healthy",checked_at_utc="2000-01-01T00:00:00Z")))
    assert health.watchdog_health(path)["status"] == "degraded"
    path.write_text(json.dumps({"private":"unrelated payload"}))
    assert health.watchdog_health(path)["status"] == "unavailable"
