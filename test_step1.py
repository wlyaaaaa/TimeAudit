import os
import sys
import asyncio
import asyncpg
import psutil

# 导入我们刚刚重构的 lifecycle_worker 模块中的核心方法
# 确保 test_step1.py 与 lifecycle_worker.py 处于同一目录下
try:
    from lifecycle_worker import check_process_elevation, check_file_signature, ProcessLifecycleWorker
except ImportError:
    print("❌ 错误：请确保 test_step1.py 与修改后的 lifecycle_worker.py 放在同一目录下。")
    sys.exit(1)

DB_DSN = "postgresql://leyang:SecurePassword123@localhost:55432/time_audit"

async def test_database_integration(pool):
    """验证带有 is_elevated 和 signature_status 的 dim_process_registry 写入与冲突处理"""
    print("\n--- 3. 数据库唯一索引与冲突测试 ---")
    
    # 模拟一个测试用进程特征数据
    test_metadata = {
        "name": "test_process.exe",
        "exe": "C:\\Windows\\System32\\test_process.exe",
        "parent_name": "cmd.exe",
        "cmdline": "--test-param=123",
        "service_name": None,
        "is_elevated": 1,        # 模拟提权
        "signature_status": 1   # 模拟受信任签名
    }
    
    # 实例化一个临时的 lifecycle_worker
    dummy_map = {}
    worker = ProcessLifecycleWorker(dummy_map)
    
    try:
        async with pool.acquire() as conn:
            # 第一次写入：应当成功插入并返回 process_key
            key1 = await worker._resolve_metadata_to_db(conn, 9999, test_metadata)
            print(f"👉 首次写入成功，生成 process_key: {key1}")
            
            # 第二次写入（完全相同的数据）：应当触发 ON CONFLICT 并返回相同的 process_key
            key2 = await worker._resolve_metadata_to_db(conn, 9999, test_metadata)
            print(f"👉 二次写入（触发冲突退避），返回 process_key: {key2}")
            
            if key1 == key2:
                print("✅ 数据库冲突退避测试通过！唯一索引 (idx_unique_process_bloodline) 运行正常。")
            else:
                print("❌ 数据库测试异常：两次返回的 key 不一致。")
                
            # 清理测试数据以保持数仓纯净
            await conn.execute("DELETE FROM public.dim_process_registry WHERE process_name = $1;", "test_process.exe")
            print("🧹 测试垃圾数据已成功清理。")
            
    except Exception as e:
        print(f"❌ 数据库写入测试失败。错误信息: {e}")

def run_win32_local_tests():
    """验证 Win32 ctypes 的稳定性和准确性"""
    print("--- 1. 进程特权提取测试 (check_process_elevation) ---")
    current_pid = os.getpid()
    elevation_status = check_process_elevation(current_pid)
    print(f"👉 当前 Python 进程 (PID: {current_pid}) 提权状态: {elevation_status} (1=管理员, 0=普通用户, -1或-2=异常)")
    
    # 获取系统核心进程（通常为受保护进程或System特权）进行鲁棒性测试
    system_tested = False
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'].lower() in ('lsass.exe', 'csrss.exe', 'svchost.exe'):
                sys_pid = proc.info['pid']
                sys_elevation = check_process_elevation(sys_pid)
                print(f"👉 系统保护进程 {proc.info['name']} (PID: {sys_pid}) 提权状态: {sys_elevation} (-1代表受保护隔离，属于正常表现)")
                system_tested = True
                break
        except Exception:
            continue
    if not system_tested:
        print("⚠️ 未找到合适的系统保护进程进行鲁棒性对比。")

    print("\n--- 2. 数字签名 Authenticode 验证测试 (check_file_signature) ---")
    # 测试受信任的微软官方文件
    signed_path = "C:\\Windows\\System32\\cmd.exe"
    sig_signed = check_file_signature(signed_path)
    print(f"👉 已签名文件测试 ({signed_path}): {sig_signed} (期望值: 1)")
    
    # 测试不存在的文件
    fake_path = "C:\\Windows\\System32\\this_file_does_not_exist_12345.exe"
    sig_fake = check_file_signature(fake_path)
    print(f"👉 不存在的文件测试 ({fake_path}): {sig_fake} (期望值: 0)")
    
    # 测试本地 Python 解释器（部分环境下的 python.exe 可能未带微软数字签名）
    python_exe = sys.executable
    sig_py = check_file_signature(python_exe)
    print(f"👉 本地 Python 解释器测试 ({python_exe}): {sig_py} (1=有签名, 0=无签名, -1=签名失效)")

async def main():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 阶段 1 隔离测试")
    print("====================================================")
    
    # 1 & 2. 运行本地 Win32 API 测试
    run_win32_local_tests()
    
    # 3. 运行数据库对接测试
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
        await test_database_integration(pool)
        await pool.close()
    except Exception as e:
        print(f"\n❌ 无法连接到 PostgreSQL 数据库以完成第3项测试，请检查容器状态。错误: {e}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())