' ============================================================
'  隐藏窗口启动器 —— 由计划任务 RAMDisk_Code_Backup 调用
'  作用：在不弹出 CMD 窗口、不抢占前台鼠标焦点的前提下，
'        于当前交互会话内运行 Z 盘资产备份脚本(需保留映射盘 Z:)。
'  WindowStyle=0 表示完全隐藏；bWaitOnReturn=False 表示不阻塞。
' ============================================================
CreateObject("Wscript.Shell").Run "cmd /c ""C:\Users\10979\Desktop\sync_code.bat""", 0, False
