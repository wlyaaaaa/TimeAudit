# -*- coding: utf-8 -*-
"""
深度数据库审计脚本 - 逐表逐字段校验数据正确性
"""
import asyncio
import asyncpg
import sys
import io
from datetime import datetime, timezone

# 强制标准输出使用 UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

DB_DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"


async def run():
    conn = await asyncpg.connect(DB_DSN)
    
    print("=" * 80)
    print("  TimeAudit 深度数据库审计报告")
    print(f"  审计时间: {datetime.now()}")
    print("=" * 80)
    
    # ============================================================
    # 1. 各表行数统计
    # ============================================================
    print("\n[1] 各表行数统计")
    print("-" * 60)
    tables = [
        "fact_system_hardware",
        "fact_process_activity", 
        "fact_process_context",
        "fact_process_lifecycle_events",
        "dim_process_registry",
        "app_usage_logs"
    ]
    for t in tables:
        count = await conn.fetchval(f"SELECT COUNT(*) FROM public.{t}")
        print(f"  {t}: {count} 行")
    
    # ============================================================
    # 2. fact_system_hardware - 硬件遥测数据审计
    # ============================================================
    print("\n[2] fact_system_hardware - 硬件遥测数据深度审计")
    print("-" * 60)
    
    hw_count = await conn.fetchval("SELECT COUNT(*) FROM public.fact_system_hardware")
    print(f"  总记录数: {hw_count}")
    
    if hw_count > 0:
        # 2a. 检查时间间隔是否稳定 (~3秒)
        print("\n  [2a] 采集间隔分析 (最新10个):")
        ts_rows = await conn.fetch("""
            SELECT timestamp FROM public.fact_system_hardware 
            ORDER BY timestamp DESC LIMIT 11
        """)
        timestamps = [r['timestamp'] for r in ts_rows]
        interval_issues = 0
        for i in range(len(timestamps)-1):
            diff = (timestamps[i] - timestamps[i+1]).total_seconds()
            status = "✅" if 1 <= diff <= 10 else "❌ 异常"
            if diff > 10 or diff < 1:
                interval_issues += 1
            print(f"    {diff:.1f}s {status}")
        
        # 2b. 数值范围合理性检查
        print("\n  [2b] 数值范围合理性:")
        c = await conn.fetchrow("""
            SELECT
                MIN(cpu_total_usage) as cpu_min, MAX(cpu_total_usage) as cpu_max,
                MIN(gpu_usage) as gpu_min, MAX(gpu_usage) as gpu_max,
                MIN(cpu_package_temp) as cpu_temp_min, MAX(cpu_package_temp) as cpu_temp_max,
                MIN(gpu_core_temp) as gpu_temp_min, MAX(gpu_core_temp) as gpu_temp_max,
                MIN(gpu_hotspot_temp) as hs_min, MAX(gpu_hotspot_temp) as hs_max,
                MIN(system_ram_usage_pct) as ram_min, MAX(system_ram_usage_pct) as ram_max,
                MIN(current_fps) as fps_min, MAX(current_fps) as fps_max,
                MIN(cpu_package_power) as cpu_pw_min, MAX(cpu_package_power) as cpu_pw_max,
                MIN(gpu_board_power) as gpu_pw_min, MAX(gpu_board_power) as gpu_pw_max,
                MIN(system_dpc_latency) as dpc_min, MAX(system_dpc_latency) as dpc_max,
                MIN(system_context_switches) as ctx_min, MAX(system_context_switches) as ctx_max,
                MIN(cpu_clock_mhz) as clk_min, MAX(cpu_clock_mhz) as clk_max,
                MIN(cpu_vcore_voltage) as vcr_min, MAX(cpu_vcore_voltage) as vcr_max,
                MIN(gpu_core_voltage) as gvcr_min, MAX(gpu_core_voltage) as gvcr_max,
                MIN(network_ping_ms) as ping_min, MAX(network_ping_ms) as ping_max,
                MIN(system_commit_size_gb) as commit_min, MAX(system_commit_size_gb) as commit_max,
                MIN(frametime_ms) as ft_min, MAX(frametime_ms) as ft_max,
                COUNT(*) FILTER (WHERE cpu_total_usage < 0 OR cpu_total_usage > 100) as cpu_oob,
                COUNT(*) FILTER (WHERE gpu_usage < 0 OR gpu_usage > 100) as gpu_oob,
                COUNT(*) FILTER (WHERE cpu_package_temp < 0 OR cpu_package_temp > 120) as ct_oob,
                COUNT(*) FILTER (WHERE gpu_core_temp < 0 OR gpu_core_temp > 120) as gt_oob,
                COUNT(*) FILTER (WHERE system_ram_usage_pct < 0 OR system_ram_usage_pct > 100) as ram_oob,
                COUNT(*) FILTER (WHERE current_fps < 0) as fps_neg,
                COUNT(*) FILTER (WHERE frametime_ms < 0) as ft_neg,
                COUNT(*) FILTER (WHERE cpu_package_power < 0) as cpu_pw_neg,
                COUNT(*) FILTER (WHERE gpu_board_power < 0) as gpu_pw_neg,
                COUNT(*) FILTER (WHERE system_dpc_latency < 0) as dpc_neg,
                COUNT(*) FILTER (WHERE system_context_switches < 0) as ctx_neg,
                COUNT(*) FILTER (WHERE network_ping_ms < 0) as ping_neg,
                COUNT(*) FILTER (WHERE system_commit_size_gb < 0) as commit_neg,
                COUNT(*) FILTER (WHERE system_commit_size_gb > 256) as commit_huge,
                COUNT(*) FILTER (WHERE cpu_clock_mhz > 8000) as clk_huge,
                COUNT(*) FILTER (WHERE cpu_clock_mhz < 500 AND cpu_clock_mhz > 0) as clk_low,
                COUNT(*) as total
            FROM public.fact_system_hardware
        """)
        print(f"    CPU 使用率: {c['cpu_min']:.1f} ~ {c['cpu_max']:.1f}%  越界={c['cpu_oob']}")
        print(f"    GPU 使用率: {c['gpu_min']:.1f} ~ {c['gpu_max']:.1f}%  越界={c['gpu_oob']}")
        print(f"    CPU 温度:   {c['cpu_temp_min']:.1f} ~ {c['cpu_temp_max']:.1f}°C  越界={c['ct_oob']}")
        print(f"    GPU 核心温度: {c['gpu_temp_min']:.1f} ~ {c['gpu_temp_max']:.1f}°C  越界={c['gt_oob']}")
        print(f"    GPU 热点温度: {c['hs_min']:.1f} ~ {c['hs_max']:.1f}°C")
        print(f"    RAM 使用率: {c['ram_min']:.1f} ~ {c['ram_max']:.1f}%  越界={c['ram_oob']}")
        print(f"    FPS:        {c['fps_min']:.1f} ~ {c['fps_max']:.1f}  负数={c['fps_neg']}")
        print(f"    帧时间:     {c['ft_min']:.2f} ~ {c['ft_max']:.2f}ms  负数={c['ft_neg']}")
        print(f"    CPU 功耗:   {c['cpu_pw_min']:.1f} ~ {c['cpu_pw_max']:.1f}W  负数={c['cpu_pw_neg']}")
        print(f"    GPU 功耗:   {c['gpu_pw_min']:.1f} ~ {c['gpu_pw_max']:.1f}W  负数={c['gpu_pw_neg']}")
        print(f"    CPU 主频:   {c['clk_min']} ~ {c['clk_max']}MHz  异常高={c['clk_huge']} 异常低={c['clk_low']}")
        print(f"    CPU Vcore:  {c['vcr_min']} ~ {c['vcr_max']}V")
        print(f"    GPU Vcore:  {c['gvcr_min']} ~ {c['gvcr_max']}V")
        print(f"    DPC延迟:    {c['dpc_min']:.1f} ~ {c['dpc_max']:.1f}μs  负数={c['dpc_neg']}")
        print(f"    上下文切换: {c['ctx_min']} ~ {c['ctx_max']}/s  负数={c['ctx_neg']}")
        print(f"    网络Ping:   {c['ping_min']} ~ {c['ping_max']}ms  负数={c['ping_neg']}")
        print(f"    提交内存:   {c['commit_min']:.1f} ~ {c['commit_max']:.1f}GB  负数={c['commit_neg']} 超256G={c['commit_huge']}")
        
        # 2c. NULL 值分布
        print(f"\n  [2c] NULL 值分布 (共{c['total']}行):")
        null_cols = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE current_fps IS NULL) as n1,
                COUNT(*) FILTER (WHERE average_fps IS NULL) as n2,
                COUNT(*) FILTER (WHERE cpu_total_usage IS NULL) as n3,
                COUNT(*) FILTER (WHERE gpu_usage IS NULL) as n4,
                COUNT(*) FILTER (WHERE cpu_package_temp IS NULL) as n5,
                COUNT(*) FILTER (WHERE gpu_core_temp IS NULL) as n6,
                COUNT(*) FILTER (WHERE system_ram_usage_pct IS NULL) as n7,
                COUNT(*) FILTER (WHERE cpu_vcore_voltage IS NULL) as n8,
                COUNT(*) FILTER (WHERE gpu_core_voltage IS NULL) as n9,
                COUNT(*) FILTER (WHERE network_ping_ms IS NULL) as n10,
                COUNT(*) FILTER (WHERE disk_max_latency_ms IS NULL) as n11,
                COUNT(*) FILTER (WHERE cpu_ccd0_usage IS NULL) as n12,
                COUNT(*) FILTER (WHERE cpu_ccd1_usage IS NULL) as n13,
                COUNT(*) FILTER (WHERE system_commit_size_gb IS NULL) as n14,
                COUNT(*) FILTER (WHERE frametime_jitter IS NULL) as n15,
                COUNT(*) as total
            FROM public.fact_system_hardware
        """)
        null_names = ['current_fps','average_fps','cpu_total_usage','gpu_usage','cpu_package_temp',
                      'gpu_core_temp','system_ram_usage_pct','cpu_vcore_voltage','gpu_core_voltage',
                      'network_ping_ms','disk_max_latency_ms','cpu_ccd0_usage','cpu_ccd1_usage',
                      'system_commit_size_gb','frametime_jitter']
        for i, name in enumerate(null_names, 1):
            cnt = null_cols[f'n{i}']
            pct = cnt / null_cols['total'] * 100 if null_cols['total'] > 0 else 0
            flag = "⚠️" if pct > 50 else ("🔶" if pct > 10 else "✅")
            print(f"      {flag} {name}: {cnt}/{null_cols['total']} ({pct:.1f}%)")
        
        # 2d. FPS 为 0 的占比 - 关键检查
        fps_zero = await conn.fetchrow("""
            SELECT 
                COUNT(*) FILTER (WHERE current_fps = 0) as fps_zero,
                COUNT(*) FILTER (WHERE current_fps > 0) as fps_positive,
                COUNT(*) FILTER (WHERE average_fps = 0) as avg_zero,
                COUNT(*) FILTER (WHERE one_percent_low_fps = 0) as low_zero,
                COUNT(*) FILTER (WHERE frametime_ms = 0) as ft_zero,
                COUNT(*) as total
            FROM public.fact_system_hardware
        """)
        print(f"\n  [2d] FPS 零值分析 (关键!):")
        print(f"    current_fps = 0: {fps_zero['fps_zero']}/{fps_zero['total']} ({fps_zero['fps_zero']/fps_zero['total']*100:.1f}%)")
        print(f"    average_fps = 0: {fps_zero['avg_zero']}/{fps_zero['total']} ({fps_zero['avg_zero']/fps_zero['total']*100:.1f}%)")
        print(f"    one_percent_low = 0: {fps_zero['low_zero']}/{fps_zero['total']} ({fps_zero['low_zero']/fps_zero['total']*100:.1f}%)")
        print(f"    frametime_ms = 0: {fps_zero['ft_zero']}/{fps_zero['total']} ({fps_zero['ft_zero']/fps_zero['total']*100:.1f}%)")
        print(f"    current_fps > 0: {fps_zero['fps_positive']}/{fps_zero['total']}")
        
        # 2e. 物理真实性校验 - 关键
        print(f"\n  [2e] 物理真实性检查:")
        phys = await conn.fetchrow("""
            SELECT 
                COUNT(*) FILTER (WHERE gpu_hotspot_temp > 0 AND gpu_core_temp > 0 AND gpu_hotspot_temp < gpu_core_temp) as hotspot_lt_core,
                COUNT(*) FILTER (WHERE gpu_hotspot_temp > 0 AND gpu_core_temp > 0 AND (gpu_hotspot_temp - gpu_core_temp) > 30) as hotspot_gap_huge,
                COUNT(*) FILTER (WHERE cpu_package_power > 0 AND cpu_total_usage < 5 AND cpu_package_power > 100) as idle_high_power,
                COUNT(*) FILTER (WHERE gpu_board_power > 0 AND gpu_usage < 5 AND gpu_board_power > 100) as gpu_idle_high_power,
                COUNT(*) FILTER (WHERE current_fps > 0 AND frametime_ms > 0 AND ABS(1000.0/frametime_ms - current_fps) / GREATEST(current_fps, 1) > 5) as fps_ft_mismatch,
                COUNT(*) FILTER (WHERE cpu_vcore_voltage > 1.6) as vcore_danger,
                COUNT(*) FILTER (WHERE gpu_core_voltage IS NOT NULL AND gpu_core_voltage > 1.2) as gvcore_danger,
                COUNT(*) as total
            FROM public.fact_system_hardware
        """)
        print(f"    GPU热点<核心温度(物理违规): {phys['hotspot_lt_core']} {'❌' if phys['hotspot_lt_core'] > 0 else '✅'}")
        print(f"    GPU热点-核心>30°C(异常): {phys['hotspot_gap_huge']} {'⚠️' if phys['hotspot_gap_huge'] > 0 else '✅'}")
        print(f"    CPU空闲但功耗>100W(异常): {phys['idle_high_power']} {'⚠️' if phys['idle_high_power'] > 0 else '✅'}")
        print(f"    GPU空闲但功耗>100W(异常): {phys['gpu_idle_high_power']} {'⚠️' if phys['gpu_idle_high_power'] > 0 else '✅'}")
        print(f"    FPS与帧时间严重不一致: {phys['fps_ft_mismatch']} {'⚠️' if phys['fps_ft_mismatch'] > 0 else '✅'}")
        print(f"    CPU Vcore > 1.6V(危险): {phys['vcore_danger']} {'❌' if phys['vcore_danger'] > 0 else '✅'}")
        print(f"    GPU Vcore > 1.2V(危险): {phys['gvcore_danger']} {'⚠️' if phys['gvcore_danger'] > 0 else '✅'}")
    
    # ============================================================
    # 3. dim_process_registry 审计
    # ============================================================
    print("\n\n[3] dim_process_registry - 进程注册表审计")
    print("-" * 60)
    reg_count = await conn.fetchval("SELECT COUNT(*) FROM public.dim_process_registry")
    print(f"  总注册数: {reg_count}")
    
    if reg_count > 0:
        rn = await conn.fetchrow("""
            SELECT 
                COUNT(*) FILTER (WHERE process_name IS NULL) as name_null,
                COUNT(*) FILTER (WHERE executable_path IS NULL) as path_null,
                COUNT(*) FILTER (WHERE process_name = '') as name_empty,
                COUNT(*) FILTER (WHERE executable_path = '') as path_empty,
                COUNT(DISTINCT process_name) as unique_names,
                COUNT(*) FILTER (WHERE signature_status NOT IN (-2,-1,0,1)) as sig_invalid,
                COUNT(*) FILTER (WHERE is_elevated NOT IN (-2,-1,0,1)) as elev_invalid,
                COUNT(*) FILTER (WHERE signature_status = 1) as signed_count,
                COUNT(*) FILTER (WHERE signature_status = 0) as unsigned_count,
                COUNT(*) FILTER (WHERE is_elevated = 1) as elevated_count,
                COUNT(*) as total
            FROM public.dim_process_registry
        """)
        print(f"  process_name NULL: {rn['name_null']}, 空串: {rn['name_empty']}")
        print(f"  executable_path NULL: {rn['path_null']}, 空串: {rn['path_empty']}")
        print(f"  唯一进程名数: {rn['unique_names']}")
        print(f"  签名状态: 已签名={rn['signed_count']}, 未签名={rn['unsigned_count']}, 非法值={rn['sig_invalid']}")
        print(f"  提权状态: 管理员={rn['elevated_count']}, 非法值={rn['elev_invalid']}")
        
        # 重复 process_key 检查
        dups = await conn.fetch("""
            SELECT process_key, COUNT(*) as cnt 
            FROM public.dim_process_registry 
            GROUP BY process_key 
            HAVING COUNT(*) > 1 LIMIT 5
        """)
        print(f"  process_key 重复: {len(dups)} 个 {'❌' if dups else '✅'}")
        
        # 样本
        samples = await conn.fetch("""
            SELECT process_key, process_name, executable_path, parent_process, 
                   is_elevated, signature_status
            FROM public.dim_process_registry 
            ORDER BY process_key DESC LIMIT 5
        """)
        print(f"\n  最新5个注册:")
        for s in samples:
            print(f"    key={s['process_key']}: {s['process_name']} | {str(s['executable_path'])[:50]} | elev={s['is_elevated']} sig={s['signature_status']}")
    
    # ============================================================
    # 4. fact_process_activity 审计
    # ============================================================
    print("\n\n[4] fact_process_activity - 进程活动数据审计")
    print("-" * 60)
    act_count = await conn.fetchval("SELECT COUNT(*) FROM public.fact_process_activity")
    print(f"  总记录数: {act_count}")
    
    if act_count > 0:
        # 引用完整性
        orphan = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_activity a
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dim_process_registry r 
                WHERE r.process_key = a.process_key
            )
        """)
        print(f"  process_key 引用完整性: 孤儿记录={orphan} {'❌' if orphan > 0 else '✅'}")
        
        # 数值范围
        ac = await conn.fetchrow("""
            SELECT 
                MIN(proc_cpu_usage) as cpu_min, MAX(proc_cpu_usage) as cpu_max,
                COUNT(*) FILTER (WHERE proc_cpu_usage > 100) as cpu_over,
                COUNT(*) FILTER (WHERE proc_cpu_usage < 0) as cpu_neg,
                MIN(proc_gpu_usage) as gpu_min, MAX(proc_gpu_usage) as gpu_max,
                COUNT(*) FILTER (WHERE proc_gpu_usage > 100) as gpu_over,
                COUNT(*) FILTER (WHERE proc_gpu_usage < 0) as gpu_neg,
                MIN(proc_ram_mb) as ram_min, MAX(proc_ram_mb) as ram_max,
                COUNT(*) FILTER (WHERE proc_ram_mb < 0) as ram_neg,
                MIN(proc_vram_used_gb) as vram_min, MAX(proc_vram_used_gb) as vram_max,
                COUNT(*) FILTER (WHERE proc_vram_used_gb > 16.5) as vram_phantom,
                MIN(proc_vram_shared_mb) as vsm_min, MAX(proc_vram_shared_mb) as vsm_max,
                COUNT(*) FILTER (WHERE proc_disk_read_rate_mb < 0) as dr_neg,
                COUNT(*) FILTER (WHERE proc_disk_write_rate_mb < 0) as dw_neg,
                COUNT(*) FILTER (WHERE proc_network_send_kb < 0) as ns_neg,
                COUNT(*) FILTER (WHERE proc_network_recv_kb < 0) as nr_neg,
                COUNT(*) FILTER (WHERE proc_thread_count <= 0) as thr_zero,
                COUNT(*) FILTER (WHERE is_not_responding = 1) as not_resp,
                COUNT(*) as total
            FROM public.fact_process_activity
        """)
        print(f"  proc_cpu_usage: {ac['cpu_min']:.2f} ~ {ac['cpu_max']:.2f}%  >100%={ac['cpu_over']} <0={ac['cpu_neg']}")
        print(f"  proc_gpu_usage: {ac['gpu_min']:.2f} ~ {ac['gpu_max']:.2f}%  >100%={ac['gpu_over']} <0={ac['gpu_neg']}")
        print(f"  proc_ram_mb: {ac['ram_min']} ~ {ac['ram_max']}MB  负数={ac['ram_neg']}")
        print(f"  proc_vram_used_gb: {ac['vram_min']:.2f} ~ {ac['vram_max']:.2f}GB  >16.5GB幻影={ac['vram_phantom']}")
        print(f"  proc_vram_shared_mb: {ac['vsm_min']} ~ {ac['vsm_max']}MB")
        print(f"  磁盘读/写负数: {ac['dr_neg']}/{ac['dw_neg']}")
        print(f"  网络发送/接收负数: {ac['ns_neg']}/{ac['nr_neg']}")
        print(f"  线程数<=0: {ac['thr_zero']}")
        print(f"  未响应: {ac['not_resp']}")
        
        # 每轮进程数量
        print(f"\n  [4a] 每轮采集进程数量 (最新10轮):")
        per_round = await conn.fetch("""
            SELECT timestamp, COUNT(*) as proc_count
            FROM public.fact_process_activity
            GROUP BY timestamp ORDER BY timestamp DESC LIMIT 10
        """)
        for pr in per_round:
            print(f"    {pr['timestamp']}: {pr['proc_count']} 进程")
        
        # 检查 CPU 使用率之和是否接近 cpu_total_usage
        print(f"\n  [4b] CPU 使用率整机口径校验 (最新5轮):")
        cpu_sum = await conn.fetch("""
            SELECT a.timestamp, 
                   SUM(a.proc_cpu_usage) as sum_proc_cpu,
                   h.cpu_total_usage as hw_cpu_total
            FROM public.fact_process_activity a
            JOIN public.fact_system_hardware h ON h.timestamp = a.timestamp
            GROUP BY a.timestamp, h.cpu_total_usage
            ORDER BY a.timestamp DESC
            LIMIT 5
        """)
        for cs in cpu_sum:
            ratio = cs['sum_proc_cpu'] / cs['hw_cpu_total'] * 100 if cs['hw_cpu_total'] and cs['hw_cpu_total'] > 0 else 0
            status = "✅" if 50 < ratio < 200 else "⚠️"
            print(f"    {cs['timestamp']}: Σproc_cpu={cs['sum_proc_cpu']:.1f}% hw_total={cs['hw_cpu_total']:.1f}% 比值={ratio:.0f}% {status}")
        
        # 检查 GPU 使用率之和是否超过系统 GPU
        print(f"\n  [4c] GPU 使用率整机口径校验 (最新5轮):")
        gpu_sum = await conn.fetch("""
            SELECT a.timestamp,
                   SUM(a.proc_gpu_usage) as sum_proc_gpu,
                   h.gpu_usage as hw_gpu_total
            FROM public.fact_process_activity a
            JOIN public.fact_system_hardware h ON h.timestamp = a.timestamp
            GROUP BY a.timestamp, h.gpu_usage
            ORDER BY a.timestamp DESC
            LIMIT 5
        """)
        for gs in gpu_sum:
            status = "✅" if gs['sum_proc_gpu'] <= (gs['hw_gpu_total'] or 0) * 1.5 + 5 else "⚠️"
            print(f"    {cs['timestamp']}: Σproc_gpu={gs['sum_proc_gpu']:.1f}% hw_total={gs['hw_gpu_total']:.1f}% {status}")

    # ============================================================
    # 5. fact_process_context 审计
    # ============================================================
    print("\n\n[5] fact_process_context - 前台焦点数据审计")
    print("-" * 60)
    ctx_count = await conn.fetchval("SELECT COUNT(*) FROM public.fact_process_context")
    print(f"  总记录数: {ctx_count}")
    
    if ctx_count > 0:
        # 引用完整性
        orphan = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_context c
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dim_process_registry r WHERE r.process_key = c.process_key
            )
        """)
        print(f"  引用完整性: 孤儿={orphan} {'❌' if orphan > 0 else '✅'}")
        
        dc = await conn.fetchrow("""
            SELECT 
                MIN(duration_ms) as dur_min, MAX(duration_ms) as dur_max, AVG(duration_ms)::bigint as dur_avg,
                COUNT(*) FILTER (WHERE duration_ms < 0) as dur_neg,
                COUNT(*) FILTER (WHERE duration_ms > 86400000) as dur_over_24h,
                COUNT(*) FILTER (WHERE end_timestamp IS NULL) as end_null,
                COUNT(*) FILTER (WHERE end_timestamp IS NOT NULL AND end_timestamp < timestamp) as end_before_start,
                COUNT(*) FILTER (WHERE is_foreground NOT IN (0,1)) as fg_invalid,
                COUNT(*) FILTER (WHERE is_foreground = 1) as fg_count,
                COUNT(*) FILTER (WHERE is_foreground = 0) as bg_count,
                COUNT(*) FILTER (WHERE window_title IS NULL OR window_title = '') as title_empty,
                COUNT(*) FILTER (WHERE window_mode IS NULL) as mode_null,
                COUNT(*) as total
            FROM public.fact_process_context
        """)
        print(f"  duration_ms: {dc['dur_min']} ~ {dc['dur_max']}, 均值={dc['dur_avg']}ms")
        print(f"    负数: {dc['dur_neg']} {'❌' if dc['dur_neg'] > 0 else '✅'}")
        print(f"    >24h: {dc['dur_over_24h']} {'⚠️' if dc['dur_over_24h'] > 0 else '✅'}")
        print(f"  end_timestamp NULL: {dc['end_null']} (最后一条可以为NULL)")
        print(f"  end < start(时序违规): {dc['end_before_start']} {'❌' if dc['end_before_start'] > 0 else '✅'}")
        print(f"  is_foreground: 前台={dc['fg_count']} 后台={dc['bg_count']} 非法值={dc['fg_invalid']}")
        print(f"  空窗口标题: {dc['title_empty']}")
        print(f"  window_mode NULL: {dc['mode_null']}")
        
        samples = await conn.fetch("""
            SELECT c.timestamp, c.process_key, c.os_pid, c.is_foreground,
                   c.window_title, c.window_mode, c.end_timestamp, c.duration_ms,
                   r.process_name
            FROM public.fact_process_context c
            LEFT JOIN public.dim_process_registry r ON r.process_key = c.process_key
            ORDER BY c.timestamp DESC LIMIT 5
        """)
        print(f"\n  最新5条:")
        for s in samples:
            print(f"    {s['timestamp']} | {s['process_name']} pid={s['os_pid']} fg={s['is_foreground']} mode={s['window_mode']} | {str(s['window_title'] or '')[:40]} | dur={s['duration_ms']}ms end={s['end_timestamp']}")

    # ============================================================
    # 6. fact_process_lifecycle_events 审计
    # ============================================================
    print("\n\n[6] fact_process_lifecycle_events - 生命周期事件审计")
    print("-" * 60)
    lc_count = await conn.fetchval("SELECT COUNT(*) FROM public.fact_process_lifecycle_events")
    print(f"  总记录数: {lc_count}")
    
    if lc_count > 0:
        event_dist = await conn.fetch("""
            SELECT event_type, COUNT(*) as cnt
            FROM public.fact_process_lifecycle_events GROUP BY event_type
        """)
        for ed in event_dist:
            print(f"  {ed['event_type']}: {ed['cnt']}条")
        
        orphan = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_lifecycle_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dim_process_registry r WHERE r.process_key = e.process_key
            )
        """)
        print(f"  引用完整性: 孤儿={orphan} {'❌' if orphan > 0 else '✅'}")
        
        ec = await conn.fetchrow("""
            SELECT 
                MIN(process_lifetime) as life_min, MAX(process_lifetime) as life_max,
                COUNT(*) FILTER (WHERE process_lifetime < 0) as life_neg,
                COUNT(*) FILTER (WHERE process_lifetime IS NULL AND event_type = 'EXIT') as life_null_exit,
                COUNT(*) FILTER (WHERE process_lifetime IS NULL AND event_type = 'START') as life_null_start,
                COUNT(*) FILTER (WHERE exit_code IS NOT NULL AND event_type = 'START') as exit_code_on_start,
                COUNT(DISTINCT exit_code) FILTER (WHERE event_type = 'EXIT') as unique_exit_codes,
                COUNT(*) FILTER (WHERE event_type NOT IN ('START','EXIT')) as invalid_type
            FROM public.fact_process_lifecycle_events
        """)
        print(f"  寿命范围(EXIT): {ec['life_min']}s ~ {ec['life_max']}s")
        print(f"    负值: {ec['life_neg']} {'❌' if ec['life_neg'] and ec['life_neg'] > 0 else '✅'}")
        print(f"    EXIT时NULL: {ec['life_null_exit']} {'⚠️' if ec['life_null_exit'] and ec['life_null_exit'] > 0 else '✅'}")
        print(f"    START时NULL(预期): {ec['life_null_start']} ✅")
        print(f"    START有exit_code(异常): {ec['exit_code_on_start']} {'❌' if ec['exit_code_on_start'] and ec['exit_code_on_start'] > 0 else '✅'}")
        print(f"    唯一退出码数: {ec['unique_exit_codes']}")
        print(f"    非法event_type: {ec['invalid_type']} {'❌' if ec['invalid_type'] and ec['invalid_type'] > 0 else '✅'}")
        
        # 退出码分布
        exit_codes = await conn.fetch("""
            SELECT exit_code, COUNT(*) as cnt
            FROM public.fact_process_lifecycle_events
            WHERE event_type = 'EXIT'
            GROUP BY exit_code ORDER BY cnt DESC LIMIT 10
        """)
        print(f"\n  退出码分布 TOP10:")
        for ec2 in exit_codes:
            print(f"    {ec2['exit_code']}: {ec2['cnt']}次")
    
    # ============================================================
    # 7. 跨表一致性
    # ============================================================
    print("\n\n[7] 跨表一致性检查")
    print("-" * 60)
    
    # 时间范围
    tz_check = await conn.fetch("""
        SELECT 'fact_system_hardware' as tbl, MIN(timestamp)::text as oldest, MAX(timestamp)::text as newest, COUNT(*) as cnt
        FROM public.fact_system_hardware
        UNION ALL
        SELECT 'fact_process_activity', MIN(timestamp)::text, MAX(timestamp)::text, COUNT(*)
        FROM public.fact_process_activity
        UNION ALL
        SELECT 'fact_process_context', MIN(timestamp)::text, MAX(timestamp)::text, COUNT(*)
        FROM public.fact_process_context
        UNION ALL
        SELECT 'fact_process_lifecycle_events', MIN(event_timestamp)::text, MAX(event_timestamp)::text, COUNT(*)
        FROM public.fact_process_lifecycle_events
    """)
    for tc in tz_check:
        print(f"  {tc['tbl']}: {tc['oldest']} ~ {tc['newest']} ({tc['cnt']}行)")
    
    # hardware 和 activity 时间戳重合度
    if act_count > 0 and hw_count > 0:
        overlap = await conn.fetchrow("""
            SELECT 
                (SELECT COUNT(DISTINCT timestamp) FROM public.fact_system_hardware) as hw_ts_count,
                (SELECT COUNT(DISTINCT timestamp) FROM public.fact_process_activity) as act_ts_count,
                (SELECT COUNT(*) FROM (
                    SELECT DISTINCT timestamp FROM public.fact_system_hardware
                    INTERSECT
                    SELECT DISTINCT timestamp FROM public.fact_process_activity
                ) x) as overlap_count
        """)
        print(f"\n  时间戳重合度: hw={overlap['hw_ts_count']} act={overlap['act_ts_count']} 重合={overlap['overlap_count']}")
        if overlap['hw_ts_count'] > 0:
            pct = overlap['overlap_count'] / overlap['hw_ts_count'] * 100
            print(f"    重合率: {pct:.1f}% {'✅' if pct > 80 else '⚠️ 活动表与硬件表时间戳不对齐'}")
    
    # ============================================================
    # 8. 分区表健康
    # ============================================================
    print("\n\n[8] 分区表健康检查")
    print("-" * 60)
    partitions = await conn.fetch("""
        SELECT parent.relname as parent_table, child.relname as partition_name
        FROM pg_inherits
        JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
        JOIN pg_class child ON pg_inherits.inhrelid = child.oid
        WHERE parent.relname IN ('fact_system_hardware', 'fact_process_activity', 'fact_process_context')
        ORDER BY parent.relname, child.relname
    """)
    current_parent = None
    for p in partitions:
        if p['parent_table'] != current_parent:
            current_parent = p['parent_table']
            print(f"\n  {current_parent}:")
        print(f"    {p['partition_name']}")
    
    # ============================================================
    # 9. 数据库落库数据自动化正确性断言校验 (Db Audit Auto-Verification)
    # ============================================================
    print("\n\n[9] 数据正确性自动化校验断言 (Db Audit Assertions)")
    print("-" * 60)
    
    failures = []
    
    # 9a. dim_process_registry
    if reg_count == 0:
        failures.append("dim_process_registry 表为空，尚无任何进程注册")
    else:
        empty_paths = await conn.fetch("""
            SELECT process_key, process_name FROM public.dim_process_registry 
            WHERE executable_path = '' AND process_name != 'System'
              AND EXISTS (
                SELECT 1 FROM public.fact_process_activity WHERE process_key = dim_process_registry.process_key
                UNION ALL
                SELECT 1 FROM public.fact_process_context WHERE process_key = dim_process_registry.process_key
                UNION ALL
                SELECT 1 FROM public.fact_process_lifecycle_events WHERE process_key = dim_process_registry.process_key
              )
        """)
        if empty_paths:
            failures.append(f"dim_process_registry 存在被引用的非 System 进程具有空可执行路径: {[dict(r) for r in empty_paths]}")
            
        empty_names = await conn.fetch("""
            SELECT process_key FROM public.dim_process_registry 
            WHERE process_name = ''
              AND EXISTS (
                SELECT 1 FROM public.fact_process_activity WHERE process_key = dim_process_registry.process_key
                UNION ALL
                SELECT 1 FROM public.fact_process_context WHERE process_key = dim_process_registry.process_key
                UNION ALL
                SELECT 1 FROM public.fact_process_lifecycle_events WHERE process_key = dim_process_registry.process_key
              )
        """)
        if empty_names:
            failures.append(f"dim_process_registry 存在被引用的进程具有空进程名: {[dict(r) for r in empty_names]}")

    # 9b. fact_system_hardware
    if hw_count == 0:
        failures.append("fact_system_hardware 表为空")
    else:
        hw_oob = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_system_hardware
            WHERE cpu_total_usage < 0 OR cpu_total_usage > 100
               OR gpu_usage < 0 OR gpu_usage > 100
               OR system_ram_usage_pct < 0 OR system_ram_usage_pct > 100
               OR cpu_package_temp < 10 OR cpu_package_temp > 115
               OR gpu_core_temp < 10 OR gpu_core_temp > 115
               OR current_fps < 0
               OR frametime_ms < 0
               OR cpu_package_power < 0 OR cpu_package_power > 400
               OR gpu_board_power < 0 OR gpu_board_power > 600
        """)
        if hw_oob > 0:
            failures.append(f"fact_system_hardware 存在物理越界/异常数据: {hw_oob} 行")

    # 9c. fact_process_activity
    if act_count == 0:
        failures.append("fact_process_activity 表为空")
    else:
        orphan_act = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_activity a
            WHERE NOT EXISTS (SELECT 1 FROM public.dim_process_registry r WHERE r.process_key = a.process_key)
        """)
        if orphan_act > 0:
            failures.append(f"fact_process_activity 存在孤儿记录 (未在注册表注册): {orphan_act} 行")
            
        act_oob = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_activity
            WHERE proc_cpu_usage < 0 OR proc_cpu_usage > 100
               OR proc_gpu_usage < 0 OR proc_gpu_usage > 100
               OR proc_ram_mb < 0
               OR proc_vram_used_gb < 0 OR proc_vram_used_gb > 17.0
        """)
        if act_oob > 0:
            failures.append(f"fact_process_activity 存在越界指标: {act_oob} 行")

    # 9d. fact_process_context
    if ctx_count > 0:
        orphan_ctx = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_context c
            WHERE NOT EXISTS (SELECT 1 FROM public.dim_process_registry r WHERE r.process_key = c.process_key)
        """)
        if orphan_ctx > 0:
            failures.append(f"fact_process_context 存在孤儿记录: {orphan_ctx} 行")
            
        ctx_oob = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_context
            WHERE duration_ms < 0 
               OR (end_timestamp IS NOT NULL AND end_timestamp < timestamp)
               OR (end_timestamp IS NOT NULL AND ABS(EXTRACT(EPOCH FROM (end_timestamp - timestamp))*1000 - duration_ms) > 1000)
        """)
        if ctx_oob > 0:
            failures.append(f"fact_process_context 存在时域异常或时长计算偏差: {ctx_oob} 行")

    # 9e. fact_process_lifecycle_events
    if lc_count > 0:
        orphan_lc = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_lifecycle_events e
            WHERE NOT EXISTS (SELECT 1 FROM public.dim_process_registry r WHERE r.process_key = e.process_key)
        """)
        if orphan_lc > 0:
            failures.append(f"fact_process_lifecycle_events 存在孤儿记录: {orphan_lc} 行")
            
        exit_null_lifetimes = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_lifecycle_events
            WHERE event_type = 'EXIT' AND process_lifetime IS NULL
        """)
        if exit_null_lifetimes > 0:
            failures.append(f"fact_process_lifecycle_events 存在 EXIT 事件但 process_lifetime 为 NULL: {exit_null_lifetimes} 行")
            
        start_with_exit_code = await conn.fetchval("""
            SELECT COUNT(*) FROM public.fact_process_lifecycle_events
            WHERE event_type = 'START' AND exit_code IS NOT NULL
        """)
        if start_with_exit_code > 0:
            failures.append(f"fact_process_lifecycle_events 存在 START 事件但带有退出码: {start_with_exit_code} 行")

    if failures:
        print("\n❌ 校验断言失败！共发现以下错误:")
        for idx, f in enumerate(failures, 1):
            print(f"  {idx}. {f}")
        await conn.close()
        import sys
        sys.exit(1)
    else:
        print("\n✅ 校验断言全部通过！数据库数据 100% 正确！")
    
    await conn.close()
    print("\n" + "=" * 80)
    print("  审计完成")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(run())

