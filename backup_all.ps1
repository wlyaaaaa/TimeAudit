# =====================================================================
#  TimeAudit 一键全量备份（数据库 + Grafana 仪表盘）
#  每天由计划任务 TimeAudit_DailyBackup 自动调用；也可手动跑：
#      powershell -ExecutionPolicy Bypass -File E:\Projects\Tools\TimeAudit\backup_all.ps1
#  运行输出写入 E:\Projects\Tools\TimeAudit\log\backup.log（每次覆盖，便于查看最近一次结果）。
# =====================================================================
$ErrorActionPreference = "Continue"
# 让子进程(python/powershell)的 UTF-8 输出经重定向写进日志时不被二次编码成乱码。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location "E:\Projects\Tools\TimeAudit"

if (-not (Test-Path "E:\Projects\Tools\TimeAudit\log")) { New-Item -ItemType Directory "E:\Projects\Tools\TimeAudit\log" -Force | Out-Null }
$log = "E:\Projects\Tools\TimeAudit\log\backup.log"

# 显式补全工具路径，免疫计划任务里 PATH 不全（git/docker 通常在系统 PATH，这里兜底）。
$env:PATH = $env:PATH + ";C:\Program Files\Git\cmd;C:\Program Files\Docker\Docker\resources\bin"
# 强制 Python UTF-8 模式，避免非交互环境下 stdout 默认 GBK 编不了 ✓/❌ 等字符而崩溃。
$env:PYTHONUTF8 = "1"

function Invoke-LoggedCommand {
    param([scriptblock]$Command, [ref]$Receipt)

    try {
        $global:LASTEXITCODE = 0
        & $Command 2>&1 | ForEach-Object {
            "$_" | Out-File $log -Append -Encoding utf8
            if ($null -ne $Receipt -and "$_".StartsWith('{')) {
                try {
                    $parsed = "$_" | ConvertFrom-Json -ErrorAction Stop
                    if ($parsed.mode -in @('backup','grafana') -and $parsed.status) { $Receipt.Value = $parsed }
                } catch { }
            }
        }
        if ($null -eq $global:LASTEXITCODE) { return 0 }
        return [int]$global:LASTEXITCODE
    } catch {
        "[backup-all] ERROR $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
        return 1
    }
}

function Write-RunReceipt {
    param($RunReceipt)
    $receiptPath = Join-Path (Split-Path $log) 'backup-last-run.json'
    $tempReceiptPath = "$receiptPath.$PID.tmp"
    try {
        [IO.File]::WriteAllText($tempReceiptPath, ($runReceipt | ConvertTo-Json -Depth 2), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($receiptPath)) { [IO.File]::Replace($tempReceiptPath, $receiptPath, [NullString]::Value) }
        else { [IO.File]::Move($tempReceiptPath, $receiptPath) }
    } catch {
        '[backup-all] WARNING last-run receipt could not be written' | Out-File $log -Append -Encoding utf8 -ErrorAction SilentlyContinue
    } finally {
        if ([IO.File]::Exists($tempReceiptPath)) { Remove-Item -LiteralPath $tempReceiptPath -Force -ErrorAction SilentlyContinue }
    }
}

$startedAt = [DateTimeOffset]::Now.ToString('o')
Write-RunReceipt ([ordered]@{
    schema = 'timeaudit.backup-run.v1'; status = 'running'; started_at = $startedAt
    completed_at = $null; local_snapshot_status = 'running'; cloud_sync_status = $null
    reason = $null; summary = '备份进行中'; exit_code = $null
})
$exitCode = 0

"============================================================" | Out-File $log -Encoding utf8
"[backup-all] start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8

# 1) PostgreSQL —— 用独立 powershell 进程跑，隔离它内部的 exit 调用
"[backup-all] 1/2 备份 PostgreSQL 数据库..." | Out-File $log -Append -Encoding utf8
$databaseReceipt = $null
$dbExit = Invoke-LoggedCommand -Receipt ([ref]$databaseReceipt) -Command { powershell -NoProfile -ExecutionPolicy Bypass -File "E:\Projects\Tools\TimeAudit\backup_db.ps1" }
$dbStatus = if ($dbExit -eq 0) { 'pass' } else { 'failed' }
if ($dbExit -ne 0) {
    "[backup-all] PostgreSQL 备份失败，exit=$dbExit" | Out-File $log -Append -Encoding utf8
    $exitCode = $dbExit
}

# 2) Grafana 仪表盘 —— 用系统级 py 启动器，免疫 PATH 顺序/uv shim 问题
"[backup-all] 2/2 备份 Grafana 仪表盘(导出JSON + git提交 + grafana.db)..." | Out-File $log -Append -Encoding utf8
$grafanaReceipt = $null
$grafanaExit = Invoke-LoggedCommand -Receipt ([ref]$grafanaReceipt) -Command { py "E:\Projects\Tools\TimeAudit\backup_grafana.py" }
$grafanaStatus = if ($grafanaExit -eq 0) { 'pass' } else { 'failed' }
if ($grafanaExit -ne 0) {
    "[backup-all] Grafana 备份失败，exit=$grafanaExit" | Out-File $log -Append -Encoding utf8
    if ($exitCode -eq 0) { $exitCode = $grafanaExit }
}

$receipt = [ordered]@{
    schema = 'timeaudit.daily-backup-receipt.v1'
    database_backup = [ordered]@{ status = $dbStatus; exit_code = $dbExit; failure = $(if($dbExit -ne 0){$databaseReceipt}else{$null}); result = $databaseReceipt }
    dashboard_configuration = [ordered]@{ status = $grafanaStatus; exit_code = $grafanaExit; result = $grafanaReceipt }
    file_warnings = @(@($databaseReceipt.file_warnings) + @($grafanaReceipt.file_warnings) | Where-Object { $null -ne $_ })
    overall_status = if ($exitCode -eq 0) { 'pass' } else { 'failed' }
}
($receipt | ConvertTo-Json -Compress -Depth 4) | Out-File $log -Append -Encoding utf8
"[backup-all] done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $log -Append -Encoding utf8
# Keep the last-run receipt independent of child diagnostics and backup exit status.
$localStatus = if ($receipt.file_warnings.Count -gt 0 -or $grafanaReceipt.retained_previous_backup -or $grafanaReceipt.local_snapshot_status -eq 'retained_previous') { 'retained_previous' } else { 'complete' }
if ($dbExit -ne 0 -or ($grafanaExit -ne 0 -and $grafanaReceipt.local_snapshot_status -notin @('complete','retained_previous'))) { $localStatus = 'failed' }
$cloudStatus = if ($grafanaReceipt.cloud_sync_status -in @('complete','failed')) { $grafanaReceipt.cloud_sync_status } elseif ($grafanaExit -eq 0 -and -not $grafanaReceipt.retained_previous_backup) { 'complete' } else { $null }
$databaseReasons = @(
    'backup_command_failed','backup_command_timed_out','backup_command_unavailable','docker_unavailable',
    'container_image_identity_invalid','archive_catalog_too_large','archive_missing_required_table','archive_too_small','archive_header_invalid',
    'archive_changed_during_verification','archive_manifest_too_large','archive_manifest_invalid','archive_manifest_mismatch',
    'backup_transaction_path_invalid','backup_transaction_invalid','backup_transaction_file_identity_unavailable','backup_transaction_manifest_archive_conflict',
    'backup_transaction_foreign_collision','backup_already_running','backup_candidate_collision','PermissionError','OSError','ValueError','FileNotFoundError','FileExistsError','NotADirectoryError','IsADirectoryError','JSONDecodeError'
)
$reason = if ($dbExit -ne 0 -and $databaseReceipt.reason -in $databaseReasons) { $databaseReceipt.reason } elseif ($grafanaReceipt.reason -eq 'git_backup_failed') { 'git_backup_failed' } else { $null }
$summary = switch ($localStatus) {
    'complete' { '本地快照已完成' }
    'retained_previous' { '此前本地快照已保留' }
    default { '本地快照失败' }
}
$summary += if ($cloudStatus -eq 'complete') { '、云端同步已完成' } elseif ($cloudStatus -eq 'failed') { '、云端推送失败' } else { '、云端同步未尝试或无结果' }
$runReceipt = [ordered]@{
    schema = 'timeaudit.backup-run.v1'
    started_at = $startedAt
    completed_at = [DateTimeOffset]::Now.ToString('o')
    local_snapshot_status = $localStatus
    cloud_sync_status = $cloudStatus
    reason = $reason
    summary = $summary
    exit_code = $exitCode
}
Write-RunReceipt $runReceipt
exit $exitCode
