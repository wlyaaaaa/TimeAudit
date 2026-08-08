-- ============================================================================
--  TimeAudit 数据库初始化骨架  (schema.sql)
-- ----------------------------------------------------------------------------
--  作用：在一个【全新空白】的 time_audit 数据库里，一次性建好所有"父表"和索引。
--
--  为什么需要它：
--    遥测引擎(main.py)启动后只会自动创建"分区子表"(按周/按月)，但它假设父表
--    (fact_process_activity / fact_process_context / fact_system_hardware /
--     dim_process_registry / fact_process_lifecycle_events)已经存在。
--    所以全新装机时，必须先跑一次本文件把父表建好，引擎才有表可写。
--
--  怎么用（全新安装时执行一次）：
--    cmd /c "docker exec -i audit-postgres psql -U leyang -d time_audit < E:\Projects\Tools\TimeAudit\schema.sql"
--
--  注意：如果你是"换电脑/容灾恢复"，请直接用 pg_restore 导入备份(.dump)，
--        那里面已经包含完整结构和历史数据，【不需要】再跑本文件。
--
--  日期分区子表(如 _y2026w24)由引擎的 auto_warmup_partitions 自动创建，这里不用写。
-- ============================================================================

-- ============================ 0. 必备扩展 ============================
-- pg_trgm：供 app_usage_logs.window_title 的 GIN 三元组模糊检索索引(idx_logs_window_title_trgm)使用。
-- 全新装机若缺此扩展，该 GIN 索引无法创建、窗口标题模糊搜索会退化为全表顺序扫描。
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================ 1. 进程指纹维度表 ============================
-- 每个"独一无二的程序身份"存一行：进程名 + 路径 + 命令行 + 父进程 + 是否提权 + 签名状态。
-- 事实表用 process_key 这个整数外键指向它，避免每行都重复存长字符串。
CREATE TABLE IF NOT EXISTS public.dim_process_registry (
    process_key      serial PRIMARY KEY,
    process_name     varchar(100) NOT NULL,
    executable_path  text         NOT NULL,
    parent_process   varchar(100),
    command_line     text,
    is_elevated      smallint                 DEFAULT 0,   -- 1=管理员 0=普通 -1/-2=未知
    service_name     varchar(100)             DEFAULT NULL,
    signature_status smallint                 DEFAULT 0,   -- 1=有效签名 0=无签名 -1/-2=校验异常
    created_at       timestamp with time zone DEFAULT now()
);

-- 复合唯一索引：保证同一个"程序身份"只存一行，多个采集舱并发插入也不会重复/死锁。
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_process_bloodline
    ON public.dim_process_registry (
        process_name, md5(executable_path), COALESCE(parent_process, ''::character varying),
        md5(COALESCE(command_line, ''::text)), COALESCE(service_name, ''::character varying),
        is_elevated, signature_status
    );
CREATE INDEX IF NOT EXISTS idx_dim_registry_name_btree
    ON public.dim_process_registry (process_name);

-- ====================== 2. 进程活跃事实表（按周分区）======================
-- 每 3 秒一拍，记录每个"在动"的进程的 CPU/GPU/内存/显存/磁盘/网络等瞬时指标。
CREATE TABLE IF NOT EXISTS public.fact_process_activity (
    timestamp               timestamp with time zone NOT NULL,
    process_key             integer                  NOT NULL,
    os_pid                  integer                  NOT NULL,
    proc_cpu_usage          real,    -- 整机口径 0-100%（已按逻辑核数归一化）
    proc_gpu_usage          real,
    proc_ram_mb             integer,
    proc_vram_used_gb       real,
    proc_vram_shared_mb     integer,
    proc_disk_read_rate_mb  real,
    proc_disk_write_rate_mb real,
    proc_disk_iops          integer,
    proc_network_send_kb    real,
    proc_network_recv_kb    real,
    proc_active_connections smallint,
    proc_remote_ip_port     text,
    proc_cpu_affinity       integer,
    proc_thread_count       integer,
    is_not_responding       smallint,
    PRIMARY KEY (timestamp, process_key, os_pid)
) PARTITION BY RANGE ("timestamp");

-- 覆盖索引：Grafana 按时间范围聚合 CPU/GPU 时可"只读索引不回表"。
CREATE INDEX IF NOT EXISTS idx_process_activity_ts_key_covering
    ON public.fact_process_activity (timestamp, process_key, os_pid)
    INCLUDE (proc_cpu_usage, proc_gpu_usage);

-- 局部索引：专门针对无响应进程状态进行索引，避免在数百万条活跃度记录中扫描
CREATE INDEX IF NOT EXISTS idx_activity_is_not_responding
    ON public.fact_process_activity ("timestamp")
    WHERE is_not_responding = 1;

-- 复合索引：专门针对进程 key 和时间戳的查询进行优化，大幅提升按具体进程检索时间轴时的性能
CREATE INDEX IF NOT EXISTS idx_activity_proc_key_ts
    ON public.fact_process_activity (process_key, "timestamp");

-- 覆盖索引(含网络字段+局部条件)：安全审计「网络流量大户/远端IP」面板只读索引不回表，且只索引有网络活动的行。
CREATE INDEX IF NOT EXISTS idx_process_activity_network_covering
    ON public.fact_process_activity ("timestamp", process_key, os_pid)
    INCLUDE (proc_network_send_kb, proc_network_recv_kb, proc_active_connections, proc_remote_ip_port)
    WHERE (proc_network_send_kb > 0 OR proc_network_recv_kb > 0 OR proc_active_connections > 0);

-- 局部索引：仅索引有远端连接的行，加速「静默外联/高危端口」审计，避免全量明细扫描。
CREATE INDEX IF NOT EXISTS idx_activity_network_partial_opt
    ON public.fact_process_activity ("timestamp")
    WHERE (proc_remote_ip_port IS NOT NULL AND proc_remote_ip_port <> ''::text);

-- 局部索引：仅索引连到高危端口(22/3389/445/1433/3306/5432/5900/4444/5555)的行，专供横向移动/后门审计。
CREATE INDEX IF NOT EXISTS idx_activity_high_risk_ports_opt
    ON public.fact_process_activity ("timestamp")
    WHERE (proc_remote_ip_port IS NOT NULL AND proc_remote_ip_port <> ''::text AND (
        proc_remote_ip_port LIKE '%:22,%' OR proc_remote_ip_port LIKE '%:22' OR
        proc_remote_ip_port LIKE '%:3389,%' OR proc_remote_ip_port LIKE '%:3389' OR
        proc_remote_ip_port LIKE '%:445,%' OR proc_remote_ip_port LIKE '%:445' OR
        proc_remote_ip_port LIKE '%:1433,%' OR proc_remote_ip_port LIKE '%:1433' OR
        proc_remote_ip_port LIKE '%:3306,%' OR proc_remote_ip_port LIKE '%:3306' OR
        proc_remote_ip_port LIKE '%:5432,%' OR proc_remote_ip_port LIKE '%:5432' OR
        proc_remote_ip_port LIKE '%:5900,%' OR proc_remote_ip_port LIKE '%:5900' OR
        proc_remote_ip_port LIKE '%:4444,%' OR proc_remote_ip_port LIKE '%:4444' OR
        proc_remote_ip_port LIKE '%:5555,%' OR proc_remote_ip_port LIKE '%:5555'));

-- 覆盖索引：「整机活跃进程数/线程总数」面板按时间扫描时只读索引取 process_key/线程数，免回表。
CREATE INDEX IF NOT EXISTS idx_process_activity_ts_thread_covering_opt
    ON public.fact_process_activity ("timestamp")
    INCLUDE (process_key, proc_thread_count);


-- ====================== 3. 前台上下文事实表（按周分区）====================
-- 记录"哪个窗口在前台、标题是什么、聚焦了多久"。duration_ms 是该窗口连续聚焦的毫秒数。
CREATE TABLE IF NOT EXISTS public.fact_process_context (
    timestamp     timestamp with time zone NOT NULL,
    process_key   integer                  NOT NULL,
    os_pid        integer                  NOT NULL,
    is_foreground smallint                 NOT NULL,
    window_title  text,
    window_mode   smallint,                          -- 2=全屏/无边框 3=普通窗口
    end_timestamp timestamp with time zone,          -- 该窗口失去焦点的时刻；NULL=仍聚焦/未闭合
    duration_ms   bigint DEFAULT 0,
    PRIMARY KEY (timestamp, process_key, os_pid)
) PARTITION BY RANGE ("timestamp");

CREATE INDEX IF NOT EXISTS idx_process_context_grafana_covering
    ON public.fact_process_context (timestamp, is_foreground)
    INCLUDE (process_key, os_pid, window_title, duration_ms);

-- 局部覆盖索引：仅索引前台行(is_foreground=1)，供「前台聚焦/资源争抢」类面板按时间快速取会话区间，免回表。
CREATE INDEX IF NOT EXISTS idx_ctx_fg_ts_cov
    ON public.fact_process_context ("timestamp")
    INCLUDE (process_key, os_pid, end_timestamp)
    WHERE (is_foreground = 1);

-- ==================== 4. 进程生命周期事件表（不分区）====================
-- 进程的"出生(START)"与"死亡(EXIT)"离散事件，EXIT 带退出码和存活秒数。
CREATE TABLE IF NOT EXISTS public.fact_process_lifecycle_events (
    event_timestamp  timestamp with time zone NOT NULL,
    process_key      integer                  NOT NULL,
    os_pid           integer                  NOT NULL,
    event_type       varchar(10)              NOT NULL,   -- 'START' / 'EXIT'
    process_lifetime integer,                             -- 存活秒数(EXIT 时)
    exit_code        varchar(20) DEFAULT NULL,            -- 如 0x00000000 / 0xC0000005
    PRIMARY KEY (event_timestamp, process_key, event_type, os_pid)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_lookup
    ON public.fact_process_lifecycle_events (process_key, event_type, os_pid);

-- ===================== 5. 系统硬件性能事实表（按月分区）===================
-- 调度目标每 1 秒一拍的整机硬件画像；部分慢传感器复用最近有效缓存。
-- 字段包括 FPS/帧时间、CPU/GPU 温度功耗电压时钟、内存、磁盘延迟、网络 Ping。
CREATE TABLE IF NOT EXISTS public.fact_system_hardware (
    timestamp               timestamp with time zone NOT NULL PRIMARY KEY,
    current_fps             real,
    average_fps             real,
    one_percent_low_fps     real,
    frametime_ms            real,
    frametime_jitter        real,
    cpu_total_usage         real,
    cpu_vcore_voltage       real,
    cpu_clock_mhz           integer,
    cpu_package_temp        real,
    cpu_package_power       real,
    system_dpc_latency      real,
    system_context_switches integer,
    gpu_usage               real,
    gpu_core_voltage        real,
    gpu_core_clock          integer,
    gpu_mem_clock           integer,
    gpu_core_temp           real,
    gpu_hotspot_temp        real,
    gpu_board_power         real,
    gpu_throttling_reasons  smallint,
    pcie_bus_utilization    real,
    system_ram_usage_pct    real,
    system_commit_size_gb   real,
    system_hard_page_faults integer,
    disk_max_latency_ms     real,
    network_ping_ms         integer,
    is_packet_loss          smallint,
    network_jitter          real,
    cpu_ccd0_usage          real,
    cpu_ccd1_usage          real
) PARTITION BY RANGE ("timestamp");

-- ============== 6. 旧版 AHK→CSV 工时表（由容器内 ingest.py 自动建）==============
-- 这张表是 TimeAudit.ahk 写 buffer.csv、ingest.py 灌进来的"简版前台工时"。
-- ingest.py 启动时会自己 CREATE，这里一并写上以便手动建库时结构完整。
CREATE TABLE IF NOT EXISTS public.app_usage_logs (
    id               BIGSERIAL PRIMARY KEY,
    start_time       TIMESTAMP WITH TIME ZONE NOT NULL,
    duration_seconds INT NOT NULL,
    process_name     VARCHAR(100) NOT NULL,
    window_title     VARCHAR(500)
);
CREATE INDEX IF NOT EXISTS idx_logs_start_time ON public.app_usage_logs (start_time);
-- 进程名 B-tree：屏幕使用时间大盘按 process_name 分类/过滤工时时加速。
CREATE INDEX IF NOT EXISTS idx_logs_process ON public.app_usage_logs (process_name);
-- 窗口标题 GIN 三元组：支撑「标题排行/最近显示」面板对 window_title 的模糊(LIKE/ILIKE)检索(依赖 pg_trgm)。
CREATE INDEX IF NOT EXISTS idx_logs_window_title_trgm ON public.app_usage_logs USING gin (window_title gin_trgm_ops);

-- ============================ 7. 归属权 ============================
ALTER TABLE public.dim_process_registry          OWNER TO leyang;
ALTER TABLE public.fact_process_activity          OWNER TO leyang;
ALTER TABLE public.fact_process_context           OWNER TO leyang;
ALTER TABLE public.fact_process_lifecycle_events  OWNER TO leyang;
ALTER TABLE public.fact_system_hardware           OWNER TO leyang;
ALTER TABLE public.app_usage_logs                 OWNER TO leyang;

-- 完成。接下来启动引擎，它会自动创建当周/当月的分区子表并开始写入。
