# TimeAudit telemetry watchdog
# Restarts a collector if its process has crashed/disappeared. Guards FOUR engines:
#   1. main.py            -> the Python hardware/process telemetry engine (writes fact_* tables)
#   2. LibreHardwareMonitor -> CPU temperature/clock/power/voltage source; endpoint health is authoritative.
#   3. TimeAudit.ahk      -> the AutoHotkey foreground/macro-state engine (feeds app_usage_logs,
#                            i.e. the 屏幕使用时间 dashboard).
#   4. audit-ingester     -> the bounded CSV-to-PostgreSQL sidecar.
# main.py also writes a local heartbeat only after a successful database batch. If that heartbeat is
# stale, the watchdog waits through a resume/startup grace period and checks again before relaunching.
# This catches a live-but-stuck collector without false-restarting immediately after system sleep.
$ErrorActionPreference = 'SilentlyContinue'
$log       = 'E:\Projects\Tools\TimeAudit\telemetry_watchdog.log'
$py        = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script    = 'E:\Projects\Tools\TimeAudit\main.py'
$ahkExe    = 'C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe'
$ahkScript = 'E:\Projects\Tools\TimeAudit\TimeAudit.ahk'
$lhmExe = 'E:\Projects\Tools\TimeAudit\LibreHardwareMonitor.exe'
$lhmTaskName = 'LibreHardwareMonitor'
$lhmEndpoint = 'http://127.0.0.1:18085/data.json'
$heartbeat = 'E:\Projects\Tools\TimeAudit\log\telemetry_heartbeat'
$ahkHeartbeat = 'E:\Projects\Tools\TimeAudit\log\ahk_heartbeat'
$ingesterHeartbeat = 'E:\Projects\Tools\TimeAudit\log\ingester_heartbeat.json'
$docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$compose = 'E:\Projects\Tools\TimeAudit\docker-compose.yml'
$dbHost = '127.0.0.1'
$dbHostPort = 45432
$configuredDbHostPort = 0
if (
    [int]::TryParse($env:TIMEAUDIT_DB_HOST_PORT, [ref]$configuredDbHostPort) -and
    $configuredDbHostPort -ge 1 -and
    $configuredDbHostPort -le 65535
) {
    $dbHostPort = $configuredDbHostPort
}
$dbProbeTimeoutMilliseconds = 1000
$heartbeatMaxAgeSeconds = 90
$heartbeatGraceSeconds = 45
$lhmGraceSeconds = 15
$lhmProbeTimeoutSeconds = 3
$lhmStartupTimeoutSeconds = 20
$ahkHeartbeatMaxAgeSeconds = 20
$ingesterHeartbeatMaxAgeSeconds = 45
$sidecarGraceSeconds = 15
$startupGraceSeconds = 15
$autoStartMaxGraceSeconds = 240
# Cold bootstrap registers every live PID before the first 1 Hz hardware row.
# This is deliberately bounded: an old, live-but-stuck collector is still
# restarted after this grace plus the normal stale confirmation window.
$mainStartupHeartbeatGraceSeconds = 180
$mainScriptPattern = '(?i)(?:^|\s|")' + [regex]::Escape($script) + '(?:"|\s|$)'
$ahkScriptPattern = '(?i)(?:^|\s|")' + [regex]::Escape($ahkScript) + '(?:"|\s|$)'

function Log($m){ "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Out-File -FilePath $log -Append -Encoding utf8 }

# Find a running process whose CommandLine matches $cmdMatch (regex).
function Find-Proc($exeName, $cmdMatch) {
    Get-CimInstance Win32_Process -Filter ("Name='{0}'" -f $exeName) |
        Where-Object { $_.CommandLine -match $cmdMatch } | Select-Object -First 1
}

function Find-MainProc {
    foreach ($exeName in 'pythonw.exe','python.exe') {
        $process = Find-Proc $exeName $mainScriptPattern
        if ($process) { return $process }
    }
}

function Test-HeartbeatFresh($path, $maxAgeSeconds) {
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $item) { return $false }
    $ageSeconds = ((Get-Date).ToUniversalTime() - $item.LastWriteTimeUtc).TotalSeconds
    return ($ageSeconds -le $maxAgeSeconds)
}

function Test-AutoStartWithinGrace {
    $task = Get-ScheduledTask -TaskName 'TimeAudit_AutoStart' -ErrorAction SilentlyContinue
    if (-not $task -or $task.State -ne 'Running') { return $false }
    $info = Get-ScheduledTaskInfo -TaskName 'TimeAudit_AutoStart' -ErrorAction SilentlyContinue
    if (-not $info) { return $false }
    $ageSeconds = ((Get-Date) - $info.LastRunTime).TotalSeconds
    return ($ageSeconds -ge 0 -and $ageSeconds -le $autoStartMaxGraceSeconds)
}

function Test-DatabaseEndpoint {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync($dbHost, $dbHostPort)
        if (-not $pending.Wait($dbProbeTimeoutMilliseconds)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-MainWithinStartupGrace($process) {
    if (-not $process -or -not $process.CreationDate) { return $false }
    try {
        $ageSeconds = ((Get-Date) - ([datetime]$process.CreationDate)).TotalSeconds
        return ($ageSeconds -ge 0 -and $ageSeconds -le $mainStartupHeartbeatGraceSeconds)
    } catch {
        return $false
    }
}

function Remove-StaleTask($taskName) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -ne 'Running') {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}

function Invoke-TimeAuditWatchdog {
Remove-StaleTask 'TimeAudit_WatchdogRestart_tmp'
Remove-StaleTask 'TimeAudit_WatchdogAhkRestart_tmp'
Remove-StaleTask 'TimeAudit_WatchdogLhmRestart_tmp'

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

function Test-LhmEndpoint {
    try {
        $response = Invoke-WebRequest -Uri $lhmEndpoint -UseBasicParsing -TimeoutSec $lhmProbeTimeoutSeconds -ErrorAction Stop
        if ($response.StatusCode -ne 200) { return $false }
        $payload = $response.Content | ConvertFrom-Json -ErrorAction Stop
        return ($null -ne $payload -and $payload.PSObject.Properties.Name -contains 'Children')
    } catch {
        return $false
    }
}

# A crashed WinForms/.NET process can remain visible briefly with zero handles and zero threads.
# Such a crash ghost must never suppress recovery merely because its image name still exists.
function Get-LhmLiveProjectProcesses {
    $expectedPath = [IO.Path]::GetFullPath($lhmExe)
    $matches = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name='LibreHardwareMonitor.exe'" -ErrorAction SilentlyContinue)) {
        $process = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
        $hasThreads = $process -and $process.Threads.Count -gt 0
        if (-not $hasThreads) { continue }
        if ([string]::IsNullOrWhiteSpace($candidate.ExecutablePath)) { continue }
        $candidatePath = [IO.Path]::GetFullPath($candidate.ExecutablePath)
        if ([string]::Equals($candidatePath, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            $matches += $candidate
        }
    }
    return $matches
}

function Wait-LhmEndpoint($timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    do {
        if (Test-LhmEndpoint) { return $true }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Restart-Lhm($reason) {
    Log ("LibreHardwareMonitor {0} - attempting endpoint recovery" -f $reason)
    if (-not (Test-Path -LiteralPath $lhmExe)) {
        Log "ABORT: LibreHardwareMonitor path missing"
        return
    }

    foreach ($process in @(Get-LhmLiveProjectProcesses)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2

    $task = Get-ScheduledTask -TaskName $lhmTaskName -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $lhmTaskName
    } else {
        Restart-ViaTask 'TimeAudit_WatchdogLhmRestart_tmp' $lhmExe '' 'E:\Projects\Tools\TimeAudit'
    }

    if (Wait-LhmEndpoint $lhmStartupTimeoutSeconds) {
        $process = @(Get-LhmLiveProjectProcesses) | Select-Object -First 1
        if ($process) { Log ("RECOVERY OK - LibreHardwareMonitor PID {0}, endpoint fresh" -f $process.ProcessId) }
        else { Log "RECOVERY OK - LibreHardwareMonitor endpoint fresh" }
    } else {
        Log "RECOVERY FAILED - LibreHardwareMonitor endpoint still unavailable"
    }
}

# === 1. main.py (Python telemetry engine) ===
function Stop-MainForRestart($reason) {
    # The collector's own singleton is deliberately passive: only this
    # serialized watchdog may replace a stale exact-script process.  This
    # prevents an orphaned watchdog child from making a newly launched
    # collector kill a healthy peer during startup.
    $deadline = (Get-Date).AddSeconds(8)
    do {
        $mainProc = Find-MainProc
        if (-not $mainProc) { return $true }

        Log ("main.py {0} - stopping existing PID {1} before replacement" -f $reason, $mainProc.ProcessId)
        try {
            Stop-Process -Id $mainProc.ProcessId -Force -ErrorAction Stop
        } catch {
            Log ("ABORT: could not stop existing main.py PID {0}: {1}" -f $mainProc.ProcessId, $_.Exception.Message)
            return $false
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    $remaining = Find-MainProc
    if ($remaining) {
        Log ("ABORT: main.py PID {0} survived replacement stop window" -f $remaining.ProcessId)
        return $false
    }
    return $true
}

function Restart-Main($reason) {
    Log ("main.py {0} - attempting restart" -f $reason)
    if (-not (Test-Path $script)) { Log "ABORT: main.py path missing" }
    else {
        if (-not (Stop-MainForRestart $reason)) { return }
        Restart-ViaTask 'TimeAudit_WatchdogRestart_tmp' $py ('"{0}"' -f $script) 'E:\Projects\Tools\TimeAudit'
        $now = Find-MainProc
        if ($now) { Log ("RESTART OK - main.py PID {0}" -f $now.ProcessId) }
        else      { Log "RESTART FAILED - main.py still down" }
    }
}

$mainProc = Find-MainProc
if (-not $mainProc) {
    Log ("main.py not running - waiting {0}s for startup race" -f $startupGraceSeconds)
    Start-Sleep -Seconds $startupGraceSeconds
    $mainProc = Find-MainProc
    if (-not $mainProc) {
        if (-not (Test-DatabaseEndpoint)) {
            Log ("main.py restart deferred because PostgreSQL endpoint {0}:{1} is unavailable" -f $dbHost, $dbHostPort)
        } elseif (Test-AutoStartWithinGrace) {
            Log ("main.py not running yet, but TimeAudit_AutoStart is within its {0}s startup window - defer this cycle" -f $autoStartMaxGraceSeconds)
        } else {
            Restart-Main 'NOT running after startup grace period'
        }
    }
} elseif (-not (Test-HeartbeatFresh $heartbeat $heartbeatMaxAgeSeconds)) {
    if (-not (Test-DatabaseEndpoint)) {
        Log ("main.py heartbeat stale but PostgreSQL endpoint {0}:{1} is unavailable - defer restart" -f $dbHost, $dbHostPort)
    } elseif (Test-MainWithinStartupGrace $mainProc) {
        Log ("main.py heartbeat stale but live process is within its {0}s startup grace - defer" -f $mainStartupHeartbeatGraceSeconds)
    } else {
        Log ("main.py heartbeat stale - waiting {0}s for startup/resume recovery" -f $heartbeatGraceSeconds)
        Start-Sleep -Seconds $heartbeatGraceSeconds
        $mainProc = Find-MainProc
        if (-not $mainProc) {
            if (Test-DatabaseEndpoint) {
                Restart-Main 'stopped during heartbeat grace period'
            } else {
                Log ("main.py stopped during heartbeat grace but PostgreSQL endpoint {0}:{1} is unavailable - defer restart" -f $dbHost, $dbHostPort)
            }
        } elseif (-not (Test-HeartbeatFresh $heartbeat $heartbeatMaxAgeSeconds)) {
            if (Test-DatabaseEndpoint) {
                Restart-Main 'heartbeat stale after grace period'
            } else {
                Log ("main.py heartbeat remains stale but PostgreSQL endpoint {0}:{1} is unavailable - defer restart" -f $dbHost, $dbHostPort)
            }
        }
    }
}

# === 2. LibreHardwareMonitor (hardware sensor source) ===
if (-not (Test-LhmEndpoint)) {
    Log ("LibreHardwareMonitor endpoint unavailable - waiting {0}s for startup/resume recovery" -f $lhmGraceSeconds)
    Start-Sleep -Seconds $lhmGraceSeconds
    if (-not (Test-LhmEndpoint)) {
        Restart-Lhm 'endpoint unavailable after grace period'
    }
}

# === 3. TimeAudit.ahk (AutoHotkey screen-time engine) ===
function Restart-Ahk($reason) {
    Log ("TimeAudit.ahk {0} - attempting restart" -f $reason)
    if (-not (Test-Path $ahkScript)) { Log "ABORT: TimeAudit.ahk path missing" }
    else {
        $existing = Find-Proc 'AutoHotkey64.exe' $ahkScriptPattern
        if ($existing) {
            Stop-Process -Id $existing.ProcessId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
        Restart-ViaTask 'TimeAudit_WatchdogAhkRestart_tmp' $ahkExe ('"{0}"' -f $ahkScript) 'E:\Projects\Tools\TimeAudit'
        $now = Find-Proc 'AutoHotkey64.exe' $ahkScriptPattern
        if ($now -and (Test-HeartbeatFresh $ahkHeartbeat $ahkHeartbeatMaxAgeSeconds)) {
            Log ("RESTART OK - TimeAudit.ahk PID {0}" -f $now.ProcessId)
        }
        else      { Log "RESTART FAILED - TimeAudit.ahk still down" }
    }
}

# #SingleInstance Force inside the script makes a relaunch idempotent. A fresh payload-free
# heartbeat additionally proves that the AHK message loop is making progress.
$ahkProc = Find-Proc 'AutoHotkey64.exe' $ahkScriptPattern
if (-not $ahkProc) {
    Restart-Ahk 'NOT running'
} elseif (-not (Test-HeartbeatFresh $ahkHeartbeat $ahkHeartbeatMaxAgeSeconds)) {
    Start-Sleep -Seconds $sidecarGraceSeconds
    if (-not (Test-HeartbeatFresh $ahkHeartbeat $ahkHeartbeatMaxAgeSeconds)) {
        Restart-Ahk 'heartbeat stale after resume grace'
    }
}

# === 4. audit-ingester (CSV -> PostgreSQL sidecar) ===
function Test-IngesterRunning {
    if (-not (Test-Path -LiteralPath $docker)) { return $false }
    $state = & $docker inspect --format '{{.State.Running}}' audit-ingester 2>$null
    return ($LASTEXITCODE -eq 0 -and "$state".Trim() -eq 'true')
}

function Restart-Ingester($reason) {
    Log ("audit-ingester {0} - attempting restart" -f $reason)
    if (-not (Test-Path -LiteralPath $docker)) {
        Log "ABORT: Docker CLI missing"
        return
    }
    & $docker restart audit-ingester 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & $docker compose -f $compose up -d audit-ingest 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 12
    if ((Test-IngesterRunning) -and (Test-HeartbeatFresh $ingesterHeartbeat $ingesterHeartbeatMaxAgeSeconds)) {
        Log "RESTART OK - audit-ingester heartbeat fresh"
    } else {
        Log "RESTART FAILED - audit-ingester unavailable or heartbeat stale"
    }
}

if (-not (Test-DatabaseEndpoint)) {
    Log ("audit-ingester recovery deferred because PostgreSQL endpoint {0}:{1} is unavailable" -f $dbHost, $dbHostPort)
} elseif (-not (Test-IngesterRunning)) {
    Restart-Ingester 'NOT running'
} elseif (-not (Test-HeartbeatFresh $ingesterHeartbeat $ingesterHeartbeatMaxAgeSeconds)) {
    Start-Sleep -Seconds $sidecarGraceSeconds
    if (-not (Test-HeartbeatFresh $ingesterHeartbeat $ingesterHeartbeatMaxAgeSeconds)) {
        Restart-Ingester 'heartbeat stale after resume grace'
    }
}

return
}

$watchdogMutexName = 'Global\TimeAuditTelemetryWatchdogMutex'
$watchdogMutex = $null
$watchdogLockAcquired = $false
try {
    $watchdogMutex = [System.Threading.Mutex]::new($false, $watchdogMutexName)
    try {
        $watchdogLockAcquired = $watchdogMutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        # The prior owner died; Windows grants this caller the abandoned lock.
        $watchdogLockAcquired = $true
    }

    if ($watchdogLockAcquired) {
        Invoke-TimeAuditWatchdog
    } else {
        Log 'watchdog invocation skipped because a live owner holds the recovery mutex'
    }
} finally {
    if ($watchdogLockAcquired -and $watchdogMutex) {
        try { $watchdogMutex.ReleaseMutex() } catch { }
    }
    if ($watchdogMutex) { $watchdogMutex.Dispose() }
}

exit 0
