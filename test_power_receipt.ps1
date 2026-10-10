$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'telemetry_watchdog.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors) { throw ($parseErrors.Message -join '; ') }
$definition = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-PowerToday' }, $true)
. ([scriptblock]::Create($definition.Extent.Text))
$powerSql = 'SELECT 1'
$powerReceipt = Join-Path ([IO.Path]::GetTempPath()) ("power-today-test-" + [guid]::NewGuid() + '.json')
$script:calls = 0; $script:ok = $true
function Invoke-DockerBounded([string]$Arguments, [int]$TimeoutSeconds = 8) {
    $script:calls++
    if (-not $Arguments.StartsWith('exec audit-postgres psql') -or $Arguments -notmatch 'ON_ERROR_STOP') { throw 'unexpected command' }
    @{ Success = $script:ok; Output = "2026-10-10|6.683|459|85548`n2026-10-11|1.583|435|19452" }
}
try {
    Write-PowerToday
    $value = Get-Content -LiteralPath $powerReceipt -Raw | ConvertFrom-Json
    if ($value.schema -ne 'timeaudit.power-today.v1' -or $value.days.Count -ne 2) { throw 'receipt shape' }
    if ($value.days[1].energy_kwh -ne 1.583 -or $value.days[1].peak_watts -ne 435 -or $value.days[0].samples -ne 85548) { throw 'receipt values' }
    if ((Get-Content -LiteralPath $powerReceipt -Raw) -notmatch '"written_at":"[^"]+[+]08:00"') { throw 'written_at offset' }
    Write-PowerToday
    if ($script:calls -ne 1) { throw 'not throttled to 10 minutes' }
    (Get-Item -LiteralPath $powerReceipt).LastWriteTime = (Get-Date).AddMinutes(-11)
    $before = Get-Content -LiteralPath $powerReceipt -Raw
    $script:ok = $false
    Write-PowerToday
    if ($script:calls -ne 2 -or (Get-Content -LiteralPath $powerReceipt -Raw) -ne $before) { throw 'failed query must keep the last receipt' }
    'power receipt tests passed'
} finally { Remove-Item -LiteralPath $powerReceipt -Force -ErrorAction SilentlyContinue }
