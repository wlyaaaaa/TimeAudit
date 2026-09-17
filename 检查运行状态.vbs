Option Explicit
Dim shell, fso, tempDir, tempFile, command, code, file, text
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
tempDir = "E:\Cache\Codex\Temp\TimeAudit-Status"
If Not fso.FolderExists(tempDir) Then fso.CreateFolder(tempDir)
tempFile = fso.BuildPath(tempDir, fso.GetTempName)
command = Chr(34) & "C:\Program Files\PowerShell\7\pwsh.exe" & Chr(34) & _
    " -NoProfile -NonInteractive -WindowStyle Hidden -File " & Chr(34) & _
    "E:\Projects\Tools\TimeAudit\check_status_gui.ps1" & Chr(34) & _
    " -OutFile " & Chr(34) & tempFile & Chr(34)
code = shell.Run(command, 0, True)
If fso.FileExists(tempFile) Then
    Set file = fso.OpenTextFile(tempFile, 1, False, -1)
    text = file.ReadAll
    file.Close
    fso.DeleteFile tempFile
Else
    text = "[OFFLINE] TimeAudit health evidence is unavailable. No recovery was attempted."
End If
If InStr(text, "[OFFLINE]") > 0 Then
    MsgBox text, 48, "TimeAudit - Status"
Else
    MsgBox text, 64, "TimeAudit - Status"
End If