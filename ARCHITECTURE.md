# TimeAudit 数据与接口

本页供维护代码、查询数据和恢复面板时查阅；机器的实际任务、路径、端口占用和运行状态以 PCConfig 与现场结果为准。

## 采集到展示

| 路线 | 输入与写入 | 用途 |
| --- | --- | --- |
| 主采集器 | `main.py` 调度四个 worker，经 asyncpg 写入 PostgreSQL | 硬件、进程资源、前台窗口与进程生命周期 |
| 前台使用记录 | `TimeAudit.ahk → log/buffer.csv → ingest.py → app_usage_logs` | 保留空闲、睡眠、锁屏语义的使用区间；与主采集器独立 |
| 展示 | Grafana 查询同一数据库 | 看板可以聚合时间桶，不能把聚合粒度当传感器实际采样频率 |

主采集器的硬件、帧率和前台心跳以 1 秒调度，全进程资源扫描以 3 秒调度且同时只运行一次；慢扫描未结束时跳过下一次慢扫描，不堵快通道。较慢传感器可能复用最近有效缓存。

- `context_worker.py`：窗口身份、标题、模式和聚焦时长，写 `fact_process_context`。
- `activity_worker.py`：同一系统快照里的进程身份及 CPU、磁盘、I/O 等差分，写 `fact_process_activity` 和 `dim_process_registry`。进程网络流量按连接数占比近似分摊，不是精确流量计。
- `hardware_worker.py`：整机硬件与帧率，写 `fact_system_hardware`。NVML 无效数据不截成正常值；使用有效 LHM 回退，否则存 NULL，历史异常保留。
- `lifecycle_worker.py`：进程 START/EXIT、退出码和存活时长；在进程存在时持有必要句柄，写 `fact_process_lifecycle_events`。

## 表与关键含义

完整字段、索引与扩展由 [schema.sql](schema.sql) 定义。

| 表 | 记录 | 分区 |
| --- | --- | --- |
| `dim_process_registry` | 稳定 process_key、路径、父进程、命令行、签名和提权状态 | 不分区 |
| `fact_process_activity` | 进程资源采样 | 周 |
| `fact_process_context` | 前台会话及 duration_ms | 周 |
| `fact_system_hardware` | 整机硬件采样 | 月 |
| `fact_process_lifecycle_events` | 进程生命周期事件 | 不分区 |
| `app_usage_logs` | 可重叠的前台使用区间 | 不分区 |

`proc_cpu_usage` 按机器逻辑核数归一到 0–100%；进程 GPU 计数按 NVIDIA 独显身份匹配，不混核显或虚拟显示器。`window_mode` 中 2 为全屏/无边框、3 为普通窗口；`duration_ms` 是聚焦毫秒数，跨睡眠/锁屏区间截断。`signature_status` 与 `is_elevated` 的失败值表示未知，不据此断定恶意。

查询事实分区表须明确时间范围；使用区间须按查询窗裁剪、合并重叠，不能只按起点筛选后累加。原生内存黑匣子、个人活动读取与诊断摘要分别遵守各自已有接口文档，不拿主采集器心跳证明所有来源已落库。

## 运行依赖与恢复

- 运行环境由 `setup_runtime.ps1` 准备，入口使用项目 `.venv\Scripts\pythonw.exe`。保持引擎单实例，不从全局 Python 混入其他工具依赖。
- `telemetry_watchdog.ps1` 依据精确进程/容器身份和新鲜心跳有界恢复；先给唤醒留宽限，停止旧实例后才替换。任务定义归 PCConfig 的 `Install-TimeAuditRuntimeWatchdog.ps1`，日志在 `telemetry_watchdog.log`。
- LibreHardwareMonitor 是独立运行任务，硬件 worker 仅读其本机 HTTP 接口；PresentMon 回退由项目管理；RTSS 共享内存只读且不由本项目启停。RTSS 映射存在却无唯一新鲜帧源时是空闲，不能因此启动回退程序。
- `net_connections()` 在可重启的无状态子进程里运行，避免原生崩溃带走主采集器；`log/python_fatal.log` 保留不含业务正文的栈。启动时闭合未结束前台会话，按墙上时间预建分区。
- `RETENTION_DAYS` 默认 1200 天，保留至少三年的设计余量；0 表示关闭清理。实际删除由代码按数据时间和分区上界判断，不用旧容量估算承诺磁盘永远够用。

[容器定义](docker-compose.yml)管理 `audit-postgres`、`audit-ingester`、`audit-grafana`。默认 PostgreSQL 宿主端口 45432、库 time_audit，Grafana 宿主端口 43000；数据目录与版本由该文件维护，不在说明中另存机器快照。数据库口令从 `TIMEAUDIT_DB_PASSWORD` 注入，不写 Git、日志或命令行；宿主端口可用 `TIMEAUDIT_DB_HOST_PORT` 配置。

PostgreSQL 会话时区保持 `Asia/Shanghai`，否则按本地日界计算的功耗看板会偏移。面板与 provisioning 必须使用同一已登记数据源 UID，恢复不引入已退役数据源。性能参数、共享内存和固定镜像版本以 compose 为准，恢复步骤见 [快速部署](快速部署.md)。

跨项目读取入口见 [个人活动接口](PERSONAL_ACTIVITY_READER.md)、[机器异常摘要](PCCONFIG_ANOMALY_DIGEST_CONTRACT.md)和[诊断摘要](TIMEAUDIT_DIAGNOSTIC_SUMMARY_CONTRACT.md)；诊断操作见 [DIAGNOSTICS_OPERATIONS.md](DIAGNOSTICS_OPERATIONS.md)。
