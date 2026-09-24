$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'telemetry_watchdog.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors) { throw ($parseErrors.Message -join '; ') }
$definition = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Restore-TimeAuditDependencies'
}, $true)
# Exercise the production recovery decision function without touching services.
. ([scriptblock]::Create($definition.Extent.Text))
$sidecarGraceSeconds = 15
function Log($message) { }
function Start-Sleep { param($Seconds) }
function Get-Process { param($Name, $ErrorAction) if ($script:desktop) { @{Id=1} } }
function Get-ScheduledTask { param($TaskName, $ErrorAction) @{State=$script:taskState} }
function Start-ScheduledTask { param($TaskName, $ErrorAction) $script:startedTasks.Add($TaskName) }
function Test-GrafanaEndpoint { $script:grafanaHealthy }
function Invoke-DockerBounded([string]$Arguments, [int]$TimeoutSeconds=8) {
    $script:commands.Add($Arguments)
    if ($Arguments.StartsWith('info')) { return @{Success=$script:engine; Output='version'} }
    if ($Arguments.StartsWith('inspect --format {{.State.Status}} ')) {
        $name = ($Arguments -split ' ')[-1]
        return @{Success=$script:states.ContainsKey($name); Output=$script:states[$name]}
    }
    if ($Arguments -match 'StartedAt') { return @{Success=$true; Output=$script:grafanaStarted} }
    return @{Success=(-not $script:mutationTimeout); Output=''; TimedOut=$script:mutationTimeout}
}
function Reset-Scenario {
    $script:commands = [Collections.Generic.List[string]]::new()
    $script:startedTasks = [Collections.Generic.List[string]]::new()
    $script:engine=$true; $script:desktop=$true; $script:taskState='Ready'
    $script:states=@{'audit-postgres'='running';'audit-grafana'='running'}
    $script:grafanaHealthy=$true; $script:mutationTimeout=$false
    $script:grafanaStarted=[DateTimeOffset]::UtcNow.AddMinutes(-10).ToString('o')
}
function Assert-Equal($actual, $expected, $label) {
    if (($actual -join '|') -ne ($expected -join '|')) { throw "$label : got $actual expected $expected" }
}
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
'PASS: 8 dependency recovery scenarios'
