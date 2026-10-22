# The scheduled entry keeps its existing user/session; only the engine launch elevates.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Elevation.ps1')
if(-not(Test-ProjectAdministrator)){Invoke-ProjectElevated (New-ProjectInvocation $PSCommandPath @{});exit $LASTEXITCODE}
Start-Process (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe') -ArgumentList ('"'+(Join-Path $PSScriptRoot 'main.py')+'"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
