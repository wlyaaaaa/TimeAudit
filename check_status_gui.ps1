# check_status_gui.ps1
# =====================================================================
# 针对 VBScript GUI 弹窗定制的纯文本解析器 (宿主机驱动直连穿透版)
# =====================================================================
$outFile = "$env:TEMP\time_audit_status.txt"
$report = @()

try {
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

    # 2. 检查 Python 守护进程 (双重保证：PID 文件强校验 + 网络反查)
    $py_running = $false
    $py_pid = "N/A"
    
    # 优先使用 PID 文件校验（最直接且免错）
    $pidFile = "E:\TimeAudit\time_audit.pid"
    if (Test-Path $pidFile) {
        $pidContent = (Get-Content $pidFile).Trim()
        if ($pidContent -match "^\d+$") {
            $py_proc = Get-Process -Id ([int]$pidContent) -ErrorAction SilentlyContinue
            if ($py_proc -and $py_proc.Name -like "python*") {
                $py_running = $true
                $py_pid = $pidContent
            }
        }
    }
    
    # 如果 PID 文件不存在或进程已死，回退到网络反查
    if (-not $py_running) {
        $net_conns = Get-NetTCPConnection -RemotePort 55432 -State Established -ErrorAction SilentlyContinue
        if ($net_conns) {
            $target_pid = ($net_conns | Select-Object -First 1).OwningProcess
            $py_proc = Get-Process -Id $target_pid -ErrorAction SilentlyContinue
            if ($py_proc -and $py_proc.Name -like "python*") {
                $py_running = $true
                $py_pid = $target_pid
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

    # 4. 🚀 强效修正：利用宿主机 Python + asyncpg 原生穿透网络流进行心跳实测
    # 彻底抛弃受阻的 docker ps / docker exec 命令，实现 100% 鉴权级体检
    $pyCode = @'
import asyncio, asyncpg, sys
async def check():
    try:
        c = await asyncpg.connect("postgresql://leyang:SecurePassword123@127.0.0.1:55432/time_audit")
        v = await c.fetchval("SELECT EXTRACT(EPOCH FROM (now() - timestamp)) FROM public.fact_system_hardware ORDER BY timestamp DESC LIMIT 1;")
        if v is not None:
            print(f"SUCCESS:{v}")
        else:
            print("NODATA")
        await c.close()
    except Exception as e:
        print(f"FAILED:{e}")
asyncio.run(check())
'@

    # 将 Python 代码块灌入标准输入流执行
    $db_status = $pyCode | python 2>$null

    if ($db_status -like "SUCCESS:*") {
        $diff_sec = [double]::Parse(($db_status -replace "SUCCESS:", "").Trim())
        $diff_sec_format = [Math]::Round($diff_sec, 1)
        
        if ($diff_sec -lt 10.0) {
            $report += "[+] 数仓实况写入   : 绿色健康 [🟢] (延时: $diff_sec_format 秒)"
        } else {
            $report += "[!] 数仓实况写入   : 心跳滞后 [⚠️] [OFFLINE] (延时: $diff_sec_format 秒)"
        }
    } elseif ($db_status -eq "NODATA") {
        $report += "[-] 数仓实况写入   : 无数据 [❌] [OFFLINE] (数据库内尚无任何采样记录)"
    } else {
        $report += "[-] 数仓实况写入   : 数仓连接失败 [❌] [OFFLINE]"
    }

    $report += "=========================================="
    $report += "提示：若有组件离线，请双击运行 start_all.bat。"

} catch {
    $report += "[-] 脚本运行时异常 : [❌] [OFFLINE]"
    $report += "错误详情: $_"
} finally {
    # 强保宽字符输出，拒绝弹窗格式崩塌
    $report | Out-File -FilePath $outFile -Encoding unicode -Force
}