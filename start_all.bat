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

echo [*] 正在以【完全隐形守护模式】拉起 Python 遥测核心...
:: 🟢 配合新 main.py：使用 PowerShell 启动完全没有黑窗口的后台 Python 实例
:: 因为 main.py 内部已经做了全套自愈重试和日志记录，这里直接无窗拉起即可
powershell -WindowStyle Hidden -Command "Start-Process python -ArgumentList '%PROJECT_DIR%\main.py' -WindowStyle Hidden"

echo [+] 开机自启守护管线引导完毕。
exit