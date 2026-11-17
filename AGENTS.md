# TimeAudit 项目约定

- **是什么：** 记录和解读这台电脑用过的情况：硬件、进程、窗口使用、游戏帧率、卡顿、用电和剪贴板历史的采集、算法、守护采集器的脚本、备份和算好的结果。
- **不做什么、归谁：** 机器接线归 PCConfig：路径、端口、计划任务的安装、登记和重建，恢复顺序，实况网站后端，以及按这些记录去动机器（内存守护、桌面急救）。一句话判断：在记录或解读这台电脑过去发生了什么的，归本仓；让电脑和各项目接得上、装得上、恢复得了的，归 PCConfig。
- **交界处：** 本仓的计划任务由 PCConfig 的安装器装（`Install-TimeAuditRuntimeWatchdog.ps1`、`Install-TimeAuditClipboardHistory.ps1`），任务里跑的脚本放本仓。本仓的数只由本仓算，实况网站要的数由本仓算好交出（如 `log/power-today.json`、`personal_activity_reader.py`），PCConfig 后端只读，缺数在本仓补出口。归本仓管、代码暂在 PCConfig 的：① `E:\PCConfig\tools\remote-computer-mcp` 的 `computer_panels_a.py`、`computer_panels_b.py`、`public_computer_panels.py` 自己查本仓库重算使用时间、应用、卡顿、用电，本仓出口上线后收回本仓，届时删掉后端里的重算；② 卡顿留现场和内存黑匣子不搬：同目录 `freeze_capture.py`、`wait_chain.py`、`live_kernel_dump.py`（跑在实况后端服务里），`E:\PCConfig\tools` 的 `Get-MemoryFreezeEvidence.ps1`、`Invoke-MemoryFreezeDiagnostics.ps1`、`Get-DesktopFreezeSnapshot.ps1`、`Get-ComputerStutterDiagnostic.ps1` 和任务 `PCConfig Memory Diagnostics`；③ 剪贴板守护脚本 `E:\PCConfig\tools\Start-TimeAuditClipboardCollector.ps1`。

- Windows 启动批处理保留 CRLF；计划任务不可靠继承交互用户 PATH，启动脚本使用绝对路径并保留 Docker/Git PATH 引导。改路径或启动脚本后运行 `python test_start_all_docker_bootstrap.py`，再核对任务指向当前项目。
- `telemetry_watchdog.ps1` 负责有界恢复 main.py、TimeAudit.ahk 及其既有依赖；不要把一次性自启任务改成并行常驻采集器。采集器保持单一 owner，失败时保留缺口。
- 休眠/唤醒和落库时间用墙上时间；CPU、网络、I/O 速率的差分分母用单调时钟。前台会话时长不得为负，闭合 `fact_process_context` 必须带 `timestamp` 分区键。
- 每进程 CPU 按逻辑核归一到 0–100%；GPU 进程数据只绑定 NVIDIA 独显。NVML 易失败项分别隔离；RTSS、PresentMon、LibreHardwareMonitor 各遵守现有 owner 边界。
- 无响应判断使用交互会话中的 `IsHungAppWindow`；引擎需要用户交互会话及现有提权运行条件，不改为 SYSTEM。父进程名使用同一系统快照的 pid→name，不逐进程调用 `psutil.Process.parent()`。
- 活跃进程扫描留在 `asyncio.to_thread`，进程差分基线即使单项异常也继续推进；PG 断连时池关闭要有超时和 terminate 兜底，避免阻塞采集节拍。
- Grafana SQL 的 Windows 路径匹配避免反斜杠被 LIKE 转义；时间网格先对齐桶边界。PG 会话时区保持 `Asia/Shanghai`，以正确计算本地日界。
- Grafana V1 折叠行之后需要常显的面板，用显式展开行隔开；部署后通过 `v2/dto` 回读核对实际行归属，顶层面板数组不代表可见状态。
- 分区事实表查询和关联都要下推明确时间界。`app_usage_logs` 是可重叠的区间事件：按查询窗裁剪并对重叠区间求并集，空集归零；不能仅按起点筛选后直接求和。
- 诊断中缺测、旧尾段和实际覆盖分开报告；心跳新鲜不等于活动已经落库。业务读取和历史诊断接口分别见 `PERSONAL_ACTIVITY_READER.md`、`TIMEAUDIT_DIAGNOSTIC_SUMMARY_CONTRACT.md`。
- 实机在线检查与离线回归分开。离线回归按改动选择 `test_*.py`；全量 `pytest` 包含依赖和在线环境要求，运行时诊断见 `DIAGNOSTICS_OPERATIONS.md`。NVML mock 不可把假句柄传到真实原生函数。
- 剪贴板历史是独立 sidecar，见 `clipboard_history/README.md`；不将其内容写入 TimeAudit 数据库。
