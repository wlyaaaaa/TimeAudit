"""PowerShell loadability and bounded watchdog recovery."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


def test_live_snapshot_runs_when_recovery_is_busy_or_fails():
    name = 'TimeAuditFixture-' + os.urandom(8).hex()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, 1, name)
    assert handle
    script = r"""
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile('__ROOT__/telemetry_watchdog.ps1',[ref]$null,[ref]$null)
$cycle=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-TimeAuditWatchdogCycle'},$true)
Invoke-Expression $cycle.Extent.Text
$repairs=0; $exports=0; $failure=$null
function Log($message) { }
function Invoke-TimeAuditWatchdog { $script:repairs++; throw 'fixture recovery failed' }
function Write-WatchdogOutcome($state,$components,$reason) { $script:failure=$reason }
function Write-TimeAuditLiveSnapshot { $script:exports++ }
@{busy=(Invoke-TimeAuditWatchdogCycle '__MUTEX__'); failure=(Invoke-TimeAuditWatchdogCycle '__MUTEX__-free'); repairs=$repairs; exports=$exports; reason=$failure}|ConvertTo-Json -Compress
""".replace('__ROOT__', str(ROOT).replace("'", "''")).replace('__MUTEX__', name)
    try:
        result = subprocess.run([powershell(), '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, encoding='utf-8-sig', timeout=15)
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(busy=0, failure=2, repairs=1, exports=2, reason='recovery_invocation_failed')


def test_snapshot_child_inherits_normal_and_launcher_restores_its_priority(tmp_path):
    child = tmp_path / 'child.py'
    child.write_text(
        "import ctypes as c, pathlib, sys\n"
        "k=c.WinDLL('kernel32');k.GetCurrentProcess.restype=c.c_void_p;k.GetPriorityClass.argtypes=(c.c_void_p,)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(k.GetPriorityClass(k.GetCurrentProcess())))\n",
        encoding='utf-8',
    )
    output = tmp_path / 'priority.txt'
    script = r"""
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile('__ROOT__/telemetry_watchdog.ps1',[ref]$null,[ref]$null)
$start=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-TimeAuditSnapshotProcess'},$true)
Invoke-Expression $start.Extent.Text
$self=[Diagnostics.Process]::GetCurrentProcess(); $self.PriorityClass='BelowNormal'
$pyConsole='__PYTHON__'
$child=Start-TimeAuditSnapshotProcess @('-B','"__CHILD__"','"__OUTPUT__"')
$self.Refresh(); $after=$self.PriorityClass.ToString()
if(-not $child.WaitForExit(20000) -or $child.ExitCode -ne 0) { throw 'probe process failed' }
$pyConsole='__CHILD__.missing.exe'; $launchFailed=$false
try { Start-TimeAuditSnapshotProcess @('ignored') } catch { $launchFailed=$true }
$self.Refresh()
@{after=$after; after_error=$self.PriorityClass.ToString(); launch_failed=$launchFailed; child=[int](Get-Content '__OUTPUT__')}|ConvertTo-Json -Compress
"""
    for key, value in dict(ROOT=ROOT, PYTHON=sys.executable, CHILD=child, OUTPUT=output).items():
        script = script.replace(f'__{key}__', str(value).replace("'", "''"))
    result = subprocess.run([powershell(), '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, encoding='utf-8-sig', timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(after='BelowNormal', after_error='BelowNormal', launch_failed=True, child=32)
