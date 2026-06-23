# TimeAudit telemetry watchdog
# Restarts main.py if the MAIN collector process has crashed/disappeared.
# Guards ONLY process existence (not data lag) on purpose: during system
# sleep/idle the engine intentionally pauses DB writes, so a lag-based check
# would false-restart. A missing main.py process is an unambiguous crash.
$ErrorActionPreference = 'SilentlyContinue'
$log    = 'E:\TimeAudit\telemetry_watchdog.log'
$py     = 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe'
$script = 'E:\TimeAudit\main.py'

function Log($m){ "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Out-File -FilePath $log -Append -Encoding utf8 }

# Is main.py running?
$main = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'main\.py' } | Select-Object -First 1
if ($main) { exit 0 }   # healthy -> nothing to do

Log "main.py NOT running - attempting restart"
if (-not (Test-Path $py) -or -not (Test-Path $script)) { Log "ABORT: python or main.py path missing"; exit 1 }

# Restart via a one-shot elevated scheduled task (same method proven to bring
# up LHM/PresentMon child processes with proper elevation).
$tn  = 'TimeAudit_WatchdogRestart_tmp'
$act = New-ScheduledTaskAction -Execute $py -Argument ('"{0}"' -f $script) -WorkingDirectory 'E:\TimeAudit'
$pr  = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Highest -LogonType Interactive
Register-ScheduledTask -TaskName $tn -Action $act -Principal $pr -Force | Out-Null
Start-ScheduledTask -TaskName $tn
Start-Sleep -Seconds 6
Unregister-ScheduledTask -TaskName $tn -Confirm:$false

$now = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
       Where-Object { $_.CommandLine -match 'main\.py' } | Select-Object -First 1
if ($now) { Log ("RESTART OK - main.py PID {0}" -f $now.ProcessId) }
else      { Log "RESTART FAILED - main.py still down" }
