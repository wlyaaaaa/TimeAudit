# =====================================================================
#  TimeAudit 一键全量备份（数据库 + Grafana 仪表盘）
#  每天由计划任务 TimeAudit_DailyBackup 自动调用；也可手动跑：
#      powershell -ExecutionPolicy Bypass -File E:\TimeAudit\backup_all.ps1
#  运行输出写入 E:\TimeAudit\log\backup.log（每次覆盖，便于查看最近一次结果）。
# =====================================================================
$ErrorActionPreference = "Continue"
# 让子进程(python/powershell)的 UTF-8 输出经重定向写进日志时不被二次编码成乱码。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location "E:\TimeAudit"

if (-not (Test-Path "E:\TimeAudit\log")) { New-Item -ItemType Directory "E:\TimeAudit\log" -Force | Out-Null }
$log = "E:\TimeAudit\log\backup.log"

# 显式补全工具路径，免疫计划任务里 PATH 不全（git/docker 通常在系统 PATH，这里兜底）。
$env:PATH = $env:PATH + ";C:\Program Files\Git\cmd;C:\Program Files\Docker\Docker\resources\bin"
# 强制 Python UTF-8 模式，避免非交互环境下 stdout 默认 GBK 编不了 ✓/❌ 等字符而崩溃。
$env:PYTHONUTF8 = "1"

"============================================================" | Out-File $log -Encoding utf8
"[backup-all] start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8

# 1) PostgreSQL —— 用独立 powershell 进程跑，隔离它内部的 exit 调用
"[backup-all] 1/2 备份 PostgreSQL 数据库..." | Out-File $log -Append -Encoding utf8
& powershell -NoProfile -ExecutionPolicy Bypass -File "E:\TimeAudit\backup_db.ps1" *>> $log

# 2) Grafana 仪表盘 —— 用系统级 py 启动器，免疫 PATH 顺序/uv shim 问题
"[backup-all] 2/2 备份 Grafana 仪表盘(导出JSON + git提交 + grafana.db)..." | Out-File $log -Append -Encoding utf8
& py "E:\TimeAudit\backup_grafana.py" *>> $log

"[backup-all] done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8
exit 0
