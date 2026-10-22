# Repository-local elevation; no system configuration or action authorization changes.
function Test-ProjectAdministrator {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function New-ProjectInvocation([string]$Path, [System.Collections.IDictionary]$Parameters) {
    $copy=@{};foreach($key in $Parameters.Keys){$v=$Parameters[$key];if($v -is [Management.Automation.SwitchParameter]){$v=[bool]$v};$copy[$key]=$v}
    $xml=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([Management.Automation.PSSerializer]::Serialize($copy)))
    return ('$p=[Management.Automation.PSSerializer]::Deserialize([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''{0}''))); & ''{1}'' @p' -f $xml,$Path.Replace("'","''"))
}
function Invoke-ProjectElevated([string]$Command) {
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $prefix='[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference="Stop";$global:LASTEXITCODE=0;'
    $prefix+='if(-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw "需要本人在场提权"};'
    $prefix+='if([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne '''+$sid+'''){throw "Elevation changed Windows user"}; Set-Location -LiteralPath '''+(Get-Location).Path.Replace("'","''")+''';'
    $exe=(Get-Command pwsh.exe -ErrorAction Stop).Source
    $sudo=Get-Command sudo.exe -ErrorAction SilentlyContinue
    $mode=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Sudo' -Name Enabled -ErrorAction SilentlyContinue
    if($sudo -and $mode -and $mode.Enabled -eq 3){
        $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($prefix+$Command+';exit $LASTEXITCODE'))
        & $sudo.Source $exe -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $encoded
        $global:LASTEXITCODE=$LASTEXITCODE; return
    }
    $log=[IO.Path]::GetTempFileName()
    try {
        $body=$prefix+'& { '+$Command+' } *> '''+$log.Replace("'","''")+''';exit $LASTEXITCODE'
        $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
        try{$process=Start-Process $exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList @('-NoProfile','-NonInteractive','-OutputFormat','Text','-EncodedCommand',$encoded) -ErrorAction Stop}
        catch{throw '需要本人在场提权：系统 sudo 内联模式和隐藏 UAC 提权均不可用。'}
        Get-Content -LiteralPath $log -Encoding UTF8
        $global:LASTEXITCODE=$process.ExitCode
    } finally {Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue}
}
