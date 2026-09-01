# -*- coding: utf-8 -*-
import ast
import asyncio
import datetime
import struct
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hardware_worker


ROOT = Path(__file__).resolve().parent


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, *args):
        self.calls.append(args)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class FpsCaptureContractTest(unittest.TestCase):
    def test_lhm_gpu_core_load_recovers_nvml_failure_without_zero(self):
        lhm = hardware_worker.HardwareTelemetryWorker._extract_lhm_gpu_metrics({
            "/NVIDIA/Load/GPU Core": "54.0 %",
            "/NVIDIA/Temperatures/GPU Core": "59.0 °C",
            "/NVIDIA/Powers/GPU Power": "188.0 W",
            "/NVIDIA/Load/GPU D3D Usage": "99.0 %",
        })
        source, metrics = hardware_worker.HardwareTelemetryWorker._merge_gpu_metrics(
            {"source_available": False}, lhm
        )

        self.assertEqual("lhm", source)
        self.assertEqual(54.0, metrics["gpu_usage"])
        self.assertEqual(59.0, metrics["gpu_core_temp"])
        self.assertEqual(188.0, metrics["gpu_board_power"])

        unavailable_source, unavailable = hardware_worker.HardwareTelemetryWorker._merge_gpu_metrics(
            {"source_available": False}, {}
        )
        self.assertIsNone(unavailable_source)
        self.assertIsNone(unavailable["gpu_usage"])
        self.assertIsNone(unavailable["gpu_core_temp"])
        self.assertIsNone(unavailable["gpu_board_power"])

    def test_status_mapping_has_six_safe_states_and_bounded_starting(self):
        map_state = hardware_worker.HardwareTelemetryWorker._map_fps_capture_state
        cases = [
            ((False, False, False, None, 0.0), "source_unavailable"),
            ((True, False, False, None, 0.0), "gated_idle"),
            ((True, True, False, 100.0, 109.9), "starting"),
            ((True, True, False, 100.0, 110.0), "waiting_frames"),
            ((True, True, True, 100.0, 110.0), "active"),
            ((True, True, False, 100.0, 100.0, "presentmon_start_failed"), "error"),
        ]

        observed = set()
        for args, expected in cases:
            observed.add(map_state(*args)[0])
            self.assertEqual(expected, map_state(*args)[0])
        self.assertEqual(
            {
                "source_unavailable", "gated_idle", "starting",
                "waiting_frames", "active", "error",
            },
            observed,
        )

    def test_named_etw_cleanup_runs_once_per_inactive_boundary(self):
        worker = hardware_worker.HardwareTelemetryWorker.__new__(
            hardware_worker.HardwareTelemetryWorker
        )
        worker._fps_state_lock = threading.Lock()
        worker._presentmon_path = "PresentMonConsole.exe"
        worker._presentmon_session_cleanup_done = False
        worker._presentmon_error = None

        with patch.object(
            hardware_worker.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            self.assertTrue(worker._cleanup_presentmon_session_once())
            self.assertTrue(worker._cleanup_presentmon_session_once())

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("TimeAuditPresentMon", command)
        self.assertIn("--terminate_existing_session", command)

    def test_rtss_entry_parser_returns_fps_and_rejects_stale_samples(self):
        entry = bytearray(12304)
        struct.pack_into("<I", entry, 0, 1234)
        struct.pack_into("<5I", entry, 268, 9000, 10000, 60, 16667, 0)
        struct.pack_into("<I", entry, 920, 4)
        struct.pack_into("<4I", entry, 924, 16000, 17000, 18000, 20000)
        struct.pack_into("<I", entry, 5020, 4)
        struct.pack_into("<I", entry, 5024, 600)
        struct.pack_into("<I", entry, 9176, 500)

        sample = hardware_worker.HardwareTelemetryWorker._parse_rtss_app_entry(
            bytes(entry),
            10500,
        )

        self.assertEqual(60.0, sample["current_fps"])
        self.assertEqual(60.0, sample["average_fps"])
        self.assertEqual(50.0, sample["one_percent_low_fps"])
        self.assertAlmostEqual(16.667, sample["frametime_ms"], places=3)
        self.assertEqual(2.0, sample["frametime_jitter"])
        self.assertIsNone(
            hardware_worker.HardwareTelemetryWorker._parse_rtss_app_entry(
                bytes(entry),
                13001,
            )
        )

    def test_recent_rtss_frame_suppresses_presentmon_only_temporarily(self):
        worker = hardware_worker.HardwareTelemetryWorker.__new__(
            hardware_worker.HardwareTelemetryWorker
        )
        worker._fps_state_lock = threading.Lock()
        worker._rtss_frame_seen_monotonic = 100.0
        with (
            patch.object(worker, "_render_active", return_value=True),
            patch.object(hardware_worker.time, "monotonic", return_value=101.0),
        ):
            self.assertFalse(worker._presentmon_needed())
        with (
            patch.object(worker, "_render_active", return_value=True),
            patch.object(hardware_worker.time, "monotonic", return_value=104.0),
        ):
            self.assertTrue(worker._presentmon_needed())

    def test_single_ddl_helper_is_called_for_initial_and_reconnect_pools(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
        schema = (ROOT / "schema.sql").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "ensure_fps_capture_schema"
        )
        collector = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_collector"
        )
        helper_source = ast.unparse(helper)
        self.assertIn("ADD COLUMN IF NOT EXISTS fps_capture_status text", helper_source)
        self.assertIn("ADD COLUMN IF NOT EXISTS fps_capture_detail text", helper_source)
        self.assertEqual(1, source.count("ADD COLUMN IF NOT EXISTS fps_capture_status text"))
        self.assertEqual(2, sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ensure_fps_capture_schema"
            for node in ast.walk(collector)
        ))
        self.assertIn("fps_capture_status", schema)
        self.assertIn("fps_capture_detail", schema)

    def test_insert_carries_status_and_detail_as_two_new_parameters(self):
        worker = hardware_worker.HardwareTelemetryWorker.__new__(
            hardware_worker.HardwareTelemetryWorker
        )
        conn = _Connection()
        data = {
            "current_fps": 0.0, "average_fps": 0.0,
            "one_percent_low_fps": 0.0, "frametime_ms": 0.0,
            "frametime_jitter": 0.0, "fps_capture_status": "waiting_frames",
            "fps_capture_detail": "no_fresh_foreground_frame",
            "cpu_total_usage": 1.0, "cpu_vcore_voltage": None,
            "cpu_clock_mhz": 1, "cpu_package_temp": 1.0,
            "cpu_package_power": 1.0, "system_dpc_latency": 1.0,
            "system_context_switches": 1, "gpu_usage": None,
            "gpu_core_voltage": None, "gpu_core_clock": None,
            "gpu_mem_clock": None, "gpu_core_temp": None,
            "gpu_hotspot_temp": None, "gpu_board_power": None,
            "gpu_throttling_reasons": None, "pcie_bus_utilization": None,
            "system_ram_usage_pct": 1.0, "system_commit_size_gb": 1.0,
            "system_hard_page_faults": 0, "disk_max_latency_ms": 0.0,
            "network_ping_ms": None, "is_packet_loss": False,
            "network_jitter": 0.0, "cpu_ccd0_usage": 0.0,
            "cpu_ccd1_usage": 0.0,
        }

        asyncio.run(worker.write_to_db(_Pool(conn), data, datetime.datetime.now(datetime.timezone.utc)))

        self.assertEqual(1, len(conn.calls))
        call = conn.calls[0]
        self.assertIn("fps_capture_status, fps_capture_detail", call[0])
        self.assertIn("$33", call[0])
        self.assertEqual(34, len(call))  # SQL plus timestamp and 32 fact values.
        self.assertEqual("waiting_frames", call[7])
        self.assertEqual("no_fresh_foreground_frame", call[8])


if __name__ == "__main__":
    unittest.main()
