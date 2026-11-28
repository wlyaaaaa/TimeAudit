# TimeAudit telemetry watchdog
# Repairs these collectors/sources, restoring Docker before the database sidecar:
#   1. main.py            -> the Python hardware/process telemetry engine (writes fact_* tables)
#   2. LibreHardwareMonitor -> CPU temperature/clock/power/voltage source; endpoint health is authoritative.
#   3. TimeAudit.ahk      -> the AutoHotkey foreground/macro-state engine (feeds app_usage_logs,
#                            i.e. the 屏幕使用时间 dashboard).
#   4. audit-ingester     -> the bounded CSV-to-PostgreSQL sidecar.
# main.py also writes a local heartbeat only after a successful database batch. If that heartbeat is
# stale, the watchdog waits through a resume/startup grace period and checks again before relaunching.
# This catches a live-but-stuck collector without false-restarting immediately after system sleep.
$ErrorActionPreference = 'SilentlyContinue'
$watchdogStartedAt = [DateTimeOffset]::Now
$log       = 'E:\Projects\Tools\TimeAudit\telemetry_watchdog.log'
$py        = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$pyConsole = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$script    = 'E:\Projects\Tools\TimeAudit\main.py'
$dbProbeScript = Join-Path $PSScriptRoot 'db_health_probe.py'
$ahkExe    = 'C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe'
$ahkScript = 'E:\Projects\Tools\TimeAudit\TimeAudit.ahk'
$lhmExe = 'E:\Projects\Tools\TimeAudit\LibreHardwareMonitor.exe'
$lhmTaskName = 'LibreHardwareMonitor'
$lhmEndpoint = 'http://127.0.0.1:18085/data.json'
$heartbeat = 'E:\Projects\Tools\TimeAudit\log\telemetry_heartbeat'
$backfillHeartbeat = 'E:\Projects\Tools\TimeAudit\log\telemetry_backfill_heartbeat'
$ahkHeartbeat = 'E:\Projects\Tools\TimeAudit\log\ahk_heartbeat'
$ingesterHeartbeat = 'E:\Projects\Tools\TimeAudit\log\ingester_heartbeat.json'
$docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$dockerRecoveryState = Join-Path $PSScriptRoot 'log\docker_engine_recovery.json'
$dockerHostLog = Join-Path $env:LOCALAPPDATA 'Docker\log\host\com.docker.backend.exe.log'
$compose = 'E:\Projects\Tools\TimeAudit\docker-compose.yml'
$grafanaHealthUrl = 'http://127.0.0.1:43000/api/health'
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

function Test-DatabaseQuery {
    # A port listener alone is insufficient: a PostgreSQL handshake can finish
    # while authentication or a simple query does not. The helper uses the same
    # local DSN as main.py, executes only SELECT 1, emits no payload, and has
    # its own bounded connect/query timeouts.
    if (-not (Test-Path -LiteralPath $pyConsole) -or -not (Test-Path -LiteralPath $dbProbeScript)) {
        return $false
    }
    try {
        $probe = Start-Process -FilePath $pyConsole -ArgumentList @('-B', $dbProbeScript) -WindowStyle Hidden -PassThru
        try { $probe.PriorityClass = [Diagnostics.ProcessPriorityClass]::AboveNormal } catch { }
        if (-not $probe.WaitForExit(8000)) {
            try { $probe.Kill() } catch { }
            return $false
        }
        return ($probe.ExitCode -eq 0)
    } catch {
        return $false
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

# Docker can disappear after successful logon bootstrap. Keep recovery in this
# serialized watchdog, before consumers. A sustained engine API outage permits
# one bounded Desktop restart and, if needed, one fallback.
function Invoke-DockerBounded([string]$Arguments, [int]$TimeoutSeconds = 8) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $docker
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'docker_start_failed' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill(); $process.WaitForExit()
            # A timed-out mutation has an unknown result. The next cycle must
            # inspect actual state; do not blindly replay it in this cycle.
            return @{ Success = $false; Output = ''; TimedOut = $true }
        }
        $output = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        return @{ Success = ($process.ExitCode -eq 0); Output = $output.Trim(); TimedOut = $false }
    } catch {
        return @{ Success = $false; Output = ''; TimedOut = $false }
    } finally { $process.Dispose() }
}

function Test-GrafanaEndpoint {
    try {
        $response = Invoke-RestMethod -Uri $grafanaHealthUrl -TimeoutSec 3 -ErrorAction Stop
        return $response.database -eq 'ok'
    } catch { return $false }
}

function Get-DockerDesktopState {
    $status = Invoke-DockerBounded 'desktop status --format json'
    if ($status.Success -and -not $status.TimedOut) {
        try {
            $value = ($status.Output | ConvertFrom-Json -ErrorAction Stop).Status
            if ($value -in @('stopped', 'running')) { return [string]$value }
        } catch { }
    }
    return 'unknown'
}

function Get-DockerLayerFault([long]$NowSeconds) {
    $rotated = Get-ChildItem -LiteralPath (Split-Path $dockerHostLog -Parent) `
        -Filter ((Split-Path $dockerHostLog -Leaf) + '.*') -File -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    $faults = foreach ($path in @($dockerHostLog, $rotated.FullName)) {
        if (-not $path) { continue }
        foreach ($line in (Get-Content -LiteralPath $path -Tail 640 -Encoding UTF8 -ErrorAction SilentlyContinue)) {
            if ($line -notmatch '^\[([^]]+)\]\[com\.docker\.backend\.exe\.engines\] engine linux/wsl shutdown requested .*: layer not mounted\)$') { continue }
            $at = [DateTimeOffset]::MinValue
            if ([DateTimeOffset]::TryParse($Matches[1], [ref]$at)) {
                $seconds = $at.ToUnixTimeSeconds()
                if ($seconds -le $NowSeconds -and $NowSeconds - $seconds -le 240) { $seconds }
            }
        }
    }
    return $faults | Sort-Object -Unique
}

function Restore-UnavailableDocker([string]$Status, [long]$NowSeconds = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())) {
    $unavailable = $Status -in @('stopped', 'running')
    $restartTimeout = 120
    $startupGrace = 180
    $state = @{ Version = 1; Since = 0L; Last = 0L; Attempted = $false; FallbackAfter = 0L; Incidents = @() }
    $hasState = Test-Path -LiteralPath $dockerRecoveryState
    if ($hasState) {
        try {
            $saved = Get-Content -LiteralPath $dockerRecoveryState -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($saved.Version -ne 1 -or $saved.Attempted -isnot [bool] -or $saved.Since -lt 0 -or $saved.Last -lt 0) { throw 'invalid recovery state' }
            $state = @{
                Version = 1; Since = [long]$saved.Since; Last = [long]$saved.Last
                Attempted = $saved.Attempted; FallbackAfter = [long]$saved.FallbackAfter
                Incidents = @($saved.Incidents | Where-Object { $_ -and $_.FailedAt -gt 0 } | Select-Object -Last 12)
            }
        } catch {
            if ($Status -eq 'healthy') {
                Remove-Item -LiteralPath $dockerRecoveryState -Force -ErrorAction SilentlyContinue
                return $false
            }
            Log 'Docker recovery state unreadable - defer automatic engine recovery'; return $true
        }
    }
    $lastIncident = $state.Incidents | Select-Object -Last 1
    $notes = @()
    foreach ($fault in (Get-DockerLayerFault $NowSeconds)) {
        if ($fault -gt [Math]::Max([long]$lastIncident.FailedAt, [long]$lastIncident.RecoveredAt)) {
            $lastIncident = @{ FailedAt = $fault; RecoveredAt = 0L; Reason = 'layer_not_mounted' }
            $state.Incidents = @($state.Incidents + $lastIncident | Select-Object -Last 12)
            $notes += "Docker Linux engine failed at $([DateTimeOffset]::FromUnixTimeSeconds($fault).ToString('o')) (layer not mounted)"
        }
    }
    if ($Status -eq 'healthy') {
        $openIncidents = @($state.Incidents | Where-Object { -not $_.RecoveredAt })
        if ($state.Last -eq 0 -and -not $state.Attempted -and -not $openIncidents) { return $false }
        foreach ($incident in $openIncidents) { $incident.RecoveredAt = $NowSeconds }
        if ($openIncidents) { $notes += "Docker engine API recovered at $([DateTimeOffset]::FromUnixTimeSeconds($NowSeconds).ToString('o'))" }
        $state.Attempted = $false; $state.FallbackAfter = 0L
    }
    if (-not $unavailable) {
        # Starting, stopping and unreadable status break continuity.
        $state.Since = 0L; $state.Last = 0L
    } else {
        if ($state.Since -eq 0 -or $NowSeconds -lt $state.Last -or $NowSeconds - $state.Last -gt 180) { $state.Since = $NowSeconds }
        $state.Last = $NowSeconds
    }
    $unavailableFor = $NowSeconds - $state.Since
    $confirmedFault = $Status -eq 'stopped' -and $lastIncident.Reason -eq 'layer_not_mounted' -and
        -not $lastIncident.RecoveredAt -and $NowSeconds - $lastIncident.FailedAt -le 240
    $restart = $unavailable -and -not $state.Attempted -and ($confirmedFault -or $unavailableFor -ge 600)
    $fallback = $state.Attempted -and $state.FallbackAfter -gt 0 -and $NowSeconds -ge $state.FallbackAfter -and
        ($Status -eq 'absent' -or ($unavailable -and $unavailableFor -ge $startupGrace))
    if ($fallback) {
        if ((Invoke-DockerBounded 'version --format {{.Server.Version}}').Success) {
            Log 'Docker API recovered during restart grace'
            $null = Restore-UnavailableDocker 'healthy'
            return $true
        }
        if ($Status -ne 'absent' -and (Get-DockerDesktopState) -notin @('running', 'stopped')) {
            $fallback = $false
            $state.Since = 0L; $state.Last = 0L
        }
    }
    if ($restart -or $fallback) {
        $autostart = Get-ScheduledTask -TaskName 'TimeAudit_AutoStart' -ErrorAction SilentlyContinue
        if (-not $autostart -or $autostart.State -in @('Running', 'Disabled')) {
            Log 'Docker recovery deferred - registered autostart unavailable, disabled or already running'
            $restart = $false; $fallback = $false
        } else {
            $state.Attempted = $true
            $state.FallbackAfter = 0L
            if ($restart) {
                $state.FallbackAfter = $NowSeconds + $restartTimeout + $startupGrace
                if (-not $lastIncident -or $lastIncident.RecoveredAt) {
                    $lastIncident = @{ FailedAt = $state.Since; RecoveredAt = 0L; Reason = 'api_unavailable' }
                    $state.Incidents = @($state.Incidents + $lastIncident | Select-Object -Last 12)
                }
            }
        }
    }
    if ($Status -eq 'healthy' -and -not $state.Incidents) {
        Remove-Item -LiteralPath $dockerRecoveryState -Force -ErrorAction SilentlyContinue
        return $false
    }
    if ($Status -in @('unknown', 'absent') -and -not $state.Incidents -and -not $hasState) { return $false }
    $temporary = $dockerRecoveryState + '.' + $PID + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($state | ConvertTo-Json -Depth 4 -Compress), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $dockerRecoveryState -Force -ErrorAction Stop
    } catch { Log 'Docker recovery state not persisted - no engine mutation'; return $true }
    finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    foreach ($note in $notes) { Log $note }
    if ($restart) {
        Log "Docker engine unavailable (desktop_status=$Status, confirmed_layer_fault=$confirmedFault, observed_seconds=$unavailableFor) - one bounded official restart"
        $result = Invoke-DockerBounded 'desktop restart --timeout 110' $restartTimeout
        Log "Docker Desktop restart returned success=$($result.Success), timed_out=$($result.TimedOut) - verifying API in later cycles"
        return $true
    }
    if (-not $fallback) { return $state.Attempted }
    $installRoot = [IO.Path]::GetFullPath((Join-Path (Split-Path $docker -Parent) '..\..')).TrimEnd('\') + '\'
    $ownedNames = @('Docker Desktop.exe', 'com.docker.backend.exe', 'com.docker.build.exe', 'docker-desktop.exe')
    $filter = ($ownedNames | ForEach-Object { "Name='$_'" }) -join ' OR '
    try {
        $candidates = if ($Status -ne 'absent') { Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop }
        foreach ($candidate in $candidates) {
            if ($candidate.Name -notin $ownedNames -or [string]::IsNullOrWhiteSpace($candidate.ExecutablePath)) { continue }
            $process = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
            if (-not $process) { continue }
            try {
                $null = $process.Handle  # Keep this identity alive while checking and stopping it.
                if ($process.HasExited -or ($process.ProcessName + '.exe') -notin $ownedNames -or [string]::IsNullOrWhiteSpace($process.Path)) { continue }
                $path = [IO.Path]::GetFullPath($process.Path)
                if (-not $path.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase) -or
                    -not [string]::Equals($path, [IO.Path]::GetFullPath($candidate.ExecutablePath), [StringComparison]::OrdinalIgnoreCase)) { continue }
                Log ("Docker Desktop fallback stopping owned PID {0}: {1}" -f $candidate.ProcessId, $path)
                Stop-Process -InputObject $process -Force -ErrorAction Stop
            } catch { $process.Refresh(); if (-not $process.HasExited) { throw } }
            finally { $process.Dispose() }
        }
        Start-ScheduledTask -TaskName 'TimeAudit_AutoStart' -ErrorAction Stop
        Log 'Docker Desktop fallback invoked registered autostart - next cycle verifies readiness'
    } catch { Log ("Docker Desktop fallback failed; attempt remains latched: {0}" -f $_.Exception.Message) }
    return $true
}

function Restore-TimeAuditDependencies {
    $probeTimer = [Diagnostics.Stopwatch]::StartNew()
    $engine = Invoke-DockerBounded 'version --format {{.Server.Version}}'
    $probeTimer.Stop()
    if (-not $engine.Success) {
        $desktop = Get-Process -Name 'Docker Desktop','com.docker.backend' -ErrorAction SilentlyContinue
        $desktopState = 'absent'
        if ($desktop) {
            $desktopState = Get-DockerDesktopState
        }
        $recoveryBlocked = Restore-UnavailableDocker $desktopState
        if ($engine.TimedOut) {
            Log ("Docker API probe timed out after {0:F2}s; desktop_status={1}" -f $probeTimer.Elapsed.TotalSeconds, $desktopState)
            return $false
        }
        if (-not $desktop) {
            if ($recoveryBlocked) { Log 'Docker remains unavailable after its one recovery attempt - no repeated bootstrap'; return $false }
            $autostart = Get-ScheduledTask -TaskName 'TimeAudit_AutoStart' -ErrorAction SilentlyContinue
            if ($autostart -and $autostart.State -notin @('Running', 'Disabled')) {
                Log 'Docker Desktop absent - invoking registered autostart; next cycle verifies readiness'
                Start-ScheduledTask -TaskName 'TimeAudit_AutoStart' -ErrorAction Stop
            } else {
                Log 'Docker status probe failed - autostart already running or not registered'
            }
        } else {
            Log "Docker API unavailable; desktop_status=$desktopState"
        }
        return $false
    }
    $null = Restore-UnavailableDocker 'healthy'
    $grafanaStartAttempted = $false
    foreach ($name in 'audit-postgres','audit-grafana') {
        $state = Invoke-DockerBounded ('inspect --format {{.State.Status}} ' + $name)
        if (-not $state.Success) {
            Log ("Dependency {0} state unavailable - preserve data and defer" -f $name)
            continue
        }
        if ($state.Output -in @('created','exited')) {
            if ($name -eq 'audit-grafana') { $grafanaStartAttempted = $true }
            Log ("Dependency {0} stopped - starting existing container" -f $name)
            $result = Invoke-DockerBounded ('start ' + $name) 30
            if (-not $result.Success) { Log ("Dependency {0} start not confirmed; next cycle inspects state" -f $name) }
        }
    }
    if ($grafanaStartAttempted) { return $true }
    if (-not (Test-GrafanaEndpoint)) {
        $started = Invoke-DockerBounded 'inspect --format {{.State.StartedAt}} audit-grafana'
        $startedAt = [DateTimeOffset]::MinValue
        if (-not $started.Success -or -not [DateTimeOffset]::TryParse($started.Output, [ref]$startedAt) -or
            ([DateTimeOffset]::UtcNow - $startedAt).TotalSeconds -lt 120) { return $true }
        Start-Sleep -Seconds $sidecarGraceSeconds
        if (-not (Test-GrafanaEndpoint)) {
            $state = Invoke-DockerBounded 'inspect --format {{.State.Status}} audit-grafana'
            if ($state.Success -and $state.Output -eq 'running') {
                Log 'Grafana health unavailable after startup grace - restarting only audit-grafana'
                $result = Invoke-DockerBounded 'restart audit-grafana' 30
                if (-not $result.Success) { Log 'Grafana restart not confirmed; next cycle inspects state' }
            }
        }
    }
    return $true
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
        if (Test-AutoStartWithinGrace) {
            Log ("main.py not running yet, but TimeAudit_AutoStart is within its {0}s startup window - defer this cycle" -f $autoStartMaxGraceSeconds)
        } else {
            Restart-Main 'NOT running after startup grace period'
        }
    }
} elseif (-not (Test-HeartbeatFresh $heartbeat $heartbeatMaxAgeSeconds)) {
    if (Test-HeartbeatFresh $backfillHeartbeat $heartbeatMaxAgeSeconds) {
        Log 'The collector is committing historical samples - defer main.py restart'
    } elseif (-not (Test-DatabaseQuery)) {
        Log ("main.py heartbeat stale but PostgreSQL endpoint {0}:{1} is unavailable - defer restart" -f $dbHost, $dbHostPort)
    } elseif (Test-MainWithinStartupGrace $mainProc) {
        Log ("main.py heartbeat stale but live process is within its {0}s startup grace - defer" -f $mainStartupHeartbeatGraceSeconds)
    } else {
        Log ("main.py heartbeat stale - waiting {0}s for startup/resume recovery" -f $heartbeatGraceSeconds)
        Start-Sleep -Seconds $heartbeatGraceSeconds
        $mainProc = Find-MainProc
        if (-not $mainProc) {
            Restart-Main 'stopped during heartbeat grace period'
        } elseif (-not (Test-HeartbeatFresh $heartbeat $heartbeatMaxAgeSeconds)) {
            if (Test-DatabaseQuery) {
                Restart-Main 'heartbeat stale after grace period'
            } else {
                Log ("main.py heartbeat remains stale but PostgreSQL endpoint {0}:{1} is unavailable - defer restart" -f $dbHost, $dbHostPort)
            }
        }
    }
}

# === 2. LibreHardwareMonitor (hardware sensor source) ===
$dockerReady = Restore-TimeAuditDependencies
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
    $state = Invoke-DockerBounded 'inspect --format {{.State.Running}} audit-ingester'
    return ($state.Success -and $state.Output -eq 'true')
}

function Restart-Ingester($reason) {
    Log ("audit-ingester {0} - attempting restart" -f $reason)
    if (-not (Test-Path -LiteralPath $docker)) {
        Log "ABORT: Docker CLI missing"
        return
    }
    $result = Invoke-DockerBounded 'restart audit-ingester' 30
    if (-not $result.Success -and -not $result.TimedOut) {
        $result = Invoke-DockerBounded ('compose -f "{0}" up -d audit-ingest' -f $compose) 30
    }
    if ($result.TimedOut) { Log 'audit-ingester recovery timed out - next cycle inspects state'; return }
    Start-Sleep -Seconds 12
    if ((Test-IngesterRunning) -and (Test-HeartbeatFresh $ingesterHeartbeat $ingesterHeartbeatMaxAgeSeconds)) {
        Log "RESTART OK - audit-ingester heartbeat fresh"
    } else {
        Log "RESTART FAILED - audit-ingester unavailable or heartbeat stale"
    }
}

if (-not $dockerReady) {
    Log 'audit-ingester recovery deferred until the Docker engine API responds'
} elseif (-not (Test-DatabaseEndpoint)) {
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

function Write-WatchdogOutcome {
    param([string]$State,[object]$Components,[string]$Reason='')
    if ($State -ne 'healthy') {
        $names = @($Components | Where-Object { $_ -cmatch '^[a-z][a-z_]{0,40}$' }) -join ','
        Log ('watchdog health: started={0:o} status={1} reason={2} components={3}' -f $watchdogStartedAt, $State, $Reason, $names)
    }
    $payload=[ordered]@{
        schema='timeaudit.watchdog-outcome.v1'
        checked_at_utc=[DateTimeOffset]::UtcNow.ToString('o')
        status=$State; reason=$Reason; degraded_components=@($Components)
    }
    $target=Join-Path $PSScriptRoot 'log\watchdog_outcome.json'
    $temporary=$target+'.'+$PID+'.tmp'
    try {
        [IO.File]::WriteAllText($temporary,($payload|ConvertTo-Json -Depth 5 -Compress),[Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $target -Force -ErrorAction Stop
    } finally { if(Test-Path $temporary){Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue} }
}

function Confirm-WatchdogHealth {
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$pyConsole
    $start.Arguments='-B "'+(Join-Path $PSScriptRoot 'timeaudit_health.py')+'" --core-only'
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process;$process.StartInfo=$start
    $failureReason = 'health_start_failed'
    try {
        if(-not $process.Start()){throw 'health_start_failed'}
        $out=$process.StandardOutput.ReadToEndAsync();$err=$process.StandardError.ReadToEndAsync()
        # Include Windows Python startup in addition to the bounded component probes.
        $failureReason = 'health_timeout'
        if(-not $process.WaitForExit(20000)){$process.Kill();$process.WaitForExit();throw 'health_timeout'}
        $failureReason = 'health_output_invalid'
        $text=$out.GetAwaiter().GetResult();$null=$err.GetAwaiter().GetResult()
        if($text.Length -gt 65536 -or $process.ExitCode -notin @(0,2)){throw 'health_output_invalid'}
        $failureReason = 'health_contract_invalid'
        $health=$text|ConvertFrom-Json
        if($health.schema -ne 'timeaudit.runtime-health.v1' -or $health.status -notin @('healthy','degraded')){throw 'health_contract_invalid'}
        $failureReason = 'health_receipt_write_failed'
        Write-WatchdogOutcome $health.status $health.degraded_components
        return $health.status -eq 'healthy'
    }catch{
        Write-WatchdogOutcome 'unavailable' @() $failureReason
        return $false
    }finally{$process.Dispose()}
}

# Public power summary for the PCConfig cockpit: same method as the Grafana panel
# "今日累计能耗" (CPU package + GPU board watts only, gaps >= 60 s dropped). Read-only
# psql inside the existing container, at most every 10 minutes; no credentials.
$powerReceipt = 'E:\Projects\Tools\TimeAudit\log\power-today.json'
$powerSql = "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY; WITH raw AS (SELECT timestamp, (cpu_package_power + gpu_board_power) AS w, COALESCE(EXTRACT(EPOCH FROM (timestamp - LAG(timestamp) OVER (ORDER BY timestamp))), 3.0) AS dt FROM public.fact_system_hardware WHERE timestamp >= (date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') - interval '1 day') AT TIME ZONE 'Asia/Shanghai' AND timestamp <= now()) SELECT (timestamp AT TIME ZONE 'Asia/Shanghai')::date, ROUND(SUM(CASE WHEN dt < 60 THEN w * dt / 3600000.0 ELSE 0 END)::numeric, 3), ROUND(MAX(w)::numeric, 0), COUNT(*) FROM raw GROUP BY 1 ORDER BY 1"
function Write-PowerToday {
    $existing = Get-Item -LiteralPath $powerReceipt -ErrorAction SilentlyContinue
    if ($existing -and ((Get-Date) - $existing.LastWriteTime).TotalSeconds -lt 600) { return }
    $result = Invoke-DockerBounded ('exec audit-postgres psql -U leyang -d time_audit -At -F "|" -v ON_ERROR_STOP=1 -c "' + $powerSql + '"') 15
    if (-not $result.Success) { return }
    $days = @(foreach ($line in ($result.Output -split "`r?`n")) {
        $parts = $line.Trim() -split '\|'
        if ($parts.Count -eq 4) {
            [ordered]@{ date=$parts[0]; energy_kwh=[double]$parts[1]; peak_watts=[int][double]$parts[2]; samples=[int]$parts[3] }
        }
    })
    $payload = [ordered]@{ schema='timeaudit.power-today.v1'; written_at=[DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString('o'); days=$days }
    $temporary = $powerReceipt + '.' + $PID + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($payload | ConvertTo-Json -Depth 4 -Compress), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $powerReceipt -Force -ErrorAction Stop
    } finally { if (Test-Path $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue } }
}

function Start-TimeAuditSnapshotProcess([string[]]$arguments) {
    $launcher = [System.Diagnostics.Process]::GetCurrentProcess()
    $restorePriority = $false
    try {
        $previousPriority = $launcher.PriorityClass
        $restorePriority = $previousPriority -in @('Idle', 'BelowNormal')
        # The venv launcher may spawn the real interpreter before Start-Process returns.
        if ($restorePriority) { $launcher.PriorityClass = 'Normal' }
        Start-Process -FilePath $pyConsole -ArgumentList $arguments -WindowStyle Hidden -PassThru -ErrorAction Stop
    } finally {
        try {
            if ($restorePriority) { $launcher.PriorityClass = $previousPriority }
        } finally { $launcher.Dispose() }
    }
}

function Write-TimeAuditLiveSnapshot([string]$mutexName = 'Global\TimeAuditLiveSnapshotMutex') {
    $liveOutput = Join-Path $PSScriptRoot 'log\live.snapshot.json'
    if (Test-HeartbeatFresh $liveOutput 120) { return }
    $mutex = $null
    $locked = $false
    $liveProcess = $null
    try {
        $mutex = [System.Threading.Mutex]::new($false, $mutexName)
        try { $locked = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked -or (Test-HeartbeatFresh $liveOutput 120)) { return }
        Log 'live snapshot export started'
        $liveProcess = Start-TimeAuditSnapshotProcess @('-B', ('"'+(Join-Path $PSScriptRoot 'timeaudit_diagnostic_summary.py')+'"'), '--hours', '24', '--live-output', ('"'+$liveOutput+'"'))
        if (-not $liveProcess.WaitForExit(35000)) {
            $liveProcess.Kill()
            $liveProcess.WaitForExit()
            Log 'live snapshot export timed out'
        } elseif ($liveProcess.ExitCode -eq 0 -and (Test-HeartbeatFresh $liveOutput 120)) {
            Log 'live snapshot file updated'
        } else {
            Log ('live snapshot file not updated; export exit={0}' -f $liveProcess.ExitCode)
        }
    } catch {
        Log ('live snapshot export failed: {0}' -f $_.Exception.GetType().Name)
    } finally {
        if ($liveProcess) { $liveProcess.Dispose() }
        if ($locked) { try { $mutex.ReleaseMutex() } catch { } }
        if ($mutex) { $mutex.Dispose() }
    }
}

function Invoke-TimeAuditWatchdogCycle([string]$watchdogMutexName) {
    $watchdogMutex = $null
    $watchdogLockAcquired = $false
    $watchdogExitCode = 0
    try {
        $watchdogMutex = [System.Threading.Mutex]::new($false, $watchdogMutexName)
        try { $watchdogLockAcquired = $watchdogMutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $watchdogLockAcquired = $true }
        if ($watchdogLockAcquired) {
            $null = Invoke-TimeAuditWatchdog
            if (-not (Confirm-WatchdogHealth)) { $watchdogExitCode = 2 }
            try { $null = Write-PowerToday } catch { }
        } else {
            Log 'watchdog invocation skipped because a live owner holds the recovery mutex'
        }
    } catch {
        $watchdogExitCode = 2
        if ($watchdogLockAcquired) { Write-WatchdogOutcome 'unavailable' @() 'recovery_invocation_failed' }
    } finally {
        if ($watchdogLockAcquired -and $watchdogMutex) {
            try { $watchdogMutex.ReleaseMutex() } catch { }
        }
        if ($watchdogMutex) { $watchdogMutex.Dispose() }
    }
    # A skipped or failed recovery must not prevent this bounded display-only writer.
    $null = Write-TimeAuditLiveSnapshot
    return $watchdogExitCode
}

exit (Invoke-TimeAuditWatchdogCycle 'Global\TimeAuditTelemetryWatchdogMutex')
