# PersonalOS 电脑剪贴板历史 sidecar

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

当前约定的普通入口名称是 `PersonalOS 剪贴板历史`。活动库属于 E 盘持久数据层，G 盘只做近线恢复副本，查询不依赖 G。

## 验证

```powershell
python -m unittest -v test_clipboard_history.py
python -m compileall -q clipboard_history
```

真机测试必须先启动 collector 形成 baseline，再运行 `python -m clipboard_history.smoke_test --data-root <private-root>`。该测试主动写入唯一合成内容，验证普通 observation 与一次带 lineage 的历史恢复，不读取或输出测试前已有剪贴板。

跨 owner 消费只使用 `adapter_stdio.py` 的 `timeaudit.clipboard-export.request.v1` / `response.v1` 合同和 `(observed_at_utc,event_id)` checkpoint，不直接写 spool。source profile 固定为 `src.timeaudit.windows_clipboard`，明确不同于电脑活动来源 `src.timeaudit.pc_activity`。当 `include_payload=true` 时 stdout 是获准调用方的瞬时私密运输面，调用方必须直接管道消费，不得终端展示、写日志或缓存。
