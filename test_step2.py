import os
import sys
import asyncio
import asyncpg
import time
import datetime
import psutil

# 确保 test_step2.py 与 hardware_worker.py 处于同一目录下
try:
    from hardware_worker import HardwareTelemetryWorker
except ImportError:
    print("❌ 错误：请确保 test_step2.py 与修改后的 hardware_worker.py 放在同一目录下。")
    sys.exit(1)

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"

async def test_database_hardware_insert(pool, data, ccd0_val, ccd1_val):
    """
    测试将 29 个标准硬件指标 + 2 个 9950X3D CCD 专属负载指标 
    安全写入已执行 ALTER TABLE 变更的分区数仓
    """
    print("\n--- 3. 数据库 31 列全维写入测试 ---")
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # 31 字段全维 SQL（包含您刚刚扩充的两个 CCD 字段）
    query = """
        INSERT INTO public.fact_system_hardware 
        ("timestamp", current_fps, average_fps, one_percent_low_fps, frametime_ms, frametime_jitter,
         cpu_total_usage, cpu_vcore_voltage, cpu_clock_mhz, cpu_package_temp, cpu_package_power, 
         system_dpc_latency, system_context_switches, gpu_usage, gpu_core_voltage, gpu_core_clock, gpu_mem_clock, 
         gpu_core_temp, gpu_hotspot_temp, gpu_board_power, gpu_throttling_reasons, pcie_bus_utilization,
         system_ram_usage_pct, system_commit_size_gb, system_hard_page_faults, disk_max_latency_ms,
         network_ping_ms, is_packet_loss, network_jitter, cpu_ccd0_usage, cpu_ccd1_usage)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31);
    """
    
    try:
        async with pool.acquire() as conn:
            # 执行写入
            await conn.execute(
                query, now, data["current_fps"], data["average_fps"], data["one_percent_low_fps"],
                data["frametime_ms"], data["frametime_jitter"], data["cpu_total_usage"], data["cpu_vcore_voltage"],
                data["cpu_clock_mhz"], data["cpu_package_temp"], data["cpu_package_power"],
                data["system_dpc_latency"], data["system_context_switches"], data["gpu_usage"], data["gpu_core_voltage"],
                data["gpu_core_clock"], data["gpu_mem_clock"], data["gpu_core_temp"], data["gpu_hotspot_temp"],
                data["gpu_board_power"], data["gpu_throttling_reasons"], data["pcie_bus_utilization"],
                data["system_ram_usage_pct"], data["system_commit_size_gb"], data["system_hard_page_faults"],
                data["disk_max_latency_ms"], data["network_ping_ms"], data["is_packet_loss"], data["network_jitter"],
                ccd0_val, ccd1_val
            )
            print("✅ 数据库 31 字段全维物理写入成功！分区子表结构完全兼容。")
            
            # 清理临时测试行，避免干扰正式大盘历史账单
            await conn.execute("DELETE FROM public.fact_system_hardware WHERE \"timestamp\" = $1;", now)
            print("🧹 物理测试垃圾行已成功清理。")
    except Exception as e:
        print(f"❌ 数据库 31 字段全维写入测试失败。错误详情:\n{e}")
        print("💡 提示：如果提示列不存在，请确认主表的 ALTER TABLE 语句是否执行成功，并且重启了测试脚本。")

def print_indicator(name, val, unit=""):
    """美化打印物理遥测指标"""
    if val is None:
        status = "⚠️ 传感器未就绪或未检测到设备"
    else:
        status = f"{val:.2f} {unit}" if isinstance(val, float) else f"{val} {unit}"
    print(f"   [📊 {name:<20}] -> {status}")

def run_hardware_profiler():
    """执行 9950X3D 与 RTX 5080 全景硬件遥测"""
    print("\n--- 1. 启动全景硬件物理特征捕获 ---")
    worker = HardwareTelemetryWorker()
    
    print("⏳ 正在为高精度 CPPC 引擎、PDH 物理通道及 DPC 监测线程预热（等待 3 秒）...")
    time.sleep(3.0)
    
    # 采集瞬时切面
    snapshot = worker.collect_hardware_snapshot("test_game.exe")
    
    print("\n--- 2. 物理遥测快照输出 (1Hz 截面) ---")
    print_indicator("CPU 总负载", snapshot["cpu_total_usage"], "%")
    print_indicator("CPU 核心主频", snapshot["cpu_clock_mhz"], "MHz")
    print_indicator("CPU 核心电压", snapshot["cpu_vcore_voltage"], "V")
    print_indicator("CPU 封装温度", snapshot["cpu_package_temp"], "°C")
    print_indicator("CPU 封装功耗", snapshot["cpu_package_power"], "W")
    print_indicator("DPC 调度延迟", snapshot["system_dpc_latency"], "微秒 (us)")
    print_indicator("系统上下文切换", snapshot["system_context_switches"], "次/秒")
    print_indicator("系统物理硬页面错误", snapshot["system_hard_page_faults"], "页/秒")
    print_indicator("磁盘最大 IO 延迟", snapshot["disk_max_latency_ms"], "毫秒 (ms)")
    
    print("-" * 50)
    print_indicator("GPU 核心温度", snapshot["gpu_core_temp"], "°C")
    print_indicator("GPU 热点温度", snapshot["gpu_hotspot_temp"], "°C")
    print_indicator("GPU 整板功耗", snapshot["gpu_board_power"], "W")
    print_indicator("GPU 核心频率", snapshot["gpu_core_clock"], "MHz")
    print_indicator("GPU 显存频率", snapshot["gpu_mem_clock"], "MHz")
    print_indicator("GPU 降频原因掩码", snapshot["gpu_throttling_reasons"])
    print_indicator("PCIe Gen5 总线负载", snapshot["pcie_bus_utilization"], "%")
    
    print("-" * 50)
    print_indicator("网络时延(114 PING)", snapshot["network_ping_ms"], "ms")
    print_indicator("网络抖动(Jitter)", snapshot["network_jitter"], "ms")
    print_indicator("网络丢包状态", snapshot["is_packet_loss"])
    
    # 手动提取 9950X3D 不对称 CCD 瞬时分布
    ccd0_load = ccd1_load = None
    try:
        cpu_percents = psutil.cpu_percent(interval=None, percpu=True)
        if len(cpu_percents) >= 32:
            ccd0_load = sum(cpu_percents[0:16]) / 16.0
            ccd1_load = sum(cpu_percents[16:32]) / 16.0
            print("-" * 50)
            print_indicator("9950X3D CCD0 负载", ccd0_load, "% (V-Cache 核心)")
            print_indicator("9950X3D CCD1 负载", ccd1_load, "% (常规高频核心)")
            print_indicator("CCD 调度失衡偏置", ccd0_load - ccd1_load, "%")
    except:
        pass
        
    worker.terminate()
    return snapshot, ccd0_load, ccd1_load

async def main():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 阶段 2 隔离测试")
    print("====================================================")
    
    # 1. 运行系统物理探针检测
    snapshot, ccd0_val, ccd1_val = run_hardware_profiler()
    
    # 2. 运行数据库级 31 字段闭环写入测试
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
        await test_database_hardware_insert(pool, snapshot, ccd0_val, ccd1_val)
        await pool.close()
    except Exception as e:
        print(f"\n❌ 无法连接到 PostgreSQL 数据库以完成第3项测试，请检查容器状态。错误: {e}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())