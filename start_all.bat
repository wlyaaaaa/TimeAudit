@echo off
:: ==========================================
:: 🚀 个人工时数仓 - 宿主机开机自启守护脚本
:: ==========================================
chcp 65001 >nul
set PROJECT_DIR=E:\TimeAudit

echo [*] 正在拉起 AHK 状态机内核...
start "" "%PROJECT_DIR%\TimeAudit.ahk"

echo [*] 正在引导本地 Docker 容器群...
cd /d "%PROJECT_DIR%"
docker compose up -d

echo [*] 正在等待本地数仓端口 (55432) 物理就绪...
:WAIT_DB
timeout /t 1 >nul
netstat -ano | findstr 55432 >nul
if %errorlevel% neq 0 (
    goto WAIT_DB
)
echo [+] 数据库物理端口已就位。

echo [*] 正在以【完全隐形守护模式 + 管理员特权】拉起 Python 遥测核心...
:: 🟢 修复：将 -Verb RunAs 正确收入双引号内，确保无窗拉起的同时触发 Windows 管理员权限
powershell -WindowStyle Hidden -Command "Start-Process python -ArgumentList '%PROJECT_DIR%\main.py' -WindowStyle Hidden -Verb RunAs"

echo [+] 开机自启守护管线引导完毕。
exit