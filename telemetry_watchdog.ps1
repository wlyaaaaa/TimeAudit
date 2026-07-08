# TimeAudit telemetry watchdog
# Restarts a collector if its process has crashed/disappeared. Guards TWO engines:
#   1. main.py            -> the Python hardware/process telemetry engine (writes fact_* tables)
#   2. TimeAudit.ahk      -> the AutoHotkey foreground/macro-state engine (feeds app_usage_logs,
#                            i.e. the 屏幕使用时间 dashboard). Previously UNGUARDED: if it died,
#                            the entire screen-time data source went silent with nobody to revive it.
# Guards ONLY process existence (not data lag) on purpose: during system sleep/idle both engines
# are merely suspended (process still exists) and intentionally pause writes, so a lag-based check
# would false-restart on every wake. A missing process is an unambiguous crash.
$ErrorActionPreference = 'SilentlyContinue'
$log       = 'E:\Projects\Tools\TimeAudit\telemetry_watchdog.log'
$py        = 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe'
$script    = 'E:\Projects\Tools\TimeAudit\main.py'
$ahkExe    = 'C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe'
$ahkScript = 'E:\Projects\Tools\TimeAudit\TimeAudit.ahk'

function Log($m){ "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Out-File -FilePath $log -Append -Encoding utf8 }

# Find a running process whose CommandLine matches $cmdMatch (regex).
function Find-Proc($exeName, $cmdMatch) {
    Get-CimInstance Win32_Process -Filter ("Name='{0}'" -f $exeName) |
        Where-Object { $_.CommandLine -match $cmdMatch } | Select-Object -First 1
}

function Remove-StaleTask($taskName) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -ne 'Running') {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}

Remove-StaleTask 'TimeAudit_WatchdogRestart_tmp'
Remove-StaleTask 'TimeAudit_WatchdogAhkRestart_tmp'

# Restart a target via a one-shot elevated, interactive-session scheduled task. This is the same
# method proven to bring up main.py / LHM / PresentMon with proper elevation AND inside the user's
# interactive desktop session (required so the AHK engine can read foreground window titles).
function Restart-ViaTask($taskName, $exe, $arg, $workDir) {
    if (-not (Test-Path $exe)) { Log ("ABORT: exe missing -> {0}" -f $exe); return }
    $exeLiteral = "'" + ($exe -replace "'", "''") + "'"
    $argLiteral = "'" + ($arg -replace "'", "''") + "'"
    $workDirLiteral = "'" + ($workDir -replace "'", "''") + "'"
    $launcher = "Start-Process -FilePath $exeLiteral -ArgumentList $argLiteral -WorkingDirectory $workDirLiteral -WindowStyle Hidden"
    $encodedLauncher = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launcher))
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {0}" -f $encodedLauncher) -WorkingDirectory $workDir
    $pr  = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Highest -LogonType Interactive
    Register-ScheduledTask -TaskName $taskName -Action $act -Principal $pr -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 6
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# === 1. main.py (Python telemetry engine) ===
if (Find-Proc 'pythonw.exe' 'main\.py') {
    # healthy
} else {
    Log "main.py NOT running - attempting restart"
    if (-not (Test-Path $script)) { Log "ABORT: main.py path missing" }
    else {
        Restart-ViaTask 'TimeAudit_WatchdogRestart_tmp' $py ('"{0}"' -f $script) 'E:\Projects\Tools\TimeAudit'
        $now = Find-Proc 'pythonw.exe' 'main\.py'
        if ($now) { Log ("RESTART OK - main.py PID {0}" -f $now.ProcessId) }
        else      { Log "RESTART FAILED - main.py still down" }
    }
}

# === 2. TimeAudit.ahk (AutoHotkey screen-time engine) ===
# #SingleInstance Force inside the script makes a relaunch idempotent: any stale instance is
# silently replaced and flushes its pending buffer via SafeExitHandler, so a race cannot duplicate data.
if (Find-Proc 'AutoHotkey64.exe' 'TimeAudit\.ahk') {
    # healthy
} else {
    Log "TimeAudit.ahk NOT running - attempting restart"
    if (-not (Test-Path $ahkScript)) { Log "ABORT: TimeAudit.ahk path missing" }
    else {
        Restart-ViaTask 'TimeAudit_WatchdogAhkRestart_tmp' $ahkExe ('"{0}"' -f $ahkScript) 'E:\Projects\Tools\TimeAudit'
        $now = Find-Proc 'AutoHotkey64.exe' 'TimeAudit\.ahk'
        if ($now) { Log ("RESTART OK - TimeAudit.ahk PID {0}" -f $now.ProcessId) }
        else      { Log "RESTART FAILED - TimeAudit.ahk still down" }
    }
}

exit 0
