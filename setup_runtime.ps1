param(
    [string]$BasePython = 'C:\Users\10979\AppData\Local\Programs\Python\Python311\python.exe',
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$venvRoot = Join-Path $ProjectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$venvPythonw = Join-Path $venvRoot 'Scripts\pythonw.exe'
$requirements = Join-Path $ProjectRoot 'requirements.txt'
$taskTemp = 'E:\Cache\Codex\Temp\TimeAuditRuntime'
$pipCache = 'E:\Downloads\pip-cache'

foreach ($path in @($BasePython, $requirements)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file is missing: $path"
    }
}
New-Item -ItemType Directory -Force -Path $taskTemp, $pipCache | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:TMPDIR = $taskTemp
$env:PIP_CACHE_DIR = $pipCache
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $BasePython -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python venv creation failed.' }
}

& $venvPython -m pip install --no-input --only-binary=:all: --requirement $requirements
if ($LASTEXITCODE -ne 0) { throw 'TimeAudit dependency installation failed.' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'TimeAudit isolated runtime has dependency conflicts.' }

$probe = @'
import importlib.metadata as metadata
import json
import sys

print(json.dumps({
    "python": sys.version.split()[0],
    "python_executable": sys.executable,
    "psutil": metadata.version("psutil"),
    "nvidia_ml_py": metadata.version("nvidia-ml-py"),
    "asyncpg": metadata.version("asyncpg"),
    "deprecated_pynvml_distribution_present": any(
        distribution.metadata["Name"].lower() == "pynvml"
        for distribution in metadata.distributions()
    ),
}))
'@
$runtime = $probe | & $venvPython -
if ($LASTEXITCODE -ne 0) { throw 'TimeAudit isolated runtime probe failed.' }

[pscustomobject]@{
    schema = 'timeaudit.isolated-runtime.v1'
    status = 'pass'
    project_root = $ProjectRoot
    python = $venvPython
    pythonw = $venvPythonw
    runtime = ($runtime | ConvertFrom-Json)
} | ConvertTo-Json -Depth 5
