# check_status_gui.ps1
# =====================================================================
# 针对 VBScript GUI 弹窗定制的纯文本解析器 (时区对齐与 PID 锁安全检测版)
# =====================================================================
$outFile = "$env:TEMP\time_audit_status.txt"
$pidFile = "E:\TimeAudit\time_audit.pid"
$report = @()

$report += "=========================================="
$report += "   🛸 个人工时数仓 - 开机自启实况体检报告"
$report += "=========================================="

# 1. 检查 AHK 进程
$ahk = Get-Process -Name "AutoHotkey*" -ErrorAction SilentlyContinue
if ($ahk) {
    $report += "[+] AHK 状态机内核 : 运行中 [🟢] (PID: $($ahk.Id))"
} else {
    $report += "[-] AHK 状态机内核 : 已离线 [❌] [OFFLINE]"
}

# 2. 检查 Python 守护进程 (使用高稳定 PID 锁机制，避开 UAC 权限死锁)
$py_running = $false
$py_pid = "N/A"
if (Test-Path $pidFile) {
    $cached_pid = Get-Content $pidFile -Raw
    if ($cached_pid) {
        $proc = Get-Process -Id $cached_pid -ErrorAction SilentlyContinue
        if ($proc -and ($proc.Name -like "python*")) {
            $py_running = $true
            $py_pid = $cached_pid
        }
    }
}

if ($py_running) {
    $report += "[+] Python 遥测守护 : 运行中 [🟢] (PID: $py_pid)"
} else {
    $report += "[-] Python 遥测守护 : 已离线 [❌] [OFFLINE]"
}

# 3. 检查 Postgres 端口
$ports = Get-NetTCPConnection -LocalPort 55432 -ErrorAction SilentlyContinue
if ($ports) {
    $unique_pid = ($ports | Select-Object -First 1).OwningProcess
    $report += "[+] Docker Postgres : 端口 55432 就绪 [🟢] (PID: $unique_pid)"
} else {
    $report += "[-] Docker Postgres : 端口 55432 闭塞 [❌] [OFFLINE]"
}

# 4. 检查数仓实况心跳 (由数据库直接在内部解算物理时差，终结 8 小时时区漂移)
try {
    $container = docker ps --filter "publish=55432" --format "{{.Names}}"
    if ($container) {
        # 直接让 Postgres 物理计算秒级 Epoch 时差，完美消灭 Windows 时区干扰
        $epoch_query = "SELECT EXTRACT(EPOCH FROM (now() - timestamp)) FROM public.fact_system_hardware ORDER BY timestamp DESC LIMIT 1;"
        $diff_sec_raw = docker exec -i $container psql -U leyang -d time_audit -t -c "$epoch_query" 2>$null
        
        if ($diff_sec_raw -and $diff_sec_raw.Trim() -ne "") {
            $diff_sec = [double]::Parse($diff_sec_raw.Trim())
            $diff_sec_format = [Math]::Round($diff_sec, 1)
            
            if ($diff_sec -lt 10.0) {
                $report += "[+] 数仓实况写入   : 绿色健康 [🟢] (延时: $diff_sec_format 秒)"
            } else {
                $report += "[!] 数仓实况写入   : 心跳滞后 [⚠️] [OFFLINE] (延时: $diff_sec_format 秒)"
            }
        } else {
            $report += "[-] 数仓实况写入   : 无数据 [❌] [OFFLINE] (数据库内尚无任何采样记录)"
        }
    } else {
        $report += "[-] 数仓实况写入   : 容器未运行 [❌] [OFFLINE]"
    }
} catch {
    $report += "[-] 数仓实况写入   : 数仓连接失败 [❌] [OFFLINE]"
}

$report += "=========================================="
$report += "提示：若有组件离线，请双击运行 startup.bat。"

# 🟢 关键：强制以宽字符 Unicode (UTF-16) 编码落库，为 VBScript 弹窗保留完美的 Emoji 颜色
$report | Out-File -FilePath $outFile -Encoding unicode -Force