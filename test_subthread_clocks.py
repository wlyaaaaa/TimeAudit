# -*- coding: utf-8 -*-
"""
TimeAudit 子线程时钟安全测试 (防 asyncio event_loop RuntimeError 警告/崩溃回归)
==========================================================================
背景：当 collect_active_processes 或 collect_hardware_snapshot 被移至 asyncio.to_thread 
工作线程中运行时，由于子线程不具有 asyncio 的事件循环，如果内部调用了 get_event_loop().time() 
会触发 RuntimeError。
本测试通过在无 event_loop 的子线程/工作线程中运行两类采集舱，断言它们不发生任何 Clock 引起的崩溃。
"""
import sys
import os
import time
import asyncio
import threading
import asyncpg

# 设置 stdout UTF-8 编码防 emoji 崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 引入 worker 模块
try:
    from activity_worker import ProcessActivityWorker
    from hardware_worker import HardwareTelemetryWorker
except ImportError as e:
    print(f"❌ 导入依赖失败，请确保在 E:\\TimeAudit 目录下执行！Error: {e}")
    sys.exit(1)

test_failed = False

def run_in_subthread(func, *args):
    """在一个完全没有 event_loop 的独立物理线程中执行函数，并捕获任何异常"""
    exception_holder = []
    def thread_target():
        try:
            res = func(*args)
            exception_holder.append((True, res))
        except Exception as e:
            exception_holder.append((False, e))
            
    t = threading.Thread(target=thread_target)
    t.start()
    t.join(timeout=15) # 设定超时防卡死
    if t.is_alive():
        return False, TimeoutError("线程采集超时！")
    if not exception_holder:
        return False, RuntimeError("线程无响应退出！")
    return exception_holder[0]

async def run_async_to_thread(func, *args):
    """通过 asyncio.to_thread 在工作线程中执行，并捕获异常"""
    try:
        res = await asyncio.to_thread(func, *args)
        return True, res
    except Exception as e:
        return False, e

def test_clocks_regression():
    global test_failed
    print("=" * 60)
    print("  TimeAudit 子线程时钟安全测试")
    print("=" * 60)

    # 1. 初始化 Worker
    print("[1] 正在初始化 Activity & Hardware Telemetry Workers...")
    try:
        act_worker = ProcessActivityWorker()
        hw_worker = HardwareTelemetryWorker()
        print("  ✓ Workers 初始化完成！")
    except Exception as e:
        print(f"  ❌ Workers 初始化失败: {e}")
        test_failed = True
        return

    # 2. 在物理子线程中测试 (无 Event Loop)
    print("\n[2] 测试: 物理子线程采集测试 (不依赖 Event Loop)...")
    
    ok, res = run_in_subthread(act_worker.collect_active_processes)
    if ok:
        print(f"  ✓ ProcessActivityWorker.collect_active_processes() 成功返回 {len(res)} 个进程快照！")
    else:
        print(f"  ❌ ProcessActivityWorker 采集发生异常: {res}")
        test_failed = True

    ok, res = run_in_subthread(hw_worker.collect_hardware_snapshot, "Idle")
    if ok:
        print("  ✓ HardwareTelemetryWorker.collect_hardware_snapshot() 成功采集硬件快照！")
    else:
        print(f"  ❌ HardwareTelemetryWorker 采集发生异常: {res}")
        test_failed = True

    # 3. 通过 asyncio.to_thread 异步测试 (主事件循环子工作线程)
    print("\n[3] 测试: asyncio.to_thread 工作线程采集测试...")
    
    async def async_test_wrapper():
        global test_failed
        ok, res = await run_async_to_thread(act_worker.collect_active_processes)
        if ok:
            print(f"  ✓ asyncio.to_thread 下 ProcessActivityWorker.collect_active_processes() 成功！")
        else:
            print(f"  ❌ asyncio.to_thread 下 ProcessActivityWorker 报错: {res}")
            test_failed = True

        ok, res = await run_async_to_thread(hw_worker.collect_hardware_snapshot, "Idle")
        if ok:
            print("  ✓ asyncio.to_thread 下 HardwareTelemetryWorker.collect_hardware_snapshot() 成功！")
        else:
            print(f"  ❌ asyncio.to_thread 下 HardwareTelemetryWorker 报错: {res}")
            test_failed = True

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(async_test_wrapper())

    print("\n" + "=" * 60)
    if not test_failed:
        print("  🎉 ALL CLOCK TESTS PASSED! 时钟安全测试全部通过！")
    else:
        print("  ⚠️ CLOCK TESTS FAILED! 时钟安全测试存在失败项！")
    print("=" * 60)

if __name__ == "__main__":
    test_clocks_regression()
    sys.exit(1 if test_failed else 0)
