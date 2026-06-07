import os
import sys
import asyncio
import asyncpg
import time
import datetime

try:
    from activity_worker import ProcessActivityWorker
except ImportError:
    print("❌ 错误：请确保 test_step4.py 与修改后的 activity_worker.py 放在同一目录下。")
    sys.exit(1)

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"

async def test_database_activity_insert(pool, active_procs):
    print("\n--- 3. 数据库 18 列进程活动全维写入测试 ---")
    worker = ProcessActivityWorker()
    try:
        # 执行批量插入测试
        await worker.write_batch_to_db(pool, active_procs)
        print("✅ 数据库 18 字段全进程活动写入成功！数据入库格式无损。")
    except Exception as e:
        print(f"❌ 数据库写入测试失败。错误详情:\n{e}")

def run_activity_profiler():
    print("\n--- 1. 启动微观进程活动探针捕获 ---")
    worker = ProcessActivityWorker()
    
    # 第一次捕获建立 I/O 基准
    worker.collect_active_processes()
    print("⏳ 正在为微观 I/O 差分引擎建立初次采样基准（等待 2 秒）...")
    time.sleep(2.0)
    
    # 第二次捕获提取真实差分数据
    procs = worker.collect_active_processes()
    
    print("\n--- 2. 活动进程特征样本 (1Hz 微观穿透) ---")
    # 寻找负载最高的 5 个进程进行分析
    procs_sorted = sorted(procs, key=lambda x: (x["cpu"] + x["gpu"] + x["net_send_kb"]), reverse=True)
    
    count = 0
    for p in procs_sorted[:5]:
        print(f"👉 进程: {p['name']} [PID: {p['os_pid']}]")
        print(f"   [CPU 负载] {p['cpu']:.1f}% | [GPU 算力] {p['gpu']:.1f}%")
        print(f"   [物理内存] {p['ram_mb']} MB | [独占显存] {p['vram_gb']:.3f} GB | [共享显存] {p['vram_shared_mb']} MB")
        print(f"   [磁盘读速] {p['r_rate']:.3f} MB/s | [磁盘写速] {p['w_rate']:.3f} MB/s | [IOPS] {p['iops']} 次/秒")
        print(f"   [网络发送] {p['net_send_kb']:.2f} KB/s | [网络接收] {p['net_recv_kb']:.2f} KB/s")
        print(f"   [连接套接字] {p['net_conn_count']} 个 | [亲和掩码] {p['affinity']}")
        print("-" * 50)
        count += 1
        
    print(f"ℹ️ 这一秒内共监测到 {len(procs)} 个活跃进程样本。")
    return procs

async def main():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 阶段 4 隔离测试")
    print("====================================================")
    
    # 1. 运行系统物理探针检测
    active_procs = run_activity_profiler()
    
    # 2. 运行数据库级闭环写入测试
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
        await test_database_activity_insert(pool, active_procs)
        await pool.close()
    except Exception as e:
        print(f"\n❌ 无法连接到 PostgreSQL 数据库以完成测试。错误: {e}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())