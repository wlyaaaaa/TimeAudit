@echo off
setlocal EnableExtensions EnableDelayedExpansion
:: ==========================================
:: 自动拉起脚本 - 管理员提权自适应版本
:: ==========================================
set "PROJECT_DIR=E:\Projects\Tools\TimeAudit"
set "DOCKER_DESKTOP=C:\Program Files\Docker\Docker\Docker Desktop.exe"
set DOCKER_WAIT_SECONDS=180
set DB_WAIT_SECONDS=120
if not defined TIMEAUDIT_DB_HOST_PORT set "TIMEAUDIT_DB_HOST_PORT=45432"
set "PATH=%PATH%;C:\Program Files\Docker\Docker\resources\bin;C:\Program Files\Git\cmd"

echo [*] 启动 AHK 守护进程...
start "" "%PROJECT_DIR%\TimeAudit.ahk"

cd /d "%PROJECT_DIR%"
echo [*] 启动 Docker Desktop 并等待 Linux engine 就绪...
if exist "%DOCKER_DESKTOP%" (
    start "" "%DOCKER_DESKTOP%"
) else (
    echo [-] 未找到 Docker Desktop: "%DOCKER_DESKTOP%"
    exit /b 1
)

set /a DOCKER_WAIT_REMAINING=%DOCKER_WAIT_SECONDS%
:WAIT_DOCKER
docker info >nul 2>nul
if %errorlevel% neq 0 (
    if !DOCKER_WAIT_REMAINING! LEQ 0 (
        echo [-] Docker daemon 在 %DOCKER_WAIT_SECONDS% 秒内未就绪，放弃启动容器。
        exit /b 1
    )
    ping -n 4 127.0.0.1 >nul
    set /a DOCKER_WAIT_REMAINING-=3
    goto WAIT_DOCKER
)
echo [+] Docker daemon 已就绪。

echo [*] 启动 Docker 依赖...
docker compose up -d
if %errorlevel% neq 0 (
    echo [-] docker compose up -d 失败。
    exit /b 1
)

echo [*] 正在等待数据库端口 (%TIMEAUDIT_DB_HOST_PORT%) 联通...
set /a DB_WAIT_REMAINING=%DB_WAIT_SECONDS%
:WAIT_DB
ping -n 3 127.0.0.1 >nul
powershell -NoProfile -NonInteractive -Command "$client = [Net.Sockets.TcpClient]::new(); try { $pending = $client.ConnectAsync('127.0.0.1', [int]$env:TIMEAUDIT_DB_HOST_PORT); if (-not $pending.Wait(1000)) { exit 1 }; if (-not $client.Connected) { exit 1 }; exit 0 } catch { exit 1 } finally { $client.Dispose() }" >nul 2>nul
if %errorlevel% neq 0 (
    if !DB_WAIT_REMAINING! LEQ 0 (
        echo [-] 数据库端口 %TIMEAUDIT_DB_HOST_PORT% 在 %DB_WAIT_SECONDS% 秒内未就绪，放弃启动采集器。
        exit /b 1
    )
    set /a DB_WAIT_REMAINING-=2
    goto WAIT_DB
)
echo [+] 数据库端口已经就位！

echo [*] 启动后台核心守护进程 + 提权拉起 Python 遥测引擎...
powershell -WindowStyle Hidden -Command "$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); if ($isAdmin) { Start-Process 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe' -ArgumentList '%PROJECT_DIR%\main.py' -WorkingDirectory '%PROJECT_DIR%' -WindowStyle Hidden } else { Start-Process 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe' -ArgumentList '%PROJECT_DIR%\main.py' -WorkingDirectory '%PROJECT_DIR%' -WindowStyle Hidden -Verb RunAs }"

echo [+] 引擎启动指令已下发！
exit /b 0
