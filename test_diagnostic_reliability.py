"""Behavioral regressions extracted from production code without starting collectors."""
import ast
import asyncio
import ctypes
import datetime
import json
import struct
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
from diagnostic_validation import validate_aggregate
import timeaudit_diagnostic_summary as summary
from test_timeaudit_diagnostic_summary import aggregate_fixture, AFTER, UNTIL

ROOT = Path(__file__).resolve().parent

def function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), namespace)
    return namespace[name]

class ReliabilityTests(unittest.TestCase):
    def test_early_wakeup_cannot_enter_collector_slot(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        collector = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_collector")
        loop = next(n for n in ast.walk(collector) if isinstance(n, ast.While) and isinstance(n.test, ast.Constant) and n.test.value is True)
        body = loop.body[:3] + [ast.Return(value=ast.Name(id="slot_started", ctx=ast.Load()))]
        probe = ast.AsyncFunctionDef(name="probe", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=[ast.While(test=ast.Constant(True), body=body, orelse=[])], decorator_list=[])
        clock = types.SimpleNamespace(now=.99, calls=0)
        async def early_sleep(delay):
            clock.calls += 1
            self.assertGreaterEqual(delay, .015625)
            clock.now += max(0, delay-.005)
            self.assertLess(clock.calls, 10)
        ns = {"loop": types.SimpleNamespace(time=lambda: clock.now), "next_telemetry_deadline": 1., "asyncio": types.SimpleNamespace(sleep=early_sleep), "time": types.SimpleNamespace(get_clock_info=lambda _: types.SimpleNamespace(resolution=.015625))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), "guard", "exec"), ns)
        entered = asyncio.run(ns["probe"]())
        self.assertGreaterEqual(entered, 1.)
        self.assertGreater(clock.calls, 0)

    def test_mutex_failure_and_full_width_handle(self):
        kernel = types.SimpleNamespace(CreateMutexW=Mock(return_value=0), GetLastError=Mock(return_value=5), CloseHandle=Mock())
        proxy = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=kernel), c_void_p=ctypes.c_void_p, c_int=ctypes.c_int, c_wchar_p=ctypes.c_wchar_p)
        claim = function("main.py", "enforce_singleton", {"ctypes": proxy, "datetime": datetime})
        self.assertIsNone(claim())
        kernel.CloseHandle.assert_not_called()
        kernel.CreateMutexW.return_value = 0x100000001
        kernel.GetLastError.return_value = 0
        self.assertEqual(claim(), 0x100000001)
        kernel.GetLastError.return_value = 183
        self.assertIsNone(claim())
        kernel.CloseHandle.assert_called_with(0x100000001)
        self.assertIs(kernel.CreateMutexW.restype, ctypes.c_void_p)

    def test_rtss_last_frame_and_window_average_are_distinct(self):
        tree = ast.parse((ROOT / "hardware_worker.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HardwareTelemetryWorker")
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("_rtss_u32", "_parse_rtss_app_entry")]
        ns = {"struct": struct}
        copied = ast.ClassDef(name="Parser", bases=[], keywords=[], body=methods, decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copied], type_ignores=[])), "rtss", "exec"), ns)
        parser = ns["Parser"]
        parser.RTSS_FRAME_FRESH_MILLISECONDS = 2000
        entry = bytearray(9180)
        for offset, value in ((268, 9000), (272, 10000), (276, 100), (280, 20000), (5024, 1000)):
            struct.pack_into("<I", entry, offset, value)
        sample = parser._parse_rtss_app_entry(entry, 10001)
        self.assertEqual(sample["current_fps"], 50)
        self.assertEqual(sample["average_fps"], 100)
        self.assertEqual(sample["frametime_ms"], 20)

    def test_missing_and_stale_sensors_are_null(self):
        tree = ast.parse((ROOT / "hardware_worker.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HardwareTelemetryWorker")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "collect_hardware_snapshot")
        ns = {"time": types.SimpleNamespace(monotonic=lambda: 123.), "psutil": types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(percent=50)), "datetime": datetime}
        copied = ast.ClassDef(name="Probe", bases=[], keywords=[], body=[method], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copied], type_ignores=[])), "sensors", "exec"), ns)
        obj = ns["Probe"]()
        keys = {n.slice.value for n in ast.walk(method) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "cached_pdh_data" and isinstance(n.slice, ast.Constant)}
        obj.cached_pdh_data = dict.fromkeys(keys)
        obj.cached_pdh_data.update(cpu_percents=[0]*32, cpu_total_usage=10, system_context_switches_rate=0, cpu_package_temp=77, cpu_package_power=200, disk_max_latency_ms=0.)
        for name in ("pdh_lock", "wmi_lock", "_fps_state_lock"):
            setattr(obj, name, threading.Lock())
        for name in ("cached_wmi_temp", "cached_wmi_power", "cached_cpu_vcore", "cached_gpu_voltage", "cached_gpu_hotspot", "_render_gate_started_monotonic", "_presentmon_started_monotonic", "_presentmon_error"):
            setattr(obj, name, None)
        obj.last_ts = 122
        obj._rtss_mapping_available = False
        obj._read_foreground_pid = lambda: 0
        obj._get_commit_charge_gb = lambda: 12
        obj.dpc_checker = types.SimpleNamespace(get_latency_us=lambda: 0)
        obj._read_rtss_fps_snapshot = lambda pid: None
        obj._select_presentmon_window = lambda *args: []
        obj._render_active = lambda: False
        obj._resolve_fps_capture_state = lambda **kwargs: ("source_unavailable", "gpu_source_unavailable")
        obj.network_metrics = {"ping_ms": None, "packet_loss": False, "jitter": 0}
        data = obj.collect_hardware_snapshot("")
        self.assertIsNone(data["cpu_package_temp"])
        self.assertIsNone(data["cpu_package_power"])
        self.assertIsNone(data["disk_max_latency_ms"])
        obj._lhm_sample_monotonic = obj._pdh_sample_monotonic = 123
        obj.cached_wmi_temp = 55
        obj.cached_wmi_power = 75
        obj.cached_gpu_hotspot = 65
        data = obj.collect_hardware_snapshot("")
        self.assertEqual(data["cpu_package_temp"], 55)
        self.assertEqual(data["cpu_package_power"], 75)
        self.assertEqual(data["gpu_hotspot_temp"], 65)
        self.assertEqual(data["disk_max_latency_ms"], 0)
        obj._lhm_sample_monotonic = 110
        data = obj.collect_hardware_snapshot("")
        self.assertIsNone(data["cpu_package_temp"])
        self.assertIsNone(data["gpu_hotspot_temp"])

    def test_malformed_or_private_aggregate_fails_closed(self):
        variants = [{}, {**aggregate_fixture(), "window_title": "private"}, {**aggregate_fixture(), "hardware_sample_count": True}, {**aggregate_fixture(), "cpu_temp_max_c": float("nan")}, {**aggregate_fixture(), "fps_state_counts": {"active": 1}}]
        for value in variants:
            with self.subTest(keys=len(value)), self.assertRaisesRegex(RuntimeError, "query_output_invalid"):
                validate_aggregate(value, fields=summary.AGGREGATE_FIELDS, count_key="hardware_sample_count", after=AFTER, until=UNTIL)
        self.assertEqual(validate_aggregate(aggregate_fixture(), fields=summary.AGGREGATE_FIELDS, count_key="hardware_sample_count", after=AFTER, until=UNTIL)["hardware_sample_count"], 3600)

if __name__ == "__main__":
    unittest.main()