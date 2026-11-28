"""PowerShell loadability and bounded watchdog recovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parent
pytestmark=pytest.mark.skipif(os.name != 'nt', reason='Windows diagnostic entry points')


def powershell():
    executable=shutil.which('pwsh')
    assert executable, 'qualified PowerShell runtime is required for this Windows project'
    return executable


def test_all_project_powershell_sources_parse():
    path=str(ROOT).replace("'","''")
    script="""
$ErrorActionPreference='Stop'
$failures=@()
$files=@(Get-ChildItem -LiteralPath '__ROOT__' -File -Filter '*.ps1')
foreach($file in $files){
    $tokens=$null;$errors=$null
    $null=[Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors)
    if(@($errors).Count){$failures+=@{file=$file.Name;errors=@($errors).Count}}
}
@{files=$files.Count;failures=$failures}|ConvertTo-Json -Depth 5 -Compress
""".replace('__ROOT__',path)
    result=subprocess.run([powershell(),'-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,encoding='utf-8-sig',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0
    report=json.loads(result.stdout)
    assert report['files']>0
    assert report['failures']==[]


def test_watchdog_timeouts_without_live_recovery():
    path=str(ROOT).replace("'","''")
    script=r"""
$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ast=[Management.Automation.Language.Parser]::ParseFile('__ROOT__/telemetry_watchdog.ps1',[ref]$null,[ref]$null)
$wanted=@('Test-IngesterRunning','Restart-Ingester','Restore-TimeAuditDependencies')
$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $wanted},$true)|ForEach-Object {Invoke-Expression $_.Extent.Text}
$docker='mock';$compose='mock directory/compose.yml';$calls=@();$logs=@();$results=[Collections.Queue]::new()
function Invoke-DockerBounded([string]$Arguments,[int]$TimeoutSeconds=8){$script:calls+=@{command=$Arguments;timeout=$TimeoutSeconds};return $script:results.Dequeue()}
function Test-Path {return $true};function Start-Sleep {};function Test-HeartbeatFresh {return $true}
function Log($message){$script:logs+=,$message}
$timeout=@{Success=$false;Output='';TimedOut=$true};$bad=@{Success=$false;Output='';TimedOut=$false};$good=@{Success=$true;Output='true';TimedOut=$false}
$results.Enqueue($timeout);Restart-Ingester 'test'
if($calls.Count -ne 1 -or $calls[0].timeout -ne 30){throw 'restart timeout must defer without compose'}
$calls=@();$results.Enqueue($bad);$results.Enqueue($timeout);Restart-Ingester 'test'
if($calls.Count -ne 2 -or $calls[1].timeout -ne 30 -or $calls[1].command -ne 'compose -f "mock directory/compose.yml" up -d audit-ingest'){throw 'compose must stay bounded and quote its path'}
$results.Enqueue($good)
if(-not (Test-IngesterRunning) -or $calls[-1].timeout -ne 8){throw 'inspect must use the default bound'}
function Get-Process {return $true};function Get-DockerDesktopState {return 'running'};function Restore-UnavailableDocker {param([string]$Status) return $false}
$calls=@();$results.Enqueue($timeout);$null=Restore-TimeAuditDependencies
if($calls.Count -ne 1 -or $logs[-1] -notmatch '^Docker API probe timed out after \d+[.,]\d{2}s; desktop_status=running$'){throw 'timeout evidence missing or extra Docker commands issued'}
@{timeout_log=$logs[-1]}|ConvertTo-Json -Compress
""".replace('__ROOT__',path)
    result=subprocess.run([powershell(),'-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,encoding='utf-8-sig',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0, result.stderr
    assert 'desktop_status=running' in json.loads(result.stdout)['timeout_log']
