# 电脑活动业务读取

`personal_activity_reader.py` 读取 TimeAudit 已保存的应用前台会话与 AHK 状态日志，用于本人理解库的工具使用习惯、生活节律与阶段变化。它不采集新数据、不修改数据库、不启动或修复采集器，不计算效率、专注或人格评分。

## 调用

在项目现有 Python 环境中运行，Docker 容器 `audit-postgres` 须已经可用：

```powershell
python personal_activity_reader.py --after 2026-09-01T00:00:00+08:00 --until 2026-10-01T00:00:00+08:00 --output <私有结果路径>
```

示例区间的结束必须已发生。两个时间都必须含时区；区间为左闭右开。可按任意明确区间查询，包含整月，按最多 24 小时分块；单块超时或输出过大时二分重试至 1 小时，仍失败则返回错误，不截断为 168 小时或交付伪完整结果。每个数据库语句只读、20 秒超时、1 秒锁等待，单块原始输出限制 32 MiB。不要将私有结果写入代码仓库。

Python 消费方调用 `read_activity(after_datetime, until_datetime, automation=[])`。入口在各查询及返回前调用现役 `C:\ProgramData\PCConfig\AuthorityHost\tools\personal_data_access.py` 的 `check_access('factor')`，只消费共享资料期；不弹验证、不另签期限，不提供跳过检查开关。`factor` 是旧业务分类名，不创建第二次验证。失败时由所属业务处理原精确原因。`query` 是内部数据库传输函数，业务消费方应使用 `read_activity`。

可选 `--automation-context <JSON路径>` 提供已知自动操作区间数组，每项包含 `after_utc`、`until_utc` 与 `source_ref`。仅用于标注交集秒数，原始测量不被扣减或改写；没有该标注不证明是人操作。读取上下文文件也先消费同一资料检查。不要凭应用名称自动判断是谁操作。

## 输出契约

`timeaudit.personal-activity.v1` JSON 返回请求区间、`observation`（查询时刻、耗时、块数、各表保存记录起始时间的最早/最晚值）、`semantics`、已知自动操作上下文及 `chunks`。

每个块包含自身区间、查询耗时、`coverage`、`anomalies`、`groups`。每组以 source/process_name/state 分开，返回观察时长的区间并集、原行时长之和、行数、开放行数、窗口模式、首尾观察时刻和至多两个代表原件主键。完整原件集合可用组条件和区间重新定位；代表主键不是完整原件清单。跨块行可重复计数，因此块行数相加表示读取行数，不能声称唯一会话总数。代码默认不选窗口标题、命令行、路径或正文；具体问题确需原上下文时由 TimeAudit 所属读取流程按主键另取。

- `foreground` 来自 `fact_process_context` 的 `is_foreground=1`。闭合会话使用起止时间裁切；`duration_ms` 与起止时间差超过 1 秒时记录异常。开放会话不外推，单列数量；窗口前已存在的开放行由 `unclosed_before_window_count` 提示，不能把旧未闭合行当作整个请求区间都在活动。
- `ahk` 来自 `app_usage_logs` 的 start_time 与 duration_seconds。状态包括 physical_idle、display_off、lock、sleep、collection_gap、no_foreground_response、foreground_hung、capture_unknown；其他行为仅称 app_observed。
- 当前 AHK 用 `A_TimeIdlePhysical` 的 60 秒阈值，并在系统输出音频时豁免 idle。逐条记录不保存音频豁免标志或历史采集器版本，因此不能从 app_observed 倒推出有物理输入，历史阈值也不能冒充已核实。`Idle` 是无前台响应；只有 `System_Idle` 对应物理空闲。
- 每组分别做区间并集；同源重叠通过 `overlapping_row_seconds` 提示。它表示重复累计的时长，不是独立重叠墙钟时长。AHK 的 cross_state_overlap_seconds 同理。两来源、不同应用之间都不可直接相加为本人时间。
- foreground 组额外输出与 AHK 各状态的交集。它保留“窗口还在前台，同时设备日志为空闲/熄屏”等事实，不自动把这一段认作本人使用。
- `recorded_union_seconds` 包括采集间隙标记这种已持久化记录；AHK 的 `explicit_collection_gap_seconds` 单列明确间隙，`non_gap_recorded_seconds` 仅表示非间隙记录并集。未覆盖秒数、缺口数量、最大缺口、首尾记录分别返回。保存边界不能证明连续覆盖；未知始终保留。

这是设备观察，不能直接断言本人在场、醒着或工作。手机明确交互等其他证据可由上游理解库共同解释，不由本 reader 改写。上游仅需保存必要聚合与 TimeAudit 原件定位，不建立第二套逐秒时序数据库。

## 验证

```powershell
python -m unittest test_personal_activity_reader -v
```

测试包含跨边界裁切、重叠并集、状态冲突、开放/异常会话、无前台与物理空闲的区别、已知自动操作交集、整月分块、超时分割和资料拒绝前零查询。真实读取验收应只回显区间、查询耗时、覆盖和计数，不在操作记录或聊天中输出应用轨迹。采集器、启动项、数据库结构与现有诊断 provider 不受此入口影响。
