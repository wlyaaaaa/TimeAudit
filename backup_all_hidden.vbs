' ============================================================
'  隐藏窗口启动器 —— 由计划任务 TimeAudit_DailyBackup 调用。
'  作用：在不弹出 PowerShell 窗口、不抢占前台焦点的前提下，
'        于当前交互会话内运行每日全量备份脚本 backup_all.ps1。
'  WindowStyle=0 表示完全隐藏；bWaitOnReturn=False 表示不阻塞。
'  可移植：自动推导本脚本所在目录，整个文件夹搬走后仍可用。
' ============================================================
Dim fso, here, shell
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\backup_all.ps1""", 0, False
