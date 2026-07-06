' Hidden launcher for the TimeAudit watchdog scheduled task.
Dim fso, shell, here, command, exitCode

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\telemetry_watchdog.ps1"""
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
