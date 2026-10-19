$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'telemetry_watchdog.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors) { throw ($parseErrors.Message -join '; ') }
$definitions = $ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in @('Get-DockerDesktopState', 'Restore-StoppedDocker', 'Restore-TimeAuditDependencies')
}, $true)
# Exercise the production recovery decision function without touching services.
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('timeaudit-docker-recovery-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $temporaryRoot
$dockerRecoveryState = Join-Path $temporaryRoot 'state.json'
$docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$sidecarGraceSeconds = 15
function Log($message) { }
function Start-Sleep { param($Seconds) }
function Get-Process {
    param($Name, $Id, $ErrorAction)
    if ($PSBoundParameters.ContainsKey('Id')) {
        if ($Id -in $script:missingPids) { return }
        $candidate = $script:candidates | Where-Object {$_.ProcessId -eq $Id} | Select-Object -First 1
        if (-not $candidate) { return }
        $path = $candidate.ExecutablePath
        if ($script:replacementPaths.ContainsKey($Id)) { $path = $script:replacementPaths[$Id] }
        $process = [pscustomobject]@{Id=$Id; ProcessName=[IO.Path]::GetFileNameWithoutExtension($candidate.Name); Path=$path; HasExited=$false; Handle=1}
        $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
        $process | Add-Member -MemberType ScriptMethod -Name Dispose -Value { }
        return $process
    }
    if ($script:desktop) { @{Id=1} }
}
function Get-ScheduledTask { param($TaskName, $ErrorAction) @{State=$script:taskState} }
function Start-ScheduledTask { param($TaskName, $ErrorAction) $script:startedTasks.Add($TaskName) }
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) $script:candidates }
function Stop-Process {
    param($InputObject, [switch]$Force, $ErrorAction)
    if ($InputObject.Id -eq $script:exitDuringStop) { $InputObject.HasExited=$true; throw 'process exited naturally' }
    if ($InputObject.Id -eq $script:deniedPid) { throw 'access denied' }
    $script:killed.Add($InputObject.Id)
}
function Test-GrafanaEndpoint { $script:grafanaHealthy }
function Invoke-DockerBounded([string]$Arguments, [int]$TimeoutSeconds=8) {
    $script:commands.Add($Arguments)
    if ($Arguments.StartsWith('info')) { return @{Success=$script:engine; Output='version'; TimedOut=$script:engineTimeout} }
    if ($Arguments -eq 'desktop status --format json') {
        return @{Success=$script:statusSuccess; Output=$script:desktopStatus; TimedOut=$script:statusTimeout}
    }
    if ($Arguments.StartsWith('desktop restart')) { return @{Success=$script:restartOK; TimedOut=(-not $script:restartOK)} }
    if ($Arguments.StartsWith('inspect --format {{.State.Status}} ')) {
        $name = ($Arguments -split ' ')[-1]
        return @{Success=$script:states.ContainsKey($name); Output=$script:states[$name]}
    }
    if ($Arguments -match 'StartedAt') { return @{Success=$true; Output=$script:grafanaStarted} }
    return @{Success=(-not $script:mutationTimeout); Output=''; TimedOut=$script:mutationTimeout}
}
function Reset-Scenario {
    Remove-Item -LiteralPath $dockerRecoveryState -Force -ErrorAction SilentlyContinue
    $script:commands = [Collections.Generic.List[string]]::new()
    $script:startedTasks = [Collections.Generic.List[string]]::new()
    $script:killed = [Collections.Generic.List[int]]::new()
    $script:candidates = @()
    $script:missingPids=@(); $script:replacementPaths=@{}; $script:exitDuringStop=0; $script:deniedPid=0
    $script:desktopStatus='{"Status":"running"}'; $script:statusSuccess=$true; $script:statusTimeout=$false
    $script:restartOK=$true
    $script:engine=$true; $script:engineTimeout=$false; $script:desktop=$true; $script:taskState='Ready'
    $script:states=@{'audit-postgres'='running';'audit-grafana'='running'}
    $script:grafanaHealthy=$true; $script:mutationTimeout=$false
    $script:grafanaStarted=[DateTimeOffset]::UtcNow.AddMinutes(-10).ToString('o')
}
function Assert-Equal($actual, $expected, $label) {
    if (($actual -join '|') -ne ($expected -join '|')) { throw "$label : got $actual expected $expected" }
}
try {
Reset-Scenario
$script:engine=$false; $script:desktop=$false
Restore-TimeAuditDependencies
Assert-Equal $startedTasks @('TimeAudit_AutoStart') 'missing Desktop uses registered startup'
Assert-Equal $commands.Count 1 'missing engine cannot mutate containers'

Reset-Scenario
$script:engine=$false; $script:desktop=$false; $script:taskState='Running'
Restore-TimeAuditDependencies
Assert-Equal $startedTasks.Count 0 'ongoing autostart is not duplicated'

Reset-Scenario
$script:engine=$false
Restore-TimeAuditDependencies
Assert-Equal $startedTasks.Count 0 'unavailable existing Desktop is not restarted'

Reset-Scenario
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^(start|restart)'}) @() 'healthy dependencies untouched'

Reset-Scenario
$script:states=@{'audit-postgres'='exited';'audit-grafana'='exited'}
$script:grafanaHealthy=$false; $script:mutationTimeout=$true
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^(start|restart)'}) @('start audit-postgres','start audit-grafana') 'only existing stopped dependencies started; unknown result not replayed'

Reset-Scenario
$script:grafanaHealthy=$false
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^restart'}) @('restart audit-grafana') 'only unhealthy Grafana restarted'

Reset-Scenario
$script:grafanaHealthy=$false; $script:grafanaStarted=[DateTimeOffset]::UtcNow.ToString('o')
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^restart'}) @() 'fresh Grafana receives startup grace'

Reset-Scenario
$script:states=@{}
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^(start|restart)'}) @() 'unknown or missing containers never recreated'
Reset-Scenario
$script:engine=$false
0..9 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (1000000 + $_ * 60) }
$null=Restore-StoppedDocker 'stopped' 1000599
Assert-Equal $commands.Count 0 'no restart before ten continuous minutes'
$null=Restore-StoppedDocker 'stopped' 1000600
Assert-Equal $commands @('desktop restart --timeout 110') 'official restart at threshold'
Assert-Equal $killed.Count 0 'successful official restart never terminates processes'
Assert-Equal $startedTasks.Count 0 'successful official restart never duplicates bootstrap'
$null=Restore-StoppedDocker 'stopped' 1000660
$null=Restore-StoppedDocker 'unknown' 1000720
$null=Restore-StoppedDocker 'stopped' 1000780
Assert-Equal $commands.Count 1 'attempt remains latched across subsequent stopped and unknown probes'
$null=Restore-StoppedDocker 'healthy'
Assert-Equal (Test-Path -LiteralPath $dockerRecoveryState) $false 'healthy API rearms a later incident'

foreach ($case in @(
    @{Output='{"Status":"stopped"}'; Success=$false; Timeout=$false},
    @{Output='{"Status":"stopped"}'; Success=$true; Timeout=$true},
    @{Output='not json'; Success=$true; Timeout=$false},
    @{Output='{"Status":"running"}'; Success=$true; Timeout=$false})) {
    Reset-Scenario
    $script:engine=$false
    $now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    @{Version=1;Since=$now-660;Last=$now-60;Attempted=$false} | ConvertTo-Json | Set-Content -LiteralPath $dockerRecoveryState
    $script:desktopStatus=$case.Output; $script:statusSuccess=$case.Success; $script:statusTimeout=$case.Timeout
    Restore-TimeAuditDependencies
    $saved=Get-Content -LiteralPath $dockerRecoveryState -Raw | ConvertFrom-Json
    Assert-Equal $saved.Since 0 'failed, timed out, malformed or running status breaks stopped continuity'
    Assert-Equal @($commands | Where-Object {$_ -match '^desktop restart'}) @() 'unknown status cannot restart Docker'
    Assert-Equal $killed.Count 0 'unknown status cannot terminate processes'
}

Reset-Scenario
$script:engine=$false
0..9 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (2000000 + $_ * 60) }
$null=Restore-StoppedDocker 'unknown' 2000550
$null=Restore-StoppedDocker 'stopped' 2000600
1..9 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (2000600 + $_ * 60) }
Assert-Equal $commands.Count 0 'unknown cannot contribute elapsed time'
$null=Restore-StoppedDocker 'stopped' 2001200
Assert-Equal $commands.Count 1 'a fresh ten-minute interval can recover'

Reset-Scenario
$script:engine=$false
$null=Restore-StoppedDocker 'stopped' 3000000
$null=Restore-StoppedDocker 'stopped' 3000600
Assert-Equal $commands.Count 0 'sleep or a long observation gap restarts the ten-minute interval'

Reset-Scenario
$script:engine=$false; $script:restartOK=$false; $script:desktopStatus='{"Status":"stopped"}'
$script:candidates=@(
    @{Name='Docker Desktop.exe';ExecutablePath='C:\Program Files\Docker\Docker\frontend\Docker Desktop.exe';ProcessId=11},
    @{Name='com.docker.backend.exe';ExecutablePath='C:\Program Files\Docker\Docker\resources\com.docker.backend.exe';ProcessId=12},
    @{Name='docker-desktop.exe';ExecutablePath='C:\Program Files\Docker\Docker\resources\cli-plugins\docker-desktop.exe';ProcessId=13},
    @{Name='Claude.exe';ExecutablePath='C:\Program Files\Docker\Docker\Claude.exe';ProcessId=21},
    @{Name='Codex.exe';ExecutablePath='E:\Tools\Codex.exe';ProcessId=22},
    @{Name='Docker Desktop.exe';ExecutablePath='C:\Program Files\Docker\Docker-other\Docker Desktop.exe';ProcessId=23},
    @{Name='com.docker.backend.exe';ExecutablePath=$null;ProcessId=24},
    @{Name='wsl.exe';ExecutablePath='C:\Windows\System32\wsl.exe';ProcessId=25})
0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (4000000 + $_ * 60) }
Assert-Equal $killed @(11,12,13) 'fallback targets only known Desktop names inside its installation directory'
Assert-Equal $startedTasks @('TimeAudit_AutoStart') 'failed restart uses existing bootstrap once'
$null=Restore-StoppedDocker 'stopped' 4000660
Assert-Equal $killed.Count 3 'failed recovery is not repeated next cycle'
Assert-Equal $startedTasks.Count 1 'bootstrap is not repeated next cycle'
$script:desktop=$false
Restore-TimeAuditDependencies
Assert-Equal $startedTasks.Count 1 'later Desktop absence cannot bypass the spent attempt latch'

$fallbackCandidates=$script:candidates
Reset-Scenario
$script:candidates=$fallbackCandidates; $script:missingPids=@(12); $script:exitDuringStop=13
$script:engine=$false; $script:restartOK=$false; $script:desktopStatus='{"Status":"stopped"}'
0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (4100000 + $_ * 60) }
Assert-Equal $killed @(11) 'already gone and naturally exiting child processes do not abort fallback'
Assert-Equal $startedTasks.Count 1 'natural child exits do not suppress bootstrap'

Reset-Scenario
$script:candidates=$fallbackCandidates; $script:replacementPaths=@{11='E:\Tools\Claude.exe'}
$script:engine=$false; $script:restartOK=$false; $script:desktopStatus='{"Status":"stopped"}'
0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (4150000 + $_ * 60) }
Assert-Equal $killed @(12,13) 'a reused PID with a different current path cannot be terminated'
Assert-Equal $startedTasks.Count 1 'skipping a reused PID does not abort other owned-process recovery'

Reset-Scenario
$script:candidates=$fallbackCandidates; $script:deniedPid=11
$script:engine=$false; $script:restartOK=$false; $script:desktopStatus='{"Status":"stopped"}'
0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (4200000 + $_ * 60) }
Assert-Equal $startedTasks.Count 0 'real stop permission failure defers bootstrap'
Assert-Equal ((Get-Content -LiteralPath $dockerRecoveryState -Raw | ConvertFrom-Json).Attempted) $true 'permission failure retains attempt latch'

foreach ($case in @(
    @{Output='{"Status":"running"}'; Success=$true; Timeout=$false},
    @{Output='{"Status":"stopped"}'; Success=$true; Timeout=$true},
    @{Output='not json'; Success=$true; Timeout=$false})) {
    Reset-Scenario
    $script:candidates=$fallbackCandidates; $script:engine=$false; $script:restartOK=$false
    $script:desktopStatus=$case.Output; $script:statusSuccess=$case.Success; $script:statusTimeout=$case.Timeout
    0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (4300000 + $_ * 60) }
    Assert-Equal $killed.Count 0 'running or unknown post-restart status prevents termination'
    Assert-Equal $startedTasks.Count 0 'running or unknown post-restart status prevents fallback bootstrap'
}

Reset-Scenario
$script:engine=$false; $script:engineTimeout=$true; $script:desktopStatus='{"Status":"stopped"}'
$now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
@{Version=1;Since=$now-660;Last=$now-60;Attempted=$false} | ConvertTo-Json | Set-Content -LiteralPath $dockerRecoveryState
Restore-TimeAuditDependencies
Assert-Equal @($commands | Where-Object {$_ -match '^desktop restart'}) @('desktop restart --timeout 110') 'explicit stopped status can recover even when the API probe timed out'

Reset-Scenario
$script:restartOK=$false; $script:engine=$true
0..10 | ForEach-Object { $null=Restore-StoppedDocker 'stopped' (5000000 + $_ * 60) }
Assert-Equal $killed.Count 0 'API recovery after unconfirmed restart suppresses termination'
Assert-Equal $startedTasks.Count 0 'API recovery after unconfirmed restart suppresses bootstrap'
'PASS: dependency recovery and bounded stopped-engine scenarios'
} finally {
    Remove-Item -LiteralPath $dockerRecoveryState -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryRoot -Force
}
