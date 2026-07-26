# PersonalOS 剪贴板来源适配合同 v1

状态：供 PersonalOS 数据工厂后续接入的只读合同；本 sidecar 不写 PersonalOS，不修改其 checkpoint。

## 来源与版本

- 合同标识：`timeaudit.clipboard-source.v1`
- 物理 Schema：SQLite `schema_version=1`
- 只读导出面：`adapter_events_v1`
- `source_instance_id`：`windows:` 加本机稳定标识的 SHA-256 截断值；不暴露原机器 GUID。
- 一次进程生命周期由 `collector_instance_id` 标识；系统启动与交互会话分别由 `boot_id`、`session_id` 标识。

## 增量导出与 checkpoint

消费者用 SQLite URI `mode=ro` 并执行 `PRAGMA query_only=ON`，按以下稳定顺序读取：

```sql
SELECT *
FROM adapter_events_v1
WHERE (observed_at_utc, event_id) > (?, ?)
ORDER BY observed_at_utc, event_id
LIMIT ?;
```

checkpoint 是最后成功提交到 PersonalOS writer 的 `(observed_at_utc,event_id)`，由 PersonalOS 自己耐久保存。只有下游 writer 成功后才推进；重复读取同一 `event_id` 必须幂等。SQLite spool 保留原始事实，不接受消费确认、删除或回写。

## 事件映射

- `event_kind=observation`：有 `payload_type`、`payload_sha256`、`payload_text`、`payload_utf8_bytes`。
- `event_kind=skip`：没有 payload；`reason` 说明 source policy、格式、大小或暂时锁定。
- `event_kind=gap`：没有 payload；表示采集时观察到的竞态或可证明缺口，不可推断缺失内容。
- `event_kind=boundary`：没有 payload；启动、锁屏、解锁、睡眠、恢复、暂停或会话结束的边界。
- `clipboard_sequence` 只在同一 Windows clipboard station 的相邻事实中解释；不得作为跨 boot、跨 session 或跨设备全局顺序。

每个 observation 都是独立事实。相同 `payload_sha256` 只能表示 blob 等价，不能合并、删除或覆盖事件。

## 再次复制 lineage

查看器恢复成功时，新 observation 为：

- `observation_kind=history_restore`
- `restored_from_event_id=<原事件>`
- `restore_request_id=<本次请求>`

PersonalOS 可在默认呈现中折叠恢复事件，但必须保留原事件与恢复事件及其 lineage。marker 缺失、无效、内容不匹配或被外部竞争覆盖时，采集器保存为普通 `copy`，不得以时间窗口或 hash 猜测回环。

## 手机与跨设备

本合同只覆盖 Windows 电脑 sidecar。手机 `/storage/emulated/0/PersonalOS/Clipboard` 经 FolderSync 单向上传是另一个来源实例；手机/电脑的语义等价、跨设备关联、统一 checkpoint 和最终历史入口由 PersonalOS 数据工厂负责，不能在 PC collector 中合并。
