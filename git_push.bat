@echo off
:: 自动切换到脚本所在的目录（即 Git 根目录）
cd /d "%~dp0"

echo [0/3] Pre-checking large files and gitignore...
:: 预防性保护：如果 .gitignore 不存在则创建，存在则强行追加大文件，防止被 Claude 重置冲掉
if not exist .gitignore type nul > .gitignore
findstr /x "time_audit_perfect_archive.sql" .gitignore >nul 2>&1
if errorlevel 1 (
    echo.>> .gitignore
    echo time_audit_perfect_archive.sql>> .gitignore
)
:: 强行把可能已经被误抓的大文件从 Git 暂存区踢出去
git rm --cached time_audit_perfect_archive.sql >nul 2>&1

echo [1/3] Adding changes...
git add .

:: 获取当前的日期和时间作为默认提交信息
set commit_msg=Auto commit: %date% %time%

echo [2/3] Committing with message: "%commit_msg%"
git commit -m "%commit_msg%"

echo [3/3] Pushing to remote repository (Forced)...
:: 核心改动：使用 -f (强推) 碾压一切非快进式冲突，无脑同步 Claude 分支历史
git push -f

echo ====================================
echo  推送完成！窗口将在 3 秒后自动关闭...
echo ====================================
:: 显式调用 .exe，彻底修复在 Git Bash 运行命令时 /t 参数报错的 Bug
timeout.exe /t 3 >nul