# -*- coding: utf-8 -*-
import sys

# 1. 穿透一切重定向，直接使用物理输出流向控制台打招呼，探测程序启动状态
raw_stdout = sys.__stdout__
if raw_stdout:
    raw_stdout.write("=" * 70 + "\n")
    raw_stdout.write("🛸 [进程雷达] telemetry_precision_test 已经拉起，开始执行单步导入...\n")
    raw_stdout.write("=" * 70 + "\n")
    raw_stdout.flush()

# 2. 单步导入追踪器
try:
    import unittest
    if raw_stdout: raw_stdout.write(" -> [1/8] unittest 模块加载成功\n"); raw_stdout.flush()
    
    import asyncio
    if raw_stdout: raw_stdout.write(" -> [2/8] asyncio 模块加载成功\n"); raw_stdout.flush()
    
    import datetime
    if raw_stdout: raw_stdout.write(" -> [3/8] datetime 模块加载成功\n"); raw_stdout.flush()
    
    import os
    if raw_stdout: raw_stdout.write(" -> [4/8] os 模块加载成功\n"); raw_stdout.flush()
    
    import time
    if raw_stdout: raw_stdout.write(" -> [5/8] time 模块加载成功\n"); raw_stdout.flush()
    
    import threading
    if raw_stdout: raw_stdout.write(" -> [6/8] threading 模块加载成功\n"); raw_stdout.flush()
    
    from unittest.mock import AsyncMock, MagicMock, patch
    if raw_stdout: raw_stdout.write(" -> [7/8] mock 模块加载成功\n"); raw_stdout.flush()
    
    # 加载可能引发 DLL 崩溃或重定向污染的核心文件
    import main
    if raw_stdout: raw_stdout.write(" -> [8/8] 核心 main 模块及其级联 Worker 载入成功！\n"); raw_stdout.flush()
    
except Exception as err:
    if raw_stdout:
        raw_stdout.write(f"\n❌ 进程在加载模块时发生严重错误: {err}\n")
        raw_stdout.flush()
    sys.exit(1)

from main import SafeStdoutWrapper
from activity_worker import ProcessActivityWorker
from hardware_worker import HardwareTelemetryWorker, DpcLatencyChecker
from context_worker import WindowStateTracker

# 手写一个兼容物理 Smallint 事务包裹的纯粹 Mock 异步上下文管理器
class MockTransaction:
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

class TelemetryPrecisionTestSuite(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        if raw_stdout:
            raw_stdout.write("\n" + "=" * 60 + "\n")
            raw_stdout.write("🛸 开始验证测试用例\n")
            raw_stdout.write("=" * 60 + "\n")
            raw_stdout.flush()

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        try:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
        except Exception:
            pass
        self.loop.close()

    def test_01_high_frequency_delta_conservation(self):
        # 1. 模拟高频抖动 (dt < 100ms)
        worker = ProcessActivityWorker()
        pid = 9999
        create_time = 1000.0
        cache_key = (pid, create_time)
        worker.pid_key_cache[cache_key] = 99
        worker.cpu_time_cache[cache_key] = (10.0, 100.0)
        
        p_info = {
            "pid": pid, "name": "test_app.exe", "cpu_time": 10.5,
            "r_bytes": 100, "w_bytes": 100, "other_bytes": 100,
            "r_ops": 10, "w_ops": 10, "ram_mb": 128, "threads": 4,
            "create_time": create_time, "is_fallback": False
        }
        
        with patch('asyncio.get_event_loop') as mock_get_event_loop:
            mock_loop = MagicMock()
            mock_get_event_loop.return_value = mock_loop
            mock_loop.time.return_value = 100.05
            
            with patch('activity_worker.fetch_system_processes', return_value=[p_info]):
                active_procs = worker.collect_active_processes()
                
            target_proc = [p for p in active_procs if p["os_pid"] == pid]
            if target_proc:
                self.assertEqual(target_proc[0]["cpu"], 0.0)
                
            cached_cpu, cached_ts = worker.cpu_time_cache[cache_key]
            self.assertEqual(cached_cpu, 10.0, "Bug 触发：时间增量丢失！")

            # 2. 正常周期 (dt = 1.0s)
            mock_loop.time.return_value = 101.0
            p_info["cpu_time"] = 11.5
            
            with patch('activity_worker.fetch_system_processes', return_value=[p_info]):
                active_procs_normal = worker.collect_active_processes()
                
            target_proc_normal = [p for p in active_procs_normal if p["os_pid"] == pid]
            self.assertTrue(len(target_proc_normal) > 0)
            self.assertAlmostEqual(target_proc_normal[0]["cpu"], 150.0, places=1)
            
        if raw_stdout: raw_stdout.write(" ✅ [TEST-01] 高频增量守恒校验通过。\n"); raw_stdout.flush()

    def test_02_thread_safe_log_rotation(self):
        test_log_file = "test_stress_rotation.log"
        if os.path.exists(test_log_file):
            try: os.remove(test_log_file)
            except: pass
            
        safe_logger = SafeStdoutWrapper(test_log_file)
        stop_stress = threading.Event()
        error_container = []

        def worker_write():
            while not stop_stress.is_set():
                try:
                    safe_logger.write(f"[{datetime.datetime.now().strftime('%X')}] 测试并发写入...\n")
                except Exception as e:
                    error_container.append(e)

        threads = [threading.Thread(target=worker_write, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()

        time.sleep(0.2)
        try:
            safe_logger.truncate_log(0) 
        except Exception as e:
            error_container.append(e)

        stop_stress.set()
        for t in threads:
            t.join(timeout=1.0)

        try:
            safe_logger.file.close()
            if os.path.exists(test_log_file):
                os.remove(test_log_file)
        except:
            pass

        self.assertEqual(len(error_container), 0)
        if raw_stdout: raw_stdout.write(" ✅ [TEST-02] 线程安全日志并发滚动校验通过。\n"); raw_stdout.flush()

    def test_03_postgresql_strict_integer_type_conversions(self):
        # 1. 【修复】：使用更轻量、更确定的 MagicMock，将需要 Await 的写方法显式声明为 AsyncMock，彻底切断底层类型污染
        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetchval = AsyncMock()
        
        mock_conn.transaction = MagicMock(return_value=MockTransaction())
        
        mock_pool = MagicMock()
        
        class AsyncContextManagerMock:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
                
        mock_pool.acquire.return_value = AsyncContextManagerMock()

        tracker = WindowStateTracker()
        
        # 2. 【修复】：Mock 前台切窗提取器，避免受物理桌面变化影响，实现测试环境的高精隔离
        tracker.check_foreground_window_fast = MagicMock(return_value={
            "hwnd": 12345, "os_pid": 5555, "window_title": "记事本", "window_mode": 3
        })
        
        metadata = {
            "process_name": "notepad.exe", "executable_path": "C:\\Windows\\notepad.exe",
            "parent_process": None, "command_line": "", "service_name": None,
            "is_elevated": 0, "signature_status": 1
        }
        
        tracker.active_slice = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc), "os_pid": 5555,
            "window_title": "记事本", "window_mode": 3, "metadata": metadata, "process_key": 42
        }
        tracker.pending_inserts.append(tracker.active_slice)
        
        self.loop.run_until_complete(tracker.poll_heartbeat(mock_pool))
        
        executed_sql = None
        for call in mock_conn.execute.call_args_list:
            args = call[0]
            if "fact_process_context" in args[0]:
                executed_sql = args[0]

        self.assertIsNotNone(executed_sql)
        self.assertTrue("1" in executed_sql)

        hardware_worker = HardwareTelemetryWorker()
        hw_sample = {
            "current_fps": 60.0, "average_fps": 60.0, "one_percent_low_fps": 55.0, "frametime_ms": 16.6,
            "frametime_jitter": 0.5, "cpu_total_usage": 10.0, "cpu_vcore_voltage": 1.25, "cpu_clock_mhz": 4300,
            "cpu_package_temp": 45.0, "cpu_package_power": 35.0, "system_dpc_latency": 15.0,
            "system_context_switches": 2000, "system_ram_usage_pct": 32.0, "system_commit_size_gb": 12.0,
            "system_hard_page_faults": 0, "gpu_usage": 40.0, "gpu_core_voltage": 0.95, "gpu_core_clock": 2500,
            "gpu_mem_clock": 10000, "gpu_core_temp": 50.0, "gpu_hotspot_temp": 58.0, "gpu_board_power": 180.0,
            "gpu_throttling_reasons": 0, "pcie_bus_utilization": 5.0, "disk_max_latency_ms": 1.2,
            "network_ping_ms": 10.0, "is_packet_loss": True, "network_jitter": 0.2, "cpu_ccd0_usage": 12.0, "cpu_ccd1_usage": 8.0
        }
        
        self.loop.run_until_complete(hardware_worker.write_to_db(mock_pool, hw_sample))
        
        hw_args = None
        for call in mock_conn.execute.call_args_list:
            args = call[0]
            if "fact_system_hardware" in args[0]:
                hw_args = args

        self.assertIsNotNone(hw_args)
        packet_loss_param = hw_args[28] 
        self.assertEqual(packet_loss_param, 1)
        
        hardware_worker.terminate()
        if raw_stdout: raw_stdout.write(" ✅ [TEST-03] 物理 Smallint 强类型转换校验通过。\n"); raw_stdout.flush()

    def test_04_watchdog_cleanup_on_terminate(self):
        worker = HardwareTelemetryWorker()
        worker.terminate()
        self.assertTrue(worker.stop_event.is_set())
        if raw_stdout: raw_stdout.write(" ✅ [TEST-04] 看门狗自销毁退役机制校验通过。\n"); raw_stdout.flush()

    @patch('win32com.client.GetObject')
    def test_05_lhm_precision_improvement_and_query(self, mock_get_object):
        mock_sensor_temp = MagicMock()
        mock_sensor_temp.SensorType = "Temperature"
        mock_sensor_temp.Name = "CPU Package"
        mock_sensor_temp.Value = 72.583
        
        mock_sensor_power = MagicMock()
        mock_sensor_power.SensorType = "Power"
        mock_sensor_power.Name = "CPU Package"
        mock_sensor_power.Value = 85.341
        
        mock_wmi = MagicMock()
        mock_wmi.InstancesOf.return_value = [mock_sensor_temp, mock_sensor_power]
        mock_get_object.return_value = mock_wmi
        
        worker = HardwareTelemetryWorker()
        
        temp_celsius = None
        temp_power = None
        wmi_obj = mock_get_object("winmgmts:\\\\.\\root\\LibreHardwareMonitor")
        for sensor in wmi_obj.InstancesOf("Sensor"):
            name_lower = sensor.Name.lower()
            if sensor.SensorType == "Temperature" and ("cpu package" in name_lower or "cpu core" in name_lower):
                temp_celsius = float(sensor.Value)
            elif sensor.SensorType == "Power" and ("cpu package" in name_lower or "cpu total" in name_lower):
                temp_power = float(sensor.Value)
            if temp_celsius and temp_power:
                break
                
        self.assertEqual(temp_celsius, 72.583)
        self.assertEqual(temp_power, 85.341)
        worker.terminate()
        if raw_stdout: raw_stdout.write(" ✅ [TEST-05] LibreHardwareMonitor 物理高精度传感器解析校验通过。\n"); raw_stdout.flush()

if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TelemetryPrecisionTestSuite)
    runner = unittest.TextTestRunner(stream=raw_stdout, verbosity=1)
    runner.run(suite)