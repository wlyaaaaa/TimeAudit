# TimeAudit 架构与数据流

## 2. 整体怎么转？（数据从哪来，到哪去）

```
┌─────────────────────────── 你的 Windows 11 主机 ───────────────────────────┐
│                                                                            │
│  采集端（宿主机，管理员权限运行）                                          │
│                                                                            │
│   main.py  ──┬─ context_worker   前台窗口：哪个窗口在用、标题、聚焦多久      │
│  (总调度)    ├─ activity_worker   每个活跃进程：CPU/GPU/内存/显存/磁盘/网络  │
│              ├─ hardware_worker   整机硬件：FPS/温度/功耗/电压/时钟/Ping     │
│              └─ lifecycle_worker  进程出生(START)/死亡(EXIT)事件 + 退出码    │
│                     │         ▲                                            │
│                     │         │ 读硬件真值                                  │
│                     │     LibreHardwareMonitor.exe (CPU/GPU 电压温度, HTTP :18085)
│                     │     RTSS shared memory / PresentMon fallback (FPS)    │
│                     ▼                                                       │
│             [asyncpg 异步批量写入]                                          │
│                     │                                                       │
│  另一条独立旧管线：   │                                                       │
│   TimeAudit.ahk ─→ log/buffer.csv ─→ (容器内) ingest.py ─→ app_usage_logs   │
│                     │                                                       │
└─────────────────────┼──────────────────────────────────────────────────────┘
                      ▼
        ┌──────────── Docker 容器群 (docker-compose) ────────────┐
        │  audit-postgres   PostgreSQL 15   端口 45432  ← 数据仓库 │
        │  audit-ingester   跑 ingest.py    搬 CSV→app_usage_logs  │
        │  audit-grafana    Grafana 大盘    端口 43000  ← 看数据   │
        └────────────────────────────────────────────────────────┘
                      ▲
                      │ 浏览器打开 http://localhost:43000
                  （你在这里看图）
```

一句话总结：**采集端（Python）用 1 秒硬件/FPS 快车道 + 3 秒进程慢车道采集 → 直接写进 Docker 里的 PostgreSQL → 你用浏览器开 Grafana 看图。**

---

## 3. ⚠️ 一个容易搞混的点：这里其实有"两条"数据管线

项目历史上长出了两套前台记录，**它们同时在跑、互不干扰**，新手最容易在这里懵：

| | 管线 A：旧版「简版工时」 | 管线 B：主引擎「全量遥测」 |
| :--- | :--- | :--- |
| 谁采集 | `TimeAudit.ahk`（AutoHotkey 脚本） | `main.py` + 4 个 worker（Python） |
| 采什么 | 只采"前台哪个窗口、用了多久"，自带空闲/睡眠/锁屏判定 | 前台窗口 + 全部进程指标 + 整机硬件 + 进程生死 |
| 怎么落库 | 先写 `log/buffer.csv`，再由容器里的 `ingest.py` 每 10 秒搬进数据库 | Python 直接异步写数据库 |
| 进哪张表 | `app_usage_logs`（一张简单表） | `fact_*` / `dim_*` 一套分区事实表 |

**你真正要看的、信息量最大的，是管线 B（`fact_*` 表）**。管线 A 是更早的轻量版本，留着兜底/对照。
本 README 后面讲的"采集舱""数据表"主要指**管线 B**。

---

## 4. 采集端：4 个 worker 各管一摊

主程序 `main.py` 是"总指挥"：它建数据库连接池、把 4 个采集 worker 拉起来，然后进入**双节拍调度**：
整机硬件/FPS/前台心跳每 1 秒一拍，昂贵的全进程扫描单飞且每 3 秒一拍。下面逐个说人话。

### `main.py` — 总调度 + 守护外壳
- 每 1 秒驱动硬件、FPS 与前台心跳；每 3 秒驱动全进程资源扫描。慢车道单飞运行，尚未完成时只跳过后续慢车道档位，不排队也不拖住快车道；健康租约超期后停止刷新总心跳，交给外部看门狗恢复。
- “每秒落一行”表示采样与写库节拍，不表示每个底层传感器都具有原生 1 Hz 新值；LHM/WMI 等较慢来源会安全复用其最近一次有效缓存。Grafana 为控制长时间范围查询成本，仍可把原始 1 秒数据聚合成更大的时间桶。
- **单例锁**：保证全机只有一个引擎在跑；发现已有实例时，新实例退出。只有外部看门狗确认旧实例停止或心跳陈旧且恢复宽限已过，才先停后启替换。
- **原生崩溃隔离**：2026-06-22 起观测到 psutil `_psutil_windows.pyd` 的 `0xc0000005`。主引擎现已避开上游已确认的 Windows `cpu_stats()` use-after-free，并把 `net_connections()` 放进可独立重启的无状态子进程；子进程崩溃不再带死主引擎。`log/python_fatal.log` 额外保留 payload-free Python fatal stack。
- **外部进程守护**（补 native 崩溃和“进程活着但采集卡死”盲点）：`telemetry_watchdog.ps1` + 计划任务 `TimeAudit_Watchdog`（每 1 分钟 + 登录触发、提权、任务失败最多重试 3 次）**独立于引擎**运行。`main.py`、`TimeAudit.ahk` 和 `audit-ingester` 都写无 payload heartbeat；watchdog 先给睡眠恢复留出宽限，再按精确进程/容器身份分别恢复。进程只是 Running 但消息循环或入库循环已经卡死，也会因 heartbeat 陈旧被识别。任务定义由 PCConfig 的 `Install-TimeAuditRuntimeWatchdog.ps1` 恢复，日志见 `telemetry_watchdog.log`。

- **隔离 Python 环境**：运行 `pwsh -File .\setup_runtime.ps1` 创建 `.venv`。启动脚本和 watchdog 只使用 `.venv\Scripts\pythonw.exe`，不再继承全局 Python 中 Open Interpreter 等工具的依赖冲突。
- **睡眠/唤醒处理**（重点，见第 7 节）：用"墙上时间"判断系统是否刚从睡眠/休眠醒来，醒来后把跨睡眠的脏数据截断掉。
- **冷启动清理**：每次启动先把上次"关机时没来得及收尾"的前台会话补上结束时间（否则会留下永远不结束的"幽灵行"）。
- **分区预热**：每 12 小时（按墙上时间）提前把"下一周/下一月"的数据库分区建好，免得到了周一零点没表可写而丢数据。
- **日志治理**：`telemetry.log` 超过 50MB 自动清空截断。

### `context_worker.py` — 前台上下文舱
- 用 Win32 API 高频问："现在最前面的窗口是谁、标题是什么、是全屏还是窗口"。
- 窗口一换，就把上一个窗口的会话"结算"：写下它聚焦了多少毫秒（`duration_ms`）。
- 写进 `fact_process_context`。

### `activity_worker.py` — 活跃进程舱
- 用 NTDLL 一次性拿到全系统进程快照（比逐个 psutil 快得多），算出每个进程这一拍的 CPU、磁盘读写、IOPS（都是"速率"，靠两拍差分算）。
- GPU 显存/占用走 Windows 图形内核的 PDH 计数器（和任务管理器同源），并**只认 NVIDIA 独显**（按厂商 ID 锁 LUID，隔离核显和虚拟显示器，见第 7 节）。
- 网络流量按"每进程连接数占比"近似分摊（这是个已知的粗略估算，不是精确值）。
- 写进 `fact_process_activity`，并维护进程身份维度表 `dim_process_registry`。

### `hardware_worker.py` — 整机硬件舱
- NVML 读 GPU：利用率、温度、功耗、显存时钟、PCIe、降频原因。核心读数越界或非有限时按句柄失败处理，重初始化 NVML；当前采样使用既有 LHM 真实利用率、温度和功率回退，没有有效来源则写 NULL，不把坏读数截断成正常值。历史无效样本保留，诊断摘要仍标记 `telemetry_out_of_bounds`。
- PDH 读 CPU：频率、ACPI 温度、硬缺页等。
- **LibreHardwareMonitor**（外部 exe）通过 HTTP `http://127.0.0.1:18085/data.json` 读 NVML/PDH 给不出来的真值：CPU 核心电压(Vcore)、CPU 封装温度(Tctl/Tdie)、GPU 核心电压、GPU 热点温度。`18085` 避开了启动前会阻断绑定的 Windows TCP 宽排除段；服务在线后出现同端口的单项活动保留是正常现象。
- **RTSS 官方共享内存**按精确前台 PID、RTSS 最近前台和唯一新鲜帧源读取 FPS / 帧时间 / 1% Low；映射可用但没有唯一帧源时视为桌面空闲，只有 RTSS 映射不可用时才回退项目内的 **PresentMonConsole**。
- 自己测 DPC 延迟、Ping、丢包、抖动。
- PresentMon fallback 有项目内的单 owner 看门狗；RTSS 共享内存只读且不由 TimeAudit 启停。LibreHardwareMonitor 则由独立的 `LibreHardwareMonitor` 计划任务作为唯一运行时 owner，并由 `telemetry_watchdog.ps1` 按端点健康状态恢复。Python 硬件舱只读 `18085`，不会再自行拉起/结束 LHM，避免两个 LHM 实例同时访问 NVML。
- 写进 `fact_system_hardware`。

### `lifecycle_worker.py` — 进程生死舱
- 每秒对全系统进程做一次"差分"：这一秒多出来的就是"出生(START)"，少掉的就是"死亡(EXIT)"。
- 进程出生时就提前抓住它的内核句柄，这样它死的时候才能读到**真实退出码**（如 `0xC0000005`）和存活时长。
- 顺带做"数字签名校验"（判断是不是微软官方签名）和"是否提权"。
- 写进 `fact_process_lifecycle_events`。

---

## 5. 数据库里有哪些表？（每张表存什么，大白话）

数据库名 `time_audit`，跑在 Docker 容器 `audit-postgres` 里，宿主机端口 **45432**。

| 表名 | 类型 | 存什么 | 怎么分区 |
| :--- | :--- | :--- | :--- |
| `dim_process_registry` | 维度表 | 每个"独一无二的程序身份"一行：进程名+路径+命令行+父进程+是否提权+签名状态。事实表用整数 `process_key` 指向它，省空间。 | 不分区 |
| `fact_process_activity` | 事实表 | 每 3 秒 × 每个活跃进程一行：CPU/GPU/内存/显存/磁盘/网络/线程数等。 | 按**周** |
| `fact_process_context` | 事实表 | 前台窗口会话：哪个窗口、标题、`duration_ms`（聚焦多久）。 | 按**周** |
| `fact_system_hardware` | 事实表 | 每 1 秒一行整机硬件画像：FPS、CPU/GPU 温度功耗电压时钟、内存、磁盘延迟、Ping。 | 按**月** |
| `fact_process_lifecycle_events` | 事实表 | 进程 START/EXIT 离散事件 + 退出码 + 存活秒数。 | 不分区 |
| `app_usage_logs` | 旧版表 | 管线 A（AHK→CSV）的简版前台工时。 | 不分区 |

**几个关键字段的含义**（看图时会用到）：

- `proc_cpu_usage`：**整机口径 0–100%**（已按 32 个逻辑核归一化）。不是单核百分比，所以一个多线程进程也不会超过 100%。
- `signature_status`：`1`=有效数字签名（多半是正经软件），`0`=没签名（可疑），`-1/-2`=校验出错。
- `is_elevated`：`1`=管理员权限运行，`0`=普通，`-1/-2`=查不到。
- `window_mode`：`2`=全屏/无边框，`3`=普通窗口。
- `gpu_throttling_reasons`：GPU 降频原因的二进制位（撞功耗墙/温度墙等）。
- `duration_ms`：前台窗口连续聚焦的毫秒数。**注意**：跨睡眠/锁屏的会话会被引擎截断，不会把睡觉时间算成"在用"。

> 完整建表语句见 **[schema.sql](schema.sql)**（全新装机时用它建表；含 `pg_trgm` 扩展与全部覆盖/局部索引）。

**数据保留 / 三年可行性**：`fact_process_activity` 约 2GB/周，跑满三年约 **330GB**，对 E 盘（数 TB）完全无压力，**三年内无需任何清理**。`main.py` 里有个 `RETENTION_DAYS`（默认 **1200 天 ≈ 3.3 年**）兜底：每 12 小时随分区预热顺手 `DROP` 掉上界早于保留期的旧周/月分区、并删两张非分区表的超期行——**默认值大于 3 年，所以三年内绝不触发删除**；设成 `0` 可彻底关闭、永久留全史。

---

## 6. 存储 + 展示：Docker 那三个容器

`docker-compose.yml` 定义了 3 个容器（开机由 `start_all.bat` 里的 `docker compose up -d` 拉起）：

| 容器 | 镜像 | 端口 | 干嘛 |
| :--- | :--- | :--- | :--- |
| `audit-postgres` | postgres:15-alpine | `45432→5432` | 数据仓库。数据存在宿主机 `./postgres_data` 目录。 |
| `audit-ingester` | 本地 Dockerfile 构建 | 无 | 每 10 秒轮转唯一 spool 段并入库；有限连接/语句超时、幂等事件 ID、无 payload heartbeat 和 Docker healthcheck 防止整批静默卡死。 |
| `audit-grafana` | grafana-oss:13.0.2 | `43000→3000` | 网页大盘。版本固定，避免 `latest` 自动升级再次破坏已验证的面板配置。 |

**账号/端口速查**：

- PostgreSQL：`localhost:45432`，库 `time_audit`；本机口令只从当前用户环境变量 `TIMEAUDIT_DB_PASSWORD` 注入，不进入 Git、日志或命令行。固定宿主端口须避开 Windows 动态端口池，必要时用 `TIMEAUDIT_DB_HOST_PORT` 覆盖并先通过 PCConfig 端口门禁。
- Grafana：浏览器开 `http://localhost:43000`。

**PostgreSQL 性能配置写在 `docker-compose.yml` 的 `command:` 里**（不是 postgresql.conf）：`shared_buffers=2GB`、`work_mem=16MB`、`effective_cache_size=8GB`，以及 NVMe 友好的 `random_page_cost=1.1` / `effective_io_concurrency=200`；外加 `shm_size: '512mb'`（并行查询大分区时 `/dev/shm` 的上限，防 "could not resize shared memory segment ... No space left on device"）。改这些要编辑 compose 后 `docker compose up -d audit-db` 重建容器才生效。

**容器时区锁定为 `Asia/Shanghai`**（compose `command:` 里的 `-c timezone=Asia/Shanghai`）。这是功耗大盘"今日/本周/本月"统计正确的前提——这些面板用 `date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')` 当边界，若会话时区是 UTC，边界会整体偏移 8 小时（"今日能耗"会从早上 8 点才开始算）。**别删这行**，否则 `docker compose up -d` 重建后时区 bug 复现。

> ⚠️ 所有仪表盘统一引用 Grafana 13 已实测稳定的内置 PostgreSQL 数据源 `P7A9DAD60F8AB4C18`，provisioning 也用这个固定 UID 注入 `TimeAudit-PostgreSQL`，保证新机 JSON 恢复不会指向不存在的数据源。旧 `bfoc1vymtgni8a` 只作为现有本机配置记录保留；它曾在查询取消/页面导航竞态下把错误误映射为 `plugin.notRegistered`，现行备份与恢复入口都会拒绝再次引入它。

---
