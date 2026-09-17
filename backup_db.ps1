#requires -Version 5.1
# Atomic export and verification are owned by timeaudit_backup.py.
param(
    [ValidateRange(1,3650)][int]$RetentionDays=14,
    [string]$BackupDir='G:\80_Backup\TimeAudit\postgresql',
    [string]$Container='audit-postgres',
    [string]$DbUser='leyang',
    [string]$DbName='time_audit'
)
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -B (Join-Path $PSScriptRoot 'timeaudit_backup.py') backup --retention-days $RetentionDays --backup-dir $BackupDir --container $Container --db-user $DbUser --db-name $DbName
exit $LASTEXITCODE