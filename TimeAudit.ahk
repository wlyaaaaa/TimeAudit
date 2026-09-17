#Requires AutoHotkey v2.0
#SingleInstance Force
#Include %A_ScriptDir%\timeaudit_state.ahk
Persistent

global logPath := "E:\Projects\Tools\TimeAudit\log\buffer.csv"
global ahkHeartbeatPath := "E:\Projects\Tools\TimeAudit\log\ahk_heartbeat"
global healthPath := A_ScriptDir . "\log\ahk_health.json"
global maxIdleTime := 60000
global isScreenOff := false
global isLocked := false
global suspended := false
global captureErrors := 0
global captureFailing := false
DirCreate(A_ScriptDir . "\log")
global spool := TimeAuditSpool(logPath)
global tracker := TimeAuditState(ObjBindMethod(spool, "Write"), A_NowUTC, DllCall("GetTickCount64", "UInt64"))
global GUID_CONSOLE_DISPLAY_STATE := Buffer(16)
NumPut("UInt", 0x6FE69556, "UShort", 0x704A, "UShort", 0x47A0,
    "UChar", 0x8F, "UChar", 0x24, "UChar", 0xC2, "UChar", 0x8D,
    "UChar", 0x93, "UChar", 0x6F, "UChar", 0xDA, "UChar", 0x47,
    GUID_CONSOLE_DISPLAY_STATE)
global hPowerNotify := DllCall("RegisterPowerSettingNotification", "Ptr", A_ScriptHwnd, "Ptr", GUID_CONSOLE_DISPLAY_STATE, "UInt", 0, "Ptr")
global sessionNotify := DllCall("Wtsapi32\WTSRegisterSessionNotification", "Ptr", A_ScriptHwnd, "UInt", 0)
OnMessage(0x0218, WindowPowerEventHook)
OnMessage(0x02B1, SessionEventHook)
OnExit(SafeExitHandler)
SetTimer(CaptureActiveWindow, -2000, 1)

CaptureActiveWindow() {
    global captureErrors, captureFailing
    Critical(true)
    try {
        CaptureActiveWindowCore()
        captureFailing := false
    } catch {
        captureErrors += 1
        captureFailing := true
    }
    finally {
        WriteAhkHeartbeat()
        WriteAhkHealth()
        SetTimer(CaptureActiveWindow, -2000, 1)
        Critical(false)
    }
}

CaptureActiveWindowCore() {
    global tracker, isScreenOff, isLocked, suspended, maxIdleTime
    if suspended
        return
    ; 2. 【层级漏斗状态机】动态判定当前这一秒电脑到底属于什么工时状态
    currentProcess := ""
    currentTitle := ""
    
    ; 暂离检测级联：若键鼠静止超设定期限，但系统输出通道有音频振幅（看视频/开会），则豁免暂离状态
    isIdleState := (A_TimeIdlePhysical >= maxIdleTime)
    if (isIdleState && IsSystemAudioPlaying()) {
        isIdleState := false
    }
    
    if (isLocked) {
        currentProcess := "System_LockScreen"
        currentTitle := "Windows session locked"
    }
    else if (isScreenOff) {
        currentProcess := "System_DisplayOff"
        currentTitle := "🖥️ 显示器已熄灭 / 操作系统处于伪睡眠或锁屏状态"
    } 
    else if (isIdleState) {
        currentProcess := "System_Idle"
        currentTitle := "用户暂离/无键鼠物理操作"
    } 
    else {
        currentProcess := "Idle"
        currentTitle := "屏幕无聚焦响应"
        try {
            activeHWND := WinExist("A")
            if activeHWND {
                ; 🛡️【非阻塞内核预检】兼顾“数据采集”与“脚本防死锁”的双重诉求
                if DllCall("IsHungAppWindow", "Ptr", activeHWND, "Int") {
                    currentProcess := "System_Hung"
                    currentTitle := "⚠️ 聚焦应用卡死重置中(灰屏期)"
                } else {
                    ; 缓存密集型探测：大幅削减 WinGetProcessName 底层系统调用开销
                    currentProcess := WinGetProcessName(activeHWND)
                    currentTitle := WinGetTitle(activeHWND)
                    
                    ; 📊 操作系统底层组件状态清洗
                    if (currentProcess == "explorer.exe") {
                        currentTitle := (currentTitle == "") ? "Windows 桌面 / 壁纸层" : "文件管理器: " . currentTitle
                    } else if (currentProcess == "SystemSettings.exe") {
                        currentTitle := "Windows 系统设置中心"
                    } else if (currentProcess == "Taskmgr.exe") {
                        currentTitle := "Windows 任务管理器"
                    } else if (currentProcess == "cmd.exe" || currentProcess == "powershell.exe") {
                        currentTitle := "系统控制台终端: " . currentTitle
                    } else if (currentProcess == "LockApp.exe" || currentProcess == "LogonUI.exe") {
                        ; 精确清洗 Windows 锁屏阶段，防止锁屏后的数据污染
                        currentProcess := "System_LockScreen"
                        currentTitle := "🖥️ 操作系统处于锁屏/登录状态"
                    }
                }
            }
        } catch {
            currentProcess := "Unknown"
            currentTitle := "无法获取聚焦窗口"
        }
    }
    
    tracker.Step(A_NowUTC, DllCall("GetTickCount64", "UInt64"), currentProcess, currentTitle, maxIdleTime // 1000)
}

WriteAhkHeartbeat() {
    global ahkHeartbeatPath
    heartbeatTemp := ahkHeartbeatPath . "." . DllCall("GetCurrentProcessId") . ".tmp"
    try {
        if FileExist(heartbeatTemp)
            FileDelete(heartbeatTemp)
        FileAppend(A_NowUTC, heartbeatTemp, "UTF-8-RAW")
        FileMove(heartbeatTemp, ahkHeartbeatPath, 1)
    }
}

WriteAhkHealth() {
    global tracker, healthPath, captureErrors, captureFailing, spool
    status := tracker.pending.Length || tracker.overflowStart != "" || captureFailing ? "degraded" : "healthy"
    payload := '{"schema":"timeaudit.ahk-health.v1","state":"' . status
        . '","pending_events":' . tracker.pending.Length
        . ',"pending_chars":' . tracker.pendingChars
        . ',"write_failures":' . tracker.writeFailures
        . ',"overflow_events":' . tracker.overflowEvents
        . ',"clock_adjustments":' . tracker.clockAdjustments
        . ',"capture_errors":' . captureErrors
        . ',"committed_until_utc":"' . tracker.committedUntil
        . '","published_segments":' . spool.sequence . '}'
    temp := healthPath . ".tmp"
    try {
        if FileExist(temp)
            FileDelete(temp)
        FileAppend(payload, temp, "UTF-8-RAW")
        FileMove(temp, healthPath, 1)
    }
}

; Kept for source consumers: new records are UTC, including across timezone changes.
GetUtcOffsetSuffix() => "+0000"

WindowPowerEventHook(wParam, lParam, *) {
    Critical(true)
    global tracker, suspended, isScreenOff, GUID_CONSOLE_DISPLAY_STATE
    if (wParam = 4) { ; PBT_APMSUSPEND is evidence, a timer stall is not.
        tracker.Step(A_NowUTC, DllCall("GetTickCount64", "UInt64"), "System_Sleep", "Windows suspend notification")
        suspended := true
    } else if (wParam = 18 || wParam = 7) {
        tracker.Step(A_NowUTC, DllCall("GetTickCount64", "UInt64"), "", "")
        suspended := false
        SetTimer(CaptureActiveWindow, -1, 1)
    } else if (wParam = 0x8013 && lParam) {
        if (NumGet(lParam, 0, "UInt64") = NumGet(GUID_CONSOLE_DISPLAY_STATE, 0, "UInt64")
            && NumGet(lParam, 8, "UInt64") = NumGet(GUID_CONSOLE_DISPLAY_STATE, 8, "UInt64")
            && NumGet(lParam, 16, "UInt") >= 4)
            isScreenOff := NumGet(lParam, 20, "UInt") = 0
    }
    return true
}

SessionEventHook(wParam, *) {
    global isLocked
    if (wParam = 7)
        isLocked := true
    else if (wParam = 8)
        isLocked := false
}

SafeExitHandler(ExitReason, *) {
    Critical(true)
    global tracker, hPowerNotify, sessionNotify
    SetTimer(CaptureActiveWindow, 0)
    tracker.Finish(A_NowUTC, DllCall("GetTickCount64", "UInt64"))
    WriteAhkHealth()
    if hPowerNotify
        DllCall("UnregisterPowerSettingNotification", "Ptr", hPowerNotify)
    if sessionNotify
        DllCall("Wtsapi32\WTSUnRegisterSessionNotification", "Ptr", A_ScriptHwnd)
    return 0
}

IsSystemAudioPlaying() {
    static IID_IAudioMeterInformation := "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
    static audioMeter := ""
    static tickCount := 0
    try {
        tickCount++
        ; 每 30 次调用（约 60 秒）强行清空一次句柄，确保音频输出物理设备切换时完美重连自愈
        if (tickCount > 30) {
            audioMeter := ""
            tickCount := 0
        }

        if (audioMeter == "") {
            rawPtr := SoundGetInterface(IID_IAudioMeterInformation)
            if (rawPtr) {
                ; 包装为 IUnknown (13) 智能 COM 包装器，由 AHK 垃圾回收器自动回收，避免引用计数泄露
                audioMeter := ComValue(13, rawPtr)
            }
        }
        if (audioMeter) {
            peak := 0.0
            ; 接口索引 3 对应 IAudioMeterInformation::GetPeakValue 
            ComCall(3, audioMeter, "float*", &peak)
            return peak > 0.001
        }
    } catch {
        audioMeter := ""
        tickCount := 0
    }
    return false
}
