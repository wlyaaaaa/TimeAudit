' CheckStatus.vbs
Set objShell = CreateObject("Wscript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

tempFile = objShell.ExpandEnvironmentStrings("%TEMP%\time_audit_status.txt")
errLog = objShell.ExpandEnvironmentStrings("%TEMP%\time_audit_powershell_error.log")

' ���� powershell ���ض��� stderr �� errLog������������Բ鿴
psCommand = "cmd /c powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File E:\Projects\Tools\TimeAudit\check_status_gui.ps1 2> " & Chr(34) & errLog & Chr(34)
exitCode = objShell.Run(psCommand, 0, True)

If objFSO.FileExists(tempFile) Then
    ' ����� -1 ��ʾ�� Unicode (UTF-16 LE) ģʽ��ȡ�ı����� powershell �� Out-File ����һ��
    Set objFile = objFSO.OpenTextFile(tempFile, 1, False, -1)
    strStatus = objFile.ReadAll
    objFile.Close
    objFSO.DeleteFile(tempFile)
    
    ' ������ڴ�����־�Ҵ�СΪ0����ɾ��
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
            errDetails = vbCrLf & "PowerShell ��������:" & vbCrLf & objErrFile.ReadAll
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
