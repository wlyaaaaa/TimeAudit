# -*- coding: utf-8 -*-
import json
import os
import asyncio
import sys
import asyncpg

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DASHBOARD_PATH = r"E:\TimeAudit\grafana_dashboards\addrd7x__🐀 资源大户与后台内鬼.json"

DB_DSN = "postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit"

async def test_dashboard_json():
    print("[Test] 1. 验证仪表盘 JSON 配置...")
    assert os.path.exists(DASHBOARD_PATH), f"找不到文件: {DASHBOARD_PATH}"
    
    with open(DASHBOARD_PATH, encoding="utf-8") as f:
        data = json.load(f)
    
    panel = None
    for p in data.get("panels", []):
        if "无响应卡死进程时间线" in p.get("title", ""):
            panel = p
            break
            
    assert panel is not None, "未能在 JSON 中找到卡死时间线面板！"
    
    target = panel.get("targets", [{}])[0]
    format_type = target.get("format")
    raw_sql = target.get("rawSql")
    
    print(f"  - 面板格式: {format_type}")
    assert format_type == "time_series", f"格式错误：期望 'time_series', 实际 '{format_type}'！若为 'table' 会在前端显示为 'metric' 和 'value' 的列名横条。"
    assert "UNION ALL" in raw_sql, "SQL 中缺少 UNION ALL 哑行兜底逻辑！"
    assert "🟢 无卡死事件" in raw_sql, "SQL 中缺少 '🟢 无卡死事件' 的 fallback 文字！"
    print("  ✓ 仪表盘 JSON 配置校验通过！")
    return raw_sql

async def test_sql_execution(raw_sql):
    print("[Test] 2. 验证 SQL 执行与哑行兜底逻辑...")
    conn = await asyncpg.connect(DB_DSN)
    try:
        # 将 Grafana 宏替换为标准的 PostgreSQL 表达式进行本地测试
        test_sql = raw_sql.replace("$__timeFrom()::timestamptz", "(now() - interval '12 hours')")
        test_sql = test_sql.replace("$__timeTo()::timestamptz", "now()")
        
        # 执行查询
        rows = await conn.fetch(test_sql)
        print(f"  - 过去 12 小时返回行数: {len(rows)}")
        assert len(rows) > 0, "查询返回了 0 行！这将导致 Grafana 报错 '缺少时间字段'。"
        
        # 检查列名
        fields = list(rows[0].keys())
        print(f"  - 返回列字段: {fields}")
        assert fields == ["time", "metric", "value"], f"返回字段不符合 time_series 要求：期望 ['time', 'metric', 'value'], 实际 {fields}"
        
        # 检查是否包含哑行
        has_fallback = False
        has_real_stuck = False
        for r in rows:
            metric_val = r["metric"]
            value_val = r["value"]
            if "无卡死事件" in metric_val:
                has_fallback = True
                assert value_val == 0, f"哑行值非 0: {value_val}"
            else:
                has_real_stuck = True
                
        if has_fallback:
            print("  ✓ 成功触发哑行逻辑，返回 '🟢 无卡死事件' 正常横条！")
            assert not has_real_stuck, "哑行和真实卡死不应同时返回！"
        else:
            print("  ✓ 存在真实的卡死进程，返回了卡死事件列表！")
            
        print("  ✓ 数据库 SQL 执行校验通过！")
    finally:
        await conn.close()

async def main():
    try:
        raw_sql = await test_dashboard_json()
        await test_sql_execution(raw_sql)
        print("\n🎉 ALL TESTS PASSED! 软件测试全部成功！")
        return 0
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        return 2

if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
