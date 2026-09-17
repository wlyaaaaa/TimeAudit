#requires -Version 7.2
[CmdletBinding()]
param([string]$OutFile=(Join-Path $env:TEMP 'time_audit_status.txt'))
$ErrorActionPreference='Stop'
$report=@('TimeAudit 运行状态','==========================================')
$names=@{telemetry='遥测写入心跳';activity_heartbeat='时间采集心跳';activity_persistence='时间记录落盘';ingester='事件入库';database='数据库最新实测';sensors='硬件传感器';backup='备份可验证性';memory_blackbox='原生内存黑匣子';watchdog_last_outcome='看门狗最近验收'}
$start=[Diagnostics.ProcessStartInfo]::new((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))
$start.UseShellExecute=$false;$start.CreateNoWindow=$true
$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
$start.ArgumentList.Add('-B');$start.ArgumentList.Add((Join-Path $PSScriptRoot 'timeaudit_health.py'))
$process=[Diagnostics.Process]::new();$process.StartInfo=$start
try {
    if(-not $process.Start()){throw 'probe_start_failed'}
    $out=$process.StandardOutput.ReadToEndAsync();$err=$process.StandardError.ReadToEndAsync()
    if(-not $process.WaitForExit(12000)){$process.Kill($true);$process.WaitForExit();throw 'probe_timeout'}
    $text=$out.GetAwaiter().GetResult();$null=$err.GetAwaiter().GetResult()
    if($process.ExitCode -notin @(0,2) -or $text.Length -gt 65536){throw 'invalid_probe_output'}
    $health=$text|ConvertFrom-Json -Depth 12
    if($health.schema -ne 'timeaudit.runtime-health.v1'){throw 'invalid_health_contract'}
    if($health.status -ne 'healthy'){$report+='[OFFLINE] 部分证据异常或缺失，见下方分项。'}
    foreach($property in $health.components.PSObject.Properties){
        $value=$property.Value;$label=$names[$property.Name]
        if(-not $label){continue}
        $mark=if($value.status -eq 'healthy'){'[正常]'}else{'[OFFLINE] 需检查'}
        $age=if($value.PSObject.Properties.Name -contains 'age_seconds'){'，距最近更新 '+$value.age_seconds+' 秒'}else{''}
        $report+="$mark $label$age"
        if($property.Name -eq 'memory_blackbox' -and $value.PSObject.Properties.Name -contains 'rolling_span_hours'){
            $report+='    滚动记录首尾跨度 '+$value.rolling_span_hours+' 小时，具体窗口仍需核对缺口；旧启动尾段另计。'
        }
    }
    $report+='=========================================='
    $report+='健康检查不会启动或重启任何组件。'
    $report+='无热点温度读数不等于过热；FPS 空闲不等于采集故障。'
    $report+='旧备份缺少校验记录时会提示需检查，不会冒充已验证。'
}catch{$report+='[OFFLINE] 状态证据暂时不可用；本次没有执行修复。'}
finally{
    $process.Dispose()
    $report|Out-File -LiteralPath $OutFile -Encoding unicode -Force
}