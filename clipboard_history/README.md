# TimeAudit 电脑剪贴板历史 sidecar

这是 TimeAudit 的独立 Windows 用户态 sidecar。它只采集当前交互会话中新发生的 Unicode 文本、HTTP(S) URL 和普通文件路径列表，写入带 WAL/FTS5 的私密 SQLite；不依赖 `main.py`、Docker、PostgreSQL 或 Grafana，也不向它们写入剪贴板内容。

## 不变量

- 采集器使用 `AddClipboardFormatListener` / `WM_CLIPBOARDUPDATE`，不轮询剪贴板，不安装键盘 hook、DLL、驱动或 Session 0 服务。
- 首次启动、解锁、恢复和从暂停继续时只建立 sequence baseline，不导入此前的当前内容。
- 每次复制都是独立事件；内容 SHA-256 只复用 blob，不去重事件。
- `ExcludeClipboardContentFromMonitorProcessing` 和 `CanIncludeInClipboardHistory=0` 会阻止本地保存；`CanUploadToCloudClipboard=0` 不会。
- 图片、二进制、虚拟文件、私有格式、过大或暂时锁定的内容只留下无 payload 的原因事件。
- 查看器使用 SQLite `mode=ro` 与 `query_only=ON`；FTS5 缺失时明确失败，不回退为长期全表扫描。
- “再次复制”写入注册格式 `PersonalOS.ClipboardHistory.RestoreV1`；采集器仅在 marker 与原事件内容一致时标记 `history_restore` 和 `restored_from_event_id`。
- 私密数据库、WAL、hash、FTS、控制状态和 heartbeat 不得进入本 PUBLIC 仓库、日志、异常文本、浏览器缓存或命令行。

## 入口与运行

机器级精确路径、ACL、计划任务、开始菜单快捷方式、watchdog、G 盘热备和恢复命令由 PCConfig 登记。项目内入口为：

- `collector.pyw`：隐藏 Win32 消息窗口采集器；
- `viewer.pyw`：Tkinter 桌面查看器；
- `adapter_stdio.py`：显式调用的版本化 JSON/stdio 只读增量出口；
- `backup.py`：SQLite Online Backup、一致性验证和空目录恢复；
- `smoke_test.py`：只输出 marker SHA-256/计数的真机回环测试。

查看器的时间列和日期筛选统一使用固定中国标准时间（UTC+8），数据库与跨 owner
适配器仍保留规范 UTC 时间戳。

查看器与 PCConfig 的个人资料访问共用同一个解锁期和截止时间。已有有效解锁期时直接显示；
否则先保持历史列表、详情和搜索内容为空，由“解锁个人资料”按钮打开现役四选一验证。
取消只结束本次验证，不重试或续期。资料到期、主动锁定、正在关闭或状态不可用时立即停止取用并清屏，
慢查询的迟到结果不会重新显示；后台采集及原始记录保持原样。

该入口依赖已安装的 PCConfig Broker 和 B2 判定模块。首次状态确认与验证在窗口内异步执行，
后续仅通过既有 UI 定时器只读共享状态，不反复启动 Broker、不建立另一个授权库。

旧查看器仅作访问保护的过渡入口；P5 统一的加密资料查看 GUI 可用后，将把剪贴板历史融入该入口并退役旧窗口和快捷方式，后台采集与已有记录继续保留。

当前约定的普通入口名称是 `TimeAudit 剪贴板历史`。活动库属于 E 盘持久数据层，G 盘只做近线恢复副本，查询不依赖 G。

## 验证

```powershell
python -m unittest -v test_clipboard_history.py
python -m unittest -v clipboard_history.test_personal_access
python -m compileall -q clipboard_history
```

`test_personal_access` 使用已安装 B2 纯判定代码、合成状态文件与界面替身，不打开真实窗口或历史库；
覆盖已有期复用、四选一结果回读、取消、到期/锁定清屏与迟到结果丢弃。

真机测试必须先启动 collector 形成 baseline，再运行 `python -m clipboard_history.smoke_test --data-root <private-root>`。该测试主动写入唯一合成内容，验证普通 observation 与一次带 lineage 的历史恢复，不读取或输出测试前已有剪贴板。

跨 owner 消费只使用 `adapter_stdio.py` 的 `timeaudit.clipboard-export.request.v1` / `response.v1` 合同和 `(observed_at_utc,event_id)` checkpoint，不直接写 spool。source profile 固定为 `src.timeaudit.windows_clipboard`，明确不同于电脑活动来源 `src.timeaudit.pc_activity`。当 `include_payload=true` 时 stdout 是获准调用方的瞬时私密运输面，调用方必须直接管道消费，不得终端展示、写日志或缓存。
