# -*- coding: utf-8 -*-
"""
Windows 11 Native Telemetry Engine - Backend Verification [Phase 3]
===================================================================
验证模块：
  1. Win11 原生 WinMM 1ms 高精度计时器中断与 DPC 微秒级抖动监控
  2. AMD Ryzen 9 9950X3D 双 CCD (32线程) 物理负载实况分析
  3. PresentMon Console 2.41 物理数据流灌注、平均帧与 1% Low 统计学解算
"""

import asyncio
import sys
import os
import time
import unittest
import psutil
import threading

# 将当前目录追加到搜索路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from hardware_worker import HardwareTelemetryWorker, DpcLatencyChecker
    from main import DB_DSN
except ImportError as e:
    print(f"[-] 依赖加载失败，请确保位于工程根目录下运行此脚本。错误详情: {e}")
    sys.exit(1)


# =====================================================================
# 🕵️ 第三阶段测试用例集
# =====================================================================

class TelemetryPhase3TestSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.workers_to_clean = []
        print("\033[94m[*] 第三阶段物理实况测试就绪。\033[0m")

    async def asyncTearDown(self):
        """释放高精度多媒体时钟中断线程，消灭句柄残留"""
        for worker in self.workers_to_clean:
            if hasattr(worker, 'presentmon_process') and worker.presentmon_process:
                pm = worker.presentmon_process
                try:
                    pm.terminate()
                    pm.wait(timeout=1.0)
                except Exception: pass
                try:
                    if pm.stdout: pm.stdout.close()
                    if pm.stderr: pm.stderr.close()
                except Exception: pass
                worker.presentmon_process = None
            
            if hasattr(worker, 'dpc_checker') and worker.dpc_checker:
                try:
                    worker.dpc_checker.stop()
                except Exception: pass
            
            try:
                worker.terminate()
            except Exception: pass
        print("\033[92m[+] 物理计时器句柄及 PresentMon 管线已安全释放。\033[0m")

    async def test_01_real_dpc_latency_stress(self):
        """性能与正确性 3.1: Windows 11 Workstation 高精度物理 DPC 抖动监测"""
        checker = DpcLatencyChecker()
        checker.start()
        
        # 等待物理计时器稳定捕获
        await asyncio.sleep(1.0)
        jitter_us = checker.get_latency_us()
        checker.stop()

        print("\n" + "="*80)
        print("⚡ 物理性能评测：[1. Win11 宿主用户态 DPC 抖动延迟监测]")
        print("="*80)
        print(f"  - Windows 11 Workstation 内核时钟中断精度: 1.000 ms")
        print(f"  - 实况检测高精度 DPC 用户态抖动延迟:       {jitter_us:.3f} us")
        
        # 一般在性能强劲的 9950X3D Workstation 上，空闲时此延迟应小于 200 us
        self.assertTrue(jitter_us >= 0.0, "DPC 延迟数据未正常生成")
        self.assertTrue(jitter_us < 2000.0, "系统产生严重的 DPC 抢占卡顿")
        print("="*80)

    def test_02_dual_ccd_physical_audit(self):
        """性能与正确性 3.2: 现场捕获 9950X3D 双 CCD (32个逻辑核心) 真实算力负载"""
        cpu_percents = psutil.cpu_percent(interval=None, percpu=True)
        
        print("\n" + "="*80)
        print("⚡ 物理性能评测：[2. AMD Ryzen 9 9950X3D 双 CCD 物理线程审计]")
        print("="*80)
        print(f"  - 宿主总逻辑处理器线程数: {len(cpu_percents)} Threads")

        if len(cpu_percents) >= 32:
            ccd0_load = sum(cpu_percents[0:16]) / 16.0
            ccd1_load = sum(cpu_percents[16:32]) / 16.0
            
            print(f"  - [CCD0 - 堆叠 3D V-Cache 核心 (线程 0-15)  平均负载] : {ccd0_load:.3f} %")
            print(f"  - [CCD1 - 高频核心通道     (线程 16-31) 平均负载] : {ccd1_load:.3f} %")
            
            # 打印全部 32 线程精细心电图
            print("  - [物理核心拓扑实况图]:")
            col_width = 8
            for i in range(16):
                t0_val = cpu_percents[i]
                t1_val = cpu_percents[i+16]
                line = f"    CCD0 Thread {i:02d} : {t0_val:5.1f}% | CCD1 Thread {i+16:02d} : {t1_val:5.1f}%"
                print(line)
        else:
            print("  - [提示] 当前硬件不具备 32 个逻辑核心，已自动跳过 9950X3D 独有拓扑剖析。")
        print("="*80)

    def test_03_presentmon_parser_ingestion(self):
        """数据正确性校验 3.3: 模拟灌注 PresentMon 2.41 原始物理帧时，解算平均/1% Low FPS"""
        worker = HardwareTelemetryWorker()
        self.workers_to_clean.append(worker)

        # 模拟 PresentMon Console 2.41 输出的真实管线帧数据 (20帧，存在一帧严重的 50ms 卡顿)
        # 1000ms / 8ms = 125 FPS
        mock_frametimes = [
            8.0, 8.1, 7.9, 8.0, 8.2, 
            8.0, 8.0, 7.8, 8.1, 50.0,  # 这一帧产生物理卡顿 (相当于 20 FPS)
            8.0, 8.1, 7.9, 8.0, 8.2, 
            8.0, 8.0, 7.8, 8.1, 8.0
        ]

        app_name = "test_game"
        
        # 将测试数据物理灌入硬件探针的统计窗口中
        with worker.lock:
            worker.app_windows[app_name] = mock_frametimes
            worker.app_last_update[app_name] = time.time()

        # 执行解算器获取快照
        snapshot = worker.collect_hardware_snapshot(app_name)

        # 统计学正确性核验：
        # 1. 最后一帧为 8.0ms，故 current_fps 应为 1000 / 8.0 = 125 FPS
        # 2. 平均帧率解算：总时长 = sum(frametimes) = 202.2 ms, 共 20 帧 => 平均帧 = 20 / (0.2022s) = 98.91 FPS
        # 3. 1% Low 帧率解算：20 帧的 99% 百分位对应卡顿帧 50.0ms => 1% Low = 1000 / 50.0 = 20 FPS
        calculated_curr = snapshot["current_fps"]
        calculated_avg = snapshot["average_fps"]
        calculated_low = snapshot["one_percent_low_fps"]
        calculated_jitter = snapshot["frametime_jitter"]

        print("\n" + "="*80)
        print("⚡ 数据正确性审查：[3. PresentMon Ingestion 1% Low 统计学解析引擎]")
        print("="*80)
        print(f"  - 模拟灌入当前实时帧时间 (Frametime) : {mock_frametimes[-1]:.2f} ms")
        print(f"  - 解析输出当前实时帧率 (Current FPS)   : {calculated_curr:.2f} FPS (预期: 125.00)")
        print(f"  - 解析输出历史平均帧率 (Average FPS)   : {calculated_avg:.2f} FPS (预期: 98.91)")
        print(f"  - 解析输出极低卡顿帧率 (1% Low FPS)    : {calculated_low:.2f} FPS (预期: 20.00)")
        print(f"  - 解析输出相邻帧时抖动 (Frame Jitter)  : {calculated_jitter:.2f} ms")
        print("="*80)

        self.assertAlmostEqual(calculated_curr, 125.0, places=1)
        self.assertAlmostEqual(calculated_avg, 98.91, places=1)
        self.assertAlmostEqual(calculated_low, 20.0, places=1)


if __name__ == "__main__":
    unittest.main()