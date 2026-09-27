# 进程明细与长期统计

用户在 DU3 选择：逐进程明细保留最近 60 天，另存长期小时、日汇总。
仅 `fact_process_activity` 改用这项策略。前台会话、AHK 使用时长、硬件采样、生命周期事件继续原有保留范围。

## 看板与统计含义

- 原资源排行、磁盘/网络累计量和整机趋势面板直接读混合查询，不另建一套看板。资源看板增加 CPU、内存的应用合计小时/日趋势，保留原友好应用名称。曲线在缺测处插入 NULL 断线点，不把采样空洞画成连续线；日段按日、小时段按小时、近期细粒度按实际查询桶处理。
- 最近 60 天可查逐进程明细。只清理上界早于截止时刻的完整周分区，因此周边界最多多留不足一周。
- `activity_app_hour/day` 按应用名和可执行路径分组，保留各项指标的总和、有效样本数、峰值、采样数和无响应采样数。同时保存原单进程样本口径和 `app_total_*` 应用合计口径：应用合计先按同一时刻、应用名和路径将多个 PID 相加，每个采样时刻仅贡献一次；各指标独立计有效样本数。汇总不保存 PID、进程注册键、命令行或远端地址。原注册维表和其他事实表的保留策略不变。
- 日界为北京时间。日均值使用各指标总和除以各指标有效样本数，不能平均小时均值。NULL 不补零；零确实采到了才是零。未注册的应用保留为显式未知应用，不因维表缺失消失。
- 原进程排行保留**单进程采样**口径并在标题标明；新的应用趋势使用**应用合计**口径。同一应用 A 两个 PID 同时 40% 和 30%，A 的应用合计峰值是 70%，单进程峰值是 40%；若另一应用 B 同刻 20%，整机峰值是 90%。错时出现的 40% 和 30% 不能相加成 70%。两套统计均长期保存，旧明细退役后仍可查询 A 的合计峰值。
- 磁盘和网络“活跃样本”排行保留原筛选条件，另存其有效计数与统计；整机累计量仍包括所有样本，微小速率不会被筛选丢掉。
- 累计读写/网络量沿现有 `速率 × 3.1 秒` 口径，为**估算**。网络本身是整机速率按连接数量占比分摊，并非逐进程精确计量。不会把休眠、停机或缺测跨度乘进总量。
- 完整且已汇总的小时/日走持久汇总；近期查询两端不足一小时和尚未汇总的尾段仍读原始数据。近期小于一小时或非整小时倍数的时间桶保持原采样分辨率。已退役的历史边界不足一小时只能按完整小时统计，不能伪造秒级精度。
- PID、IP、亲和性、窗口关联、分钟级卡死/泄漏等细节只读尚存明细。面板标题/说明明确最近 60 天；跨越已退役历史时，空白不表示零活动或无异常，卡死面板不会把已丢失的历史显示成“无卡死”。

## 工作方式

实现位于 `process_activity_retention.py`，由 `main.py` 已有的 12 小时维护任务串行调用，不新增计划任务或后台进程。维护 SQL 单条超时明确为 900 秒，旧分区锁等待仍限 5 秒；采集连接池的 5 秒默认超时不变。取消或查询超时使当前事务回滚，已提交日段可以继续。

一行 `activity_retention_state` 保存部署开关、原始明细起点、首次回填已完成日段的上界和完整汇总覆盖上界。首次安装 `enabled=false`：安装、回填和看板部署均不会自行启用清理。完整回填结束后才标记汇总可读；每个日段的汇总与 `backfill_until` 同事务提交；首次回填中断后重跑从已完成上界继续，失败段会重新执行。未完成的首次回填仍由原始数据提供查询。完整回填后再明确运行 `backfill` 则从尚存原始数据起点全量重算，供补写旧行后刷新。

周期维护重算最近两天，并补齐上次维护结束之后的所有时间，跨长停机/维护失败不会跳过中段。小时覆盖窗口采用替换写入，日汇总由持久小时重新合并；相邻分区在同一天分界、前一半日已经退役时，也不会擦掉前半日汇总。超过两天才补写的旧活动数据，可运行 `backfill` 更新；最终退役前始终再扫描源分区。

启用后，最终退役操作在同一事务内锁定旧分区，按日重建它的汇总，核对应用小时采样总数与原始行数，再 DROP 并推进原始明细起点。锁等待超过 5 秒、回填失败、核对失败或 DROP 失败则整笔回滚，不推进起点。其他维护/CLI 通过数据库建议锁串行，忙时自动维护本轮跳过。分区必须有显式时区、整小时边界且在 `public`；不支持的结构保留并报错。

## 部署顺序（先命令准备，最后由用户操作 GUI）

数据库命令使用已有合法凭据注入的 `TIMEAUDIT_DB_PASSWORD` 环境，不把凭据写进脚本或输出。先合并源码并同步活动检出，再执行：

```powershell
$ta = 'E:\Projects\Tools\TimeAudit'
$python = "$ta\.venv\Scripts\python.exe"
& $python "$ta\process_activity_retention.py" install
& $python "$ta\process_activity_retention.py" backfill --max-days 1
# 观察首日实际 source_rows、elapsed_seconds、source_rows_per_second，再接续：
& $python "$ta\process_activity_retention.py" backfill
& $python "$ta\process_activity_retention.py" status
# 此时必须保持 enabled=false；完整回填后 summaries_ready=true。
```

本次仅部署以下四份 JSON，保留原 UID、原文件夹和数据源 `P7A9DAD60F8AB4C18`；不恢复仓库中另外两份未改动看板：

```powershell
$dashboards = @(
  'addforex__🔍 进程取证与安全审计舱.json'
  'addmc8x__🚀 前台交互与流畅度诊断舱.json'
  'addrd7x__🐀 资源大户与后台内鬼.json'
  'b7d809e5-d072-4d24-ae23-c573bfcabc56__🖥️ 整机硬件能效与系统资源大盘.json'
)
foreach ($dashboard in $dashboards) {
  & $python "$ta\restore_grafana.py" --file (Join-Path "$ta\grafana_dashboards" $dashboard) --dry-run
  if ($LASTEXITCODE -ne 0) { throw "Dashboard validation failed: $dashboard" }
}
```

本次现场正常 API 返回 401，未取得现役入口可合法注入的 Grafana API 凭据。当前路线是在完成上述数据库准备后继续保持 `enabled=false`，最后由用户登录既有 Grafana，通过 GUI 导入这四份 JSON，选择原文件夹并按原 UID 覆盖已有看板，保留既有数据源。随后通过现有 Grafana SQLite **只读**入口回读四份定义、UID、文件夹与数据源，核对查询及提示已更新；不能直接修改 SQLite 来代替导入。

以后只有既有合法凭据环境可用时才可改用 API，并且应先回读原 `meta.folderUid`，按原值导入。当前 `restore_grafana.py` 写接口固定使用空 `folderUid`，所以本次只用它做精确 `--file --dry-run`，不要直接去掉 dry-run 后声称保留了原文件夹。无需为本次更改认证、增建凭据层或写入 SQLite。

父任务还应选一个旧完整日和一个近期跨小时窗口，用直接 SQL 与 `activity_app_stats` 核对各指标 sum/count/max、采样数及应用合计峰值，确认最近小时查询和历史统计可见。独立审查、四个看板导入及读回、历史可见验收完成后，才执行下面的实际清理步骤；GUI 尚未完成时不得提前启用：

```powershell
& $python "$ta\process_activity_retention.py" enable
& $python "$ta\process_activity_retention.py" maintain
& $python "$ta\process_activity_retention.py" status
```

`backfill --max-days 1` 只完成一个最多 24 小时的段，不声明全量覆盖、不启用删除。随后普通 `backfill` 从进度继续。CLI 每段输出源样本数、耗时和样本/秒；首段样本密度可能不同于后续，按实际后续段持续修正估时，不能拿分区物理字节除首段样本吞吐冒充准确磁盘吞吐。回填仅持普通读锁及汇总写锁，可继续采集；父任务同时监测落库新鲜度与查询延迟。

`maintain` 会实际删除已完成最终汇总的旧分区；首次真实回填/退役可能耗时。最终通过数据库读回剩余分区上界、汇总表大小、`raw_since`，检查最近 60 天仍完整存在，并核对采集心跳/落库新鲜度。已运行的旧 `main.py` 需要沿现有无窗口启动/看门狗路线重启一次，才能接入新的周期维护；停止/恢复窗口如实记录。无需打开浏览器或操作 GUI。

单独换版优先使用现役 `TimeAudit_Watchdog`（`wscript.exe telemetry_watchdog_hidden.vbs`）。先确认 DB、LHM、ingester 健康且无备份冲突；在同一 `Global\TimeAuditTelemetryWatchdogMutex` 内定向停止精确旧 `main.py` 进程，释放后触发该既有看门狗任务，让它按缺失进程恢复支路创建隐藏 `pythonw.exe`，回读新 PID、心跳和活动落库。不要用 `TimeAudit_AutoStart` 代替纯引擎重启：其批处理会启动 Docker Desktop 程序，可能激活 GUI。源码支持的路线不等于这次已经做过生产重启。

用户最后在原 Grafana 看板中检查三件事即可：最近一小时曲线；三个月资源排行及 CPU/内存趋势；超过 60 天的细节面板保留提示。此项可见体验验收由用户操作，不能用 SQL 测试代替。

## 回退与恢复边界

```powershell
& $python "$ta\process_activity_retention.py" disable
```

该命令与维护共用串行锁；在跑的分区事务完成后才会取得锁并关闭后续清理。需要立即中止在跑维护时，先按现有运行管理方式结束该维护连接/进程，让当前数据库事务回滚，再执行 disable；不要并发强改状态。

**尚未退役任何分区时**：关闭开关即可保留全部明细；可恢复旧版看板和代码，新增汇总表留待确认不再使用后处理。

**已经退役旧分区时**：关闭开关只停止以后的退役，小时/日汇总不能还原 PID、命令行、IP 或每秒样本。保留混合查询看板和 reader；直接还原旧 SQL 会让历史统计空白。若确需恢复明细，先在隔离数据库还原既有完整备份、核对目标分区，再安排定向恢复和状态起点调整，不覆盖正在采集的数据库。未核验备份可用之前，不能承诺明细可回滚。

## 回归

`test_process_activity_retention.py` 使用真正的 PostgreSQL，仅接受名称以 `timeaudit_retention_test` 开头的独立合成数据库。数据全为人工构造，测试不连接生产数据库。

```powershell
$env:TIMEAUDIT_RETENTION_TEST_DSN = 'postgresql://postgres@127.0.0.1:<测试端口>/timeaudit_retention_test'
& $python -m unittest test_process_activity_retention test_main_cadence test_grafana_dashboard_contract test_restore_grafana -q
```

覆盖 NULL/加权、多 PID 应用合计与错时峰值、每条曲线的小时/日缺测断线、真实 main 入口的短超时池接线和取消回滚、首次回填断点接续、日界与非日界分区、重复回填、晚到样本、60 天跨界、长维护中断、混合查询边界、细粒度分辨率、整小时原始表零行索引探测、DROP 失败回滚、并行维护互斥和所有改后 SQL。合成回归通过不等于生产回填、看板发布或空间回收已经完成。
