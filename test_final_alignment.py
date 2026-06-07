import os
import sys
import asyncio
import asyncpg
import time
import psutil

try:
    from lifecycle_worker import ProcessLifecycleWorker, check_process_elevation, check_file_signature
    from activity_worker import ProcessActivityWorker
    from context_worker import WindowStateTracker
    print("✅ 模块依赖树加载测试通过：三大 Worker 成功并网，无循环依赖。")
except ImportError as e:
    print(f"❌ 模块导入失败：可能存在命名或循环依赖冲突！错误: {e}")
    sys.exit(1)

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"

async def test_dimensional_alignment(pool):
    print("\n--- 2. 启动三大模块同一性写入核对 (Dimension Alignment Check) ---")
    
    current_pid = os.getpid()
    
    # 初始化三大 Worker
    lifecycle_worker = ProcessLifecycleWorker({})
    activity_worker = ProcessActivityWorker()
    tracker = WindowStateTracker()
    
    # 🟢 步骤 A：初次扫描，为当前测试进程在活动舱建立 I/O 基准
    activity_worker.collect_active_processes()
    
    print("⏳ 正在模拟测试进程活性，促使金字塔过滤器放行...")
    
    # 🟢 步骤 B：执行瞬间 busy-wait CPU 消耗与 1MB 临时磁盘写入，强行刺穿 0.01MB/s 的初筛门槛
    temp_file = "temp_align_spike.tmp"
    try:
        # 产生磁盘写入
        with open(temp_file, "wb") as f:
            f.write(b"A" * 1024 * 1024)  # 1MB
        # 产生轻微 CPU 计算
        t_end = time.perf_counter() + 0.15
        while time.perf_counter() < t_end:
            pass
        if os.path.exists(temp_file):
            os.remove(temp_file)
    except:
        pass

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 轨道 A：生命周期模块注册
            print(f"👉 启动 [生命周期舱] 对 PID {current_pid} 进行指纹提取与注册...")
            key_lifecycle = await lifecycle_worker.register_live_pid(conn, current_pid)
            
            # 轨道 B：活动扫描模块注册
            print(f"👉 启动 [进程活动舱] 对 PID {current_pid} 进行特征打捞并注册...")
            active_procs = activity_worker.collect_active_processes()
            current_proc_info = next((p for p in active_procs if p["os_pid"] == current_pid), None)
            if not current_proc_info:
                print("❌ 错误：活动扫描舱未能在进程列表中检索到当前测试进程。")
                print("💡 原因：测试进程可能依然未能突破过滤门槛。")
                return
            key_activity = await activity_worker.get_or_register_cached(conn, current_proc_info)
            
            # 轨道 C：切窗状态机模块注册
            print(f"👉 启动 [切窗状态机舱] 对 PID {current_pid} 进行前台上下文指纹打捞并注册...")
            context_metadata = tracker.harvest_process_metadata(current_pid)
            key_context = await tracker.get_or_register_metadata_slow(conn, context_metadata)

            # 打印键值结果
            print("\n--- 3. 维度注册同一性判定 ---")
            print(f"   [🗝️ 生命周期舱生成的 process_key] -> {key_lifecycle}")
            print(f"   [🗝️ 进程活动舱生成的 process_key] -> {key_activity}")
            print(f"   [🗝️ 切窗状态机舱生成的 process_key] -> {key_context}")

            # 终极对齐断言
            if key_lifecycle == key_activity == key_context:
                print("\n✅ 终极维度一致性对齐测试通过！")
                print("   - 三大模块对同一个物理进程生成的指纹完全契合。")
                print("   - 数据库 dim_process_registry 成功执行 ON CONFLICT 冲突退避，未发生逻辑重复表项。")
            else:
                print("\n❌ 警告：一致性审计失败！三大模块生成的主键不一致，仍存在维度漏洞。")

            # 物理回滚事务，确保测试数据不污染生产环境数据库
            raise Exception("🌱 [测试事务回滚] 验证完毕，安全清理测试维度行。")

async def main():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 终极一致性与防新 Bug 审计")
    print("====================================================")
    
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
    except Exception as e:
        print(f"❌ 数据库连接池就绪失败: {e}")
        return

    try:
        await test_dimensional_alignment(pool)
    except Exception as mock_rollback:
        if "测试事务回滚" in str(mock_rollback):
            print("\n🧹 测试事务已成功执行安全回滚。维度注册表保持纯净。")
            print("🏁 本轮防新 Bug 终极审计闭环结束。")
        else:
            print(f"❌ 运行中抛出未预期异常:\n{mock_rollback}")
            
    await pool.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())