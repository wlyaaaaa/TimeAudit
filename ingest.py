print("==================================================")
print("🔥 警告：Python 解释器已成功捕获源码，正在全力初始化...")
print("==================================================")

import os
import time
import csv
import psycopg  

# === 🔒 容器网络连接配置 ===
DB_HOST = "audit-db"
DB_USER = "leyang"
DB_PASS = "SecurePassword123"
DB_NAME = "time_audit"
CSV_PATH = "/log/buffer.csv"

def connect_db():
    """死守数据库连接，直到容器完全初始化完毕"""
    while True:
        try:
            return psycopg.connect(
                host=DB_HOST, 
                user=DB_USER, 
                password=DB_PASS, 
                dbname=DB_NAME
            )
        except Exception:
            time.sleep(2)

def init_db():
    """初始化数据库表结构与高频索引"""
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_usage_logs (
                    id BIGSERIAL PRIMARY KEY,
                    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    duration_seconds INT NOT NULL,
                    process_name VARCHAR(100) NOT NULL,
                    window_title VARCHAR(500)
                );
                CREATE INDEX IF NOT EXISTS idx_logs_start_time ON app_usage_logs (start_time);
            """)
            conn.commit()

def sync_pipeline():
    """增量无损同步核心管道"""
    if not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0:
        return
    
    processing_tmp_path = CSV_PATH + ".processing"
    
    try:
        if os.path.exists(processing_tmp_path):
            os.remove(processing_tmp_path)
        os.rename(CSV_PATH, processing_tmp_path)
    except Exception:
        return 
        
    if not os.path.exists(processing_tmp_path):
        return
        
    rows_to_insert = []
    with open(processing_tmp_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 4: 
                continue
            try:
                rows_to_insert.append((row[0], int(row[1]), row[2], row[3]))
            except ValueError:
                continue
                
    if rows_to_insert:
        # ⚡ 核心修正：利用 with 上下文管理器包盘，任何 SQL 崩溃都会自动回滚并 close 释放连接，绝不泄漏
        try:
            with connect_db() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO app_usage_logs (start_time, duration_seconds, process_name, window_title) VALUES (%s, %s, %s, %s);",
                        rows_to_insert
                    )
                conn.commit()
            print(f"成功无感同步 {len(rows_to_insert)} 条工时区间数据至本地 PostgreSQL 数据库。")
            
            # ⚡ 核心修正：只有成功入库的数据，才配被物理删除
            if os.path.exists(processing_tmp_path):
                os.remove(processing_tmp_path)
        except Exception as db_err:
            # 如果入库失败，还原现场，把文件名改回去，留给下个周期重试，死守数据不丢失！
            print(f"📡 数仓写入发生抖动(已启动保护机制还原现场): {db_err}")
            if os.path.exists(processing_tmp_path) and not os.path.exists(CSV_PATH):
                os.rename(processing_tmp_path, CSV_PATH)

if __name__ == "__main__":
    init_db()
    print("🚀 数据同步管道常驻线程已就位，开启 10 秒周期性无锁扫描...")
    while True:
        try:
            sync_pipeline()
        except Exception as e:
            print(f"管道未知异常(已捕获): {e}")
        time.sleep(10)