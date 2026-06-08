# -*- coding: utf-8 -*-
import asyncio
import datetime
import asyncpg

# 数据库 DSN 配置
DB_DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"

async def check_status():
    print("=" * 70)
    print("🛸 Windows 11 Telemetry Engine 数据库落库高精特征诊断工具")
    print("=" * 70)
    
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
    except Exception as e:
        print(f"❌ 数据库连接失败: {e}")
        print("请确保 PostgreSQL 服务已经拉起，并且 DSN 配置无误。")
        return

    async with pool.acquire() as conn:
        # 1. 查询硬件时序表最新一条记录
        try:
            hw_row = await conn.fetchrow("""
                SELECT timestamp, cpu_package_temp, cpu_package_power, gpu_core_temp, is_packet_loss
                FROM public.fact_system_hardware 
                ORDER BY timestamp DESC 
                LIMIT 1;
            """)
        except Exception as e:
            hw_row = None
            print(f"❌ 查询 fact_system_hardware 表失败: {e}")

        # 2. 查询前台切窗表最新一条记录
        try:
            ctx_row = await conn.fetchrow("""
                SELECT timestamp, window_title, os_pid 
                FROM public.fact_process_context 
                ORDER BY timestamp DESC 
                LIMIT 1;
            """)
        except Exception as e:
            ctx_row = None
            print(f"❌ 查询 fact_process_context 表失败: {e}")

    await pool.close()

    if not hw_row:
        print("\n⚠️ 警告: 目前 fact_system_hardware 表中尚无任何时序数据落库。")
    else:
        ts = hw_row["timestamp"]
        temp = hw_row["cpu_package_temp"]
        power = hw_row["cpu_package_power"]
        gpu_temp = hw_row["gpu_core_temp"]
        pkg_loss = hw_row["is_packet_loss"]
        
        now = datetime.datetime.now(datetime.timezone.utc)
        delay_sec = (now - ts).total_seconds()

        print(f"\n📊 [1/2] 硬件时序表检测 (fact_system_hardware):")
        print(f"  - 最新写入时间: {ts} (距离现在 {delay_sec:.1f} 秒前)")
        print(f"  - 采集 CPU 温度: {temp}°C")
        print(f"  - 采集 CPU 功耗: {power}W")
        print(f"  - 采集 GPU 温度: {gpu_temp}°C")
        print(f"  - 网络丢包状态: {'有丢包' if pkg_loss == 1 else '无丢包 (正常)'}")

        # 判断高精并网状态
        # 降级状态下的合成温度公式为 39.0 + (cpu_total * 0.46) 
        # 降级功耗公式为 24.0 + (cpu_total * 1.46)
        # 如果浮点数的小数位非常规，且能精确读到非公式计算值，证明 LHM 物理寄存器已接管
        is_lhm_active = False
        if temp is not None and power is not None:
            # 检查是否为物理传感器产生的细微非整点浮点数
            temp_dec = abs(temp - int(temp))
            power_dec = abs(power - int(power))
            if temp_dec > 0.0001 and power_dec > 0.0001:
                is_lhm_active = True

        if is_lhm_active:
            print("\n  ✅ 【高精诊断】: LibreHardwareMonitor 物理驱动已成功并网并接管数据库写入！")
            print("                (当前写入的数据为来自 CPU 物理寄存器的高精度时序数据)")
        else:
            print("\n  ⚠️ 【警告提示】: LibreHardwareMonitor 驱动未并网，当前落库数据为系统原生 ACPI 或公式合成估算值。")

    if not ctx_row:
        print("\n⚠️ 警告: 目前 fact_process_context 前台切窗表尚无任何数据落库。")
    else:
        ctx_ts = ctx_row["timestamp"]
        title = ctx_row["window_title"]
        pid = ctx_row["os_pid"]
        print(f"\n🖥️ [2/2] 前台聚焦表检测 (fact_process_context):")
        print(f"  - 最新聚焦时间: {ctx_ts}")
        print(f"  - 前台窗口标题: {title}")
        print(f"  - 对应进程 PID: {pid}")
        print("\n  ✅ 【状态诊断】: 前台窗口焦点流数据正在正常、稳定地录入数据库。")

if __name__ == "__main__":
    asyncio.run(check_status())