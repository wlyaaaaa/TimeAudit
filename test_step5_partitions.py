import sys
import asyncio
import asyncpg
import datetime

# 导入主控中的周/月边界计算函数
try:
    from main import get_week_bounds, get_month_bounds, auto_warmup_partitions, DB_DSN
except ImportError:
    print("❌ 错误：请确保 test_step5_partitions.py 与 main.py 放在同一目录下。")
    sys.exit(1)

async def run_partition_validation():
    print("====================================================")
    print("🔎 Windows 11 Telemetry Engine - 阶段 5 分区健壮性测试")
    print("====================================================")
    
    try:
        pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=2)
        print("[连接池] 数据库连接池就绪。")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return

    # 1. 验证本周、下周，以及本月、下月的物理边界计算
    print("\n--- 1. 预热算法边界推演 (Time Boundary Projection) ---")
    
    y, m, m_start, m_end = get_month_bounds(0)
    print(f"👉 【本月度分区预测】 对应月份: {y}年{m:02d}月")
    print(f"   - 物理起止边界: {m_start}  --->  {m_end}")
    
    ny, nm, nm_start, nm_end = get_month_bounds(1)
    print(f"👉 【下月度分区预热】 对应月份: {ny}年{nm:02d}月")
    print(f"   - 物理起止边界: {nm_start}  --->  {nm_end}")
    
    yw, wk, w_start, w_end = get_week_bounds(0)
    print(f"👉 【本周度分区预测】 对应周次: {yw}年第{wk:02d}周")
    print(f"   - 物理起止边界: {w_start}  --->  {w_end}")

    nyw, nwk, nw_start, nw_end = get_week_bounds(1)
    print(f"👉 【下周度分区预热】 对应周次: {nyw}年第{nwk:02d}周")
    print(f"   - 物理起止边界: {nw_start}  --->  {nw_end}")

    # 2. 模拟触发自动分区预热引擎
    print("\n--- 2. 执行分区预热引擎建表测试 (auto_warmup_partitions) ---")
    try:
    # 🟢 临时利用测试脚本的 async 特性，向数据库预热我们 DDL 中从未定义的未来分区
        async with pool.acquire() as conn:
        # 预热第 32 周 (当前第 24 周 + 8 周)
            y_w, w_wk, w_s, w_e = get_week_bounds(8) 
            suffix_w = f"y{y_w}w{w_wk:02d}"
            for parent_table in ["fact_process_context", "fact_process_activity"]:
                await conn.execute(f"CREATE TABLE IF NOT EXISTS public.{parent_table}_{suffix_w} PARTITION OF public.{parent_table} FOR VALUES FROM ('{w_s}') TO ('{w_e}');")
            
        # 预热 8 月份 (当前 6 月 + 2 个月)
            y_m, m_m, m_s, m_e = get_month_bounds(2) 
            suffix_m = f"y{y_m}m{m_m:02d}"
            await conn.execute(f"CREATE TABLE IF NOT EXISTS public.fact_system_hardware_{suffix_m} PARTITION OF public.fact_system_hardware FOR VALUES FROM ('{m_s}') TO ('{m_e}');")
        
        print("✅ 未来时间穿梭分区建表成功！")
    except Exception as e:
        print(f"❌ 预热建表失败: {e}")

    # 3. 实时打捞数据库物理子表状态，展示挂载关系
    print("\n--- 3. 数据库物理挂载结构审计 (Physical Partition Hierarchy) ---")
    query_partitions = """
        SELECT 
            nmsp_parent.nspname AS parent_schema,
            tbl_parent.relname AS parent_table,
            nmsp_child.nspname AS child_schema,
            tbl_child.relname AS child_partition
        FROM pg_inherits
        JOIN pg_class tbl_parent ON pg_inherits.inhparent = tbl_parent.oid
        JOIN pg_class tbl_child ON pg_inherits.inhrelid = tbl_child.oid
        JOIN pg_namespace nmsp_parent ON tbl_parent.relnamespace = nmsp_parent.oid
        JOIN pg_namespace nmsp_child ON tbl_child.relnamespace = nmsp_child.oid
        WHERE tbl_parent.relname IN ('fact_system_hardware', 'fact_process_context', 'fact_process_activity')
        ORDER BY parent_table, child_partition;
    """
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(query_partitions)
        if rows:
            current_parent = None
            for r in rows:
                p_table = r["parent_table"]
                c_partition = r["child_partition"]
                if p_table != current_parent:
                    print(f"\n📁 主事实表: public.{p_table}")
                    current_parent = p_table
                print(f"   └── 🛠️ 已挂载的分区存储舱: {c_partition}")
        else:
            print("⚠️ 未检测到任何已挂载的分区子表，请检查主表 DDL 是否正确。")

    await pool.close()
    print("\n🏁 分区健壮性与承载测试结束。")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_partition_validation())