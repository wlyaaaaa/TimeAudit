' Hidden launcher for TimeAudit_AutoStart.
' WindowStyle=0 keeps the batch file from opening a foreground console.
Dim fso, here, shell, exitCode
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
exitCode = shell.Run("cmd.exe /c """ & here & "\start_all.bat""", 0, True)
WScript.Quit exitCode
