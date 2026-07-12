# =====================================================================
#  TimeAudit 数据库自动备份脚本  (backup_db.ps1)
# ---------------------------------------------------------------------
#  作用：把容器里的 PostgreSQL 数据库 time_audit 完整导出成一个压缩备份
#        文件，存到 G:\80_Backup\TimeAudit\postgresql\，并自动删除过期的旧备份。
#
#  用法（在普通 PowerShell 里直接跑即可，不需要管理员）：
#      cd E:\Projects\Tools\TimeAudit
#      powershell -ExecutionPolicy Bypass -File .\backup_db.ps1
#
#  可选参数：
#      -RetentionDays 30     # 保留最近 30 天的备份（默认 14 天）
#      -BackupDir D:\bak     # 备份存到别的盘（默认 G:\80_Backup\TimeAudit\postgresql）
#
#  恢复方法见《快速部署.md》"换电脑 / 容灾恢复"一章。
#
#  建议：用 Windows 任务计划程序设为每天 20:40 自动跑一次（命令见文末注释）。
# =====================================================================

param(
    [int]$RetentionDays = 14,
    [string]$BackupDir  = "G:\80_Backup\TimeAudit\postgresql",
    [string]$Container  = "audit-postgres",
    [string]$DbUser     = "leyang",
    [string]$DbName     = "time_audit"
)

$ErrorActionPreference = "Stop"
$ts        = Get-Date -Format "yyyyMMdd_HHmmss"
$fileName  = "time_audit_$ts.dump"
$localPath = Join-Path $BackupDir $fileName

Write-Host "[*] TimeAudit 数据库备份开始 ..." -ForegroundColor Cyan

# 1) 确保备份目录存在
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Write-Host "[*] 已创建备份目录: $BackupDir"
}

# 2) 确认容器在运行
$running = (docker ps --filter "name=$Container" --filter "status=running" --format "{{.Names}}")
if ($running -notcontains $Container) {
    Write-Host "[X] 数据库容器 '$Container' 没有在运行，无法备份。请先 'docker compose up -d'。" -ForegroundColor Red
    exit 1
}

# 3) 在容器内用 pg_dump 导出"自定义压缩格式"(-Fc)。
#    这种格式自带压缩、支持 pg_restore 选择性/并行恢复，是官方推荐的备份格式。
#    -i 把容器 stdout 二进制流原样接到宿主机文件，避免 PowerShell 文本编码破坏二进制。
Write-Host "[*] 正在导出数据库 (pg_dump -Fc) ..."
try {
    & cmd /c "docker exec -i $Container pg_dump -U $DbUser -d $DbName -Fc > `"$localPath`""
    if ($LASTEXITCODE -ne 0) { throw "pg_dump 返回非零退出码 $LASTEXITCODE" }
} catch {
    Write-Host "[X] 备份失败: $_" -ForegroundColor Red
    if (Test-Path $localPath) { Remove-Item $localPath -Force }
    exit 1
}

# 4) 校验文件非空
$size = (Get-Item $localPath).Length
if ($size -lt 1024) {
    Write-Host "[X] 备份文件异常（小于 1KB），可能导出失败，已删除。" -ForegroundColor Red
    Remove-Item $localPath -Force
    exit 1
}
$sizeMB = [math]::Round($size / 1MB, 2)
Write-Host "[+] 备份成功: $localPath  ($sizeMB MB)" -ForegroundColor Green

# 5) 清理过期备份（只删本脚本生成的 time_audit_*.dump，绝不误删别的文件）
$cutoff  = (Get-Date).AddDays(-$RetentionDays)
$expired = Get-ChildItem -Path $BackupDir -Filter "time_audit_*.dump" |
           Where-Object { $_.LastWriteTime -lt $cutoff }
foreach ($f in $expired) {
    Remove-Item $f.FullName -Force
    Write-Host "[*] 已清理过期备份: $($f.Name)"
}

$kept = (Get-ChildItem -Path $BackupDir -Filter "time_audit_*.dump").Count
Write-Host "[+] 完成。当前共保留 $kept 个备份（保留策略：$RetentionDays 天）。" -ForegroundColor Green

# =====================================================================
#  设为每天自动备份（在【管理员】PowerShell 里执行一次即可）：
#
#    $A = New-ScheduledTaskAction -Execute "powershell.exe" `
#         -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File E:\Projects\Tools\TimeAudit\backup_db.ps1"
#    $T = New-ScheduledTaskTrigger -Daily -At '20:40'
#    $P = New-ScheduledTaskPrincipal -LogonType Interactive -RunLevel Highest
#    Register-ScheduledTask -TaskName "TimeAudit_DailyBackup" -Action $A -Trigger $T -Principal $P -Force
#
#  恢复（导入到一个干净的新库）：
#    docker exec -i audit-postgres pg_restore -U leyang -d time_audit --clean --if-exists `
#        < G:\80_Backup\TimeAudit\postgresql\time_audit_YYYYMMDD_HHmmss.dump
# =====================================================================
