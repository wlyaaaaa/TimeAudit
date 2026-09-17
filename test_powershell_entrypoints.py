"""PowerShell loadability and the actual text-rendering entry point."""
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


def test_gui_provider_produces_a_readable_report(tmp_path):
    # The provider writes text only; this does not open a dialog or recover a service.
    output=tmp_path/'health-report.txt'
    result=subprocess.run([powershell(),'-NoProfile','-NonInteractive','-File',str(ROOT/'check_status_gui.ps1'),'-OutFile',str(output)],capture_output=True,text=True,encoding='utf-8-sig',timeout=18,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0
    assert output.is_file()
    text=output.read_text(encoding='utf-16')
    assert 'TimeAudit 运行状态' in text
    assert '[正常]' in text or '[OFFLINE]' in text
    assert len(text)>60
