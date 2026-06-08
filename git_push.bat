@echo off
:: 自动切换到脚本所在的目录（即 Git 根目录）
cd /d "%~dp0"

echo [1/3] Adding changes...
git add .

:: 获取当前的日期和时间作为默认提交信息
set commit_msg=Auto commit: %date% %time%

echo [2/3] Committing with message: "%commit_msg%"
git commit -m "%commit_msg%"

echo [3/3] Pushing to remote repository...
git push

echo ====================================
echo  推送完成！窗口将在 3 秒后自动关闭...
echo ====================================
timeout /t 3 >nul