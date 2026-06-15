@echo off
:: ==========================================
:: 自动拉起脚本 - 管理员提权自适应版本
:: ==========================================
set PROJECT_DIR=E:\TimeAudit

echo [*] 启动 AHK 守护进程...
start "" "%PROJECT_DIR%\TimeAudit.ahk"

echo [*] 启动 Docker 依赖...
cd /d "%PROJECT_DIR%"
docker compose up -d

echo [*] 正在等待数据库端口 (55432) 联通...
:WAIT_DB
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr 55432 >nul
if %errorlevel% neq 0 (
    goto WAIT_DB
)
echo [+] 数据库端口已经就位！

echo [*] 启动后台核心守护进程 + 提权拉起 Python 遥测引擎...
powershell -WindowStyle Hidden -Command "$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); if ($isAdmin) { Start-Process 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe' -ArgumentList '%PROJECT_DIR%\main.py' -WorkingDirectory '%PROJECT_DIR%' -WindowStyle Hidden } else { Start-Process 'C:\Users\10979\AppData\Local\Programs\Python\Python311\pythonw.exe' -ArgumentList '%PROJECT_DIR%\main.py' -WorkingDirectory '%PROJECT_DIR%' -WindowStyle Hidden -Verb RunAs }"

echo [+] 引擎启动指令已下发！
exit
