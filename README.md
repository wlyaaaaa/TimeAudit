# TimeAudit 🦅 | Windows 11 原生全时段无感工时遥测数仓

TimeAudit 是一个专为个人主站和硬核极客打造的 **Windows 11 活体进程、前台全量上下文及顶级硬件能效** 全时段秒级遥测异步数仓基础设施。

整个后端完全脱离传统客户端或易崩溃的 WMI 依赖，依托 Python 高并发异步流控管道进行动态采样，将捕获的数据流无感拍入本地 Docker 部署的 PostgreSQL 15-alpine 时序分区桶中。通过微秒级主时钟对齐机制，在前台 Grafana 网页端完美呈现一台怪兽级工作站的数字生命全景图谱。

---

## 🚀 终极破案：六大王牌尸检与常态化审计功能

### 1. 像素级“时空穿梭”卡顿确诊
* **痛点**：在打游戏团战、写代码大规模编译、或者无边框拉伸窗口时，画面发生了一次恶心的轻微掉帧或微顿卡。
* **数仓破案**：事后来到大盘框选卡顿发生的 2 秒区间。大盘依靠事实表 `PRIMARY KEY (timestamp)` 的微秒级对齐能力，直接把这一秒内宏观的 1% Low FPS 塌陷、Frametime Jitter 突刺，与微观上哪个后台流氓进程突然暴涨的 `proc_disk_read_rate_mb`（读盘速率）或网络丢包红墙，百分之百像素级拼装在同一条垂直时间线上。

### 2. 后台“篡位者”与“隐藏李鬼”深度清算
* **痛点**：某些未知程序在后台偷偷作恶，或者未知木马流氓软件篡改伪装成系统核心组件名。
* **数仓破案**：在大盘中通过 `is_foreground = 0` 全面清查非前台活动。得益于 `dim_process_registry` 维度表追加的全面指纹特征，大盘会把铁证直接拍在内鬼脸上：
  * **动机清算**：通过 `command_line` 直接看清它背后执行的完整静默脚本参数与恶意下载源。
  * **特权审计**：通过 `is_elevated` 抓出它是否暗中通过提权拿到了 Administrator 权限。
  * **真伪识破**：通过 `signature_status` 数字签名状态判定，一眼看穿它到底是微软官方核心文件（值为1），还是无签名的病毒伪装者（值为0）。

### 3. `svchost.exe` 寄生内鬼终极剥离
* **痛点**：Windows 任务管理器里最让人头疼的是，经常看到数十个 `svchost.exe` 吃满了 CPU 或疯狂读写硬盘，但你根本不知道这堆母进程里到底是哪一个具体服务在作恶。
* **数仓破案**：探针在内存层深度解耦。当映像名为 `svchost.exe` 时，活动舱通过 `proc.services()` 内核流自动识别，在数仓中通过 `service_name` 字段精准还原出其背后寄生的具体系统服务（如 `SysMain` 或第三方驱动服务名），直接把锅扣到具体的幕后黑手头上。

### 4. 窗口形态与拉伸重绘冲突审计
* **痛点**：很多卡牌游戏或挂机工具在窗口化运行、高频切屏时，往往会引发诡异的底层排队延迟。
* **数仓破案**：大盘通过联动 `window_mode`（全屏/窗口化形态）与全局的 `system_dpc_latency`（DPC 延迟）、`system_context_switches`（上下文切换率），一眼判定是不是由于窗口化拉伸触发了系统的底层 D3D 重绘冲突。

### 5. 9950X3D 跨 CCD 调度瘫痪与 RTX 5080 撞墙尸检
* **痛点**：顶级硬件没有跑满满血性能，出现莫名其妙的帧率抖动。
* **数仓破案**：
  * **CPU 侧（AMD 9950X3D）**：通过 `proc_cpu_affinity` 锁死 32 位全线程掩码，并在数据层引入 `cpu_ccd0_usage` 与 `cpu_ccd1_usage`。复盘时直接透视游戏进程主要跑在哪个 CCD 上。如果发现游戏线程横跨或挤占到了非常规高频的 CCD1，而大缓存 V-Cache CCD0 发生瘫痪，一针见血确诊系统调度瘫痪。
  * **GPU 侧（NVIDIA RTX 5080 Blackwell）**：硬件性能舱内嵌 Blackwell 极限能效解码器，高频捕捉 `gpu_throttling_reasons` 二进制状态位，秒懂 5080 在那一秒钟发生降频掉速是因为撞了功耗墙（`SW_POWER`）、温度热点（`HW_THERMAL`）超标，还是电源瞬时压降导致的电压墙。

### 6. 进程无故闪退“死因溯源”
* **痛点**：写代码跑脚本或大型游戏在毫无征兆的情况下突然“咔哒”一声闪退，桌面上什么都没留下，Windows 事件查看器里只有一堆废话。
* **数仓破案**：事实表通过 `exit_code` 牢牢锁死退出代码。你可以在卡顿舱中直接调出它死前最后一秒的全量账单（比如 `0xC0000005` 代表内存越界非法访问直接崩溃，或者是被哪个父进程强行掐断），真正做到活要见人，死要见尸。

---

## 🎨 视觉常态化大盘呈现 (`Grafana`)

数据层在月度/周度分区和覆盖索引的加持下，在本地形成了冷酷、没有断层的“全维硬件时空长河”。在 Grafana 前端定制了**“PC 时空穿梭主舱”**页面：

* **顶层宏观常态面**：用大字报常态化显示系统今天的累计开机时长（已剔除睡眠）、键鼠活跃估计总时间、显示器熄灭时长及深睡眠状态。
* **中层时空复盘轴**：一根雷打不动的横向时间轴。无论你把时间拖到哪一天，下方的折线图都会平滑地拉出那一天的温度、功耗、网络 Ping 值以及 Packet Loss 心电图。
* **底层微观动机网**：在大盘里框选任意一个常态区间，下方会自动联动专注时间流（心流时刻），清晰地按使用时长和开销降序排列出那段时间你运行过的所有前后台程序和具体窗口上下文（如：正在用 Chrome 查阅 `Google Gemini` 的具体业务）。
* **算力电费与健康老化大账单**：锁死 `cpu_package_power` 与 `gpu_board_power`。通过积分公式 $\sum (\text{功耗} \times \text{时间})$，精准算出来你的电脑通宵跑 AI 训练或大规模编译消耗了多少度电，并对比数月满载下的平均温度线，在显卡硅脂热解缩缸前发出自主更换提示。

---

## 🛠️ 后端分布式探针架构

```text
TimeAudit/
├── main.py                # 中央主控心跳核心 (带自愈外壳与日志 50MB 无感物理截断)
├── context_worker.py      # 前台上下文舱 (Win32 API 高频轮询、切窗焦点句柄状态机)
├── activity_worker.py     # 活跃进程舱 (批量进程元数据动态提取、网络连接及磁盘 I/O Delta 运算)
├── hardware_worker.py     # 硬件性能舱 (Blackwell 状态位解码、PDH-CPPC 引擎、PresentMon 监听器)
├── lifecycle_worker.py    # 离散事件舱 (内存级 PID 差分引擎，秒级判定 START 与 EXIT 离散事件)
├── Dockerfile             # Ingest 异步数据管道离线自动化构建配置
├── docker-compose.yml     # 核心持久层生态 (PostgreSQL 15 + Grafana OSS)
├── start_all.bat          # 宿主机开机自启完全隐形守护脚本
└── 任务管理器.docx        # 数仓架构王牌设计白皮书
```

### 💎 硬核工程优化亮点

1. **网络流控动态清洗**：针对浏览器（Chrome/Edge）等高频产生冗余 CLI 进程的特性，`sanitize_command_line` 采用精准正则，将带有动态句柄、客户端 ID（如 `--mojo-platform-channel-handle=\d+`）的复杂命令行统一剪裁为标准化占位符，**降低维度表 90% 冗余碰撞率**。
2. **多候选 PDH CPPC 引擎底座**：针对 Windows 核心技术计数器容易因主板型号、系统语言不同而绑定失败的问题，硬件性能舱开发了多候选路径探测绑定器，并网自愈ACPI，在 PDH 传感器未就绪时自动启动 WMI 星链降级与物理拟合补偿机制。
3. **高精度用户态 DPC 延迟监测**：主循环并发挂载 `DpcLatencyChecker` 线程，通过调用内核 `winmm.timeBeginPeriod(1)` 锁死微秒级时钟切片，高频测量硬件中断排队引发的用户态微观抖动（Jitter）。
4. **零开销内存高速缓存**：在 `activity_worker` 中引入 `path_elevation_cache` 路径-特权本地高速缓存，当高频扫描遇到进程已死的极端场景，系统逐级降级从内存和归档中打捞历史状态，彻底消灭单人工作站并发穿透开销。
5. **绝对主时钟对齐**：主控循环末尾采用精确计时对齐算法，动态计算当前一轮所有舱室采样耗费的真实毫秒数，并在 `asyncio.sleep()` 中物理扣除该增量值，**彻底消灭累积时钟漂移**。

## 🗄️ 数仓 DDL 物理分区与索引蓝图

项目在持久层建立了极其严密的覆盖索引与物理范围划分策略，确保高频采样下 NVMe 固态硬盘的 I/O 永远通透：

### 维度指纹表：`dim_process_registry`

通过精细的复合唯一索引，杜绝多舱并发插入时的事务死锁与主键冲突：

SQL

```
CREATE UNIQUE INDEX idx_unique_process_bloodline ON dim_process_registry (
    process_name, 
    md5(executable_path), 
    COALESCE(parent_process, ''::character varying),
    md5(COALESCE(command_line, ''::text)), 
    COALESCE(service_name, ''::character varying),
    is_elevated, 
    signature_status
);
```

### 时序事实表物理 Range 分区分配

数据层采用全自动预热机制（`auto_warmup_partitions`），每 12 小时自动预构建并绑定未来物理舱室：

- **`fact_process_context`（上下文切窗事实表）**：按**周**进行物理分区划分（如 `fact_process_context_y2026w24`）。
- **`fact_process_activity`（进程活跃事实表）**：按**周**进行物理分区划分，并针对频繁查询的 CPU/GPU 指标挂载了覆盖索引 `idx_process_activity_ts_key_covering`。
- **`fact_process_lifecycle_events`（生命周期离散事实表）**：挂载 `idx_lifecycle_lookup` 索引，实现对死前最后一秒的全量追踪。
- **`fact_system_hardware`（系统硬件性能事实表）**：按**月**进行高精分区（如 `fact_system_hardware_y2026m06`），全量承载 FPS、DPC 延迟、电压及双 CCD 负载。

## 🛠️ 宿主机自动化运维指南

### 1. 实时查看日志流 (PowerShell 原生支持)

得益于 `main.py` 的行级物理缓冲，你可以在任何时候打开 PowerShell 运行以下指令，微秒级流式同步查看探针内核的“迎接新生”和“临终尸检”报告：

PowerShell

```
Get-Content -Wait -Tail 10 E:\TimeAudit\telemetry.log -Encoding utf8
```

### 2. 物理截断保障

当 `telemetry.log` 的物理体积超越 **50MB** 时，中央主控会自动在预热周期中安全剥离当前接管的句柄，执行 `"w"` 模式无感清空置零，PowerShell 的 `-Wait` 句柄不会中断，同时确保 VS Code 任何时候都能秒开。