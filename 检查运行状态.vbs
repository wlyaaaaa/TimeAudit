' CheckStatus.vbs
Set objShell = CreateObject("Wscript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

tempFile = objShell.ExpandEnvironmentStrings("%TEMP%\time_audit_status.txt")
errLog = objShell.ExpandEnvironmentStrings("%TEMP%\time_audit_powershell_error.log")

' 运行 powershell 并重定向 stderr 到 errLog，如果出错可以查看
psCommand = "cmd /c powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File E:\TimeAudit\check_status_gui.ps1 2> " & Chr(34) & errLog & Chr(34)
exitCode = objShell.Run(psCommand, 0, True)

If objFSO.FileExists(tempFile) Then
    ' 这里的 -1 表示以 Unicode (UTF-16 LE) 模式读取文本，与 powershell 的 Out-File 保持一致
    Set objFile = objFSO.OpenTextFile(tempFile, 1, False, -1)
    strStatus = objFile.ReadAll
    objFile.Close
    objFSO.DeleteFile(tempFile)
    
    ' 如果存在错误日志且大小为0，则删除
    If objFSO.FileExists(errLog) Then
        If objFSO.GetFile(errLog).Size = 0 Then
            objFSO.DeleteFile(errLog)
        End If
    End If
Else
    errDetails = ""
    If objFSO.FileExists(errLog) Then
        If objFSO.GetFile(errLog).Size > 0 Then
            Set objErrFile = objFSO.OpenTextFile(errLog, 1, False)
            errDetails = vbCrLf & "PowerShell 错误详情:" & vbCrLf & objErrFile.ReadAll
            objErrFile.Close
        End If
        objFSO.DeleteFile(errLog)
    End If
    strStatus = "Error: Diagnostic report file not found." & vbCrLf & _
                "Exit Code: " & exitCode & vbCrLf & _
                errDetails
End If

If InStr(strStatus, "[OFFLINE]") > 0 Or InStr(strStatus, "Error:") > 0 Then
    MsgBox strStatus, 16, "TimeAudit - Status Warning"
Else
    MsgBox strStatus, 64, "TimeAudit - Status Normal"
End If
