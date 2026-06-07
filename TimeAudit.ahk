#Requires AutoHotkey v2.0
Persistent

; === 🛡️ 工业级单流状态机配置 ===
global lastProcess := ""
global lastTitle := ""
global startTime := A_Now           ; 统一采用最直观的本地时间
global lastFiredTime := A_Now       ; 现实世界绝对时间心跳锚点
global isScreenOff := false         ; 显式时区与显示器物理信号标志位
global logPath := "E:\TimeAudit\log\buffer.csv"
global maxUnwrittenDuration := 300
global hPowerNotify := 0            ; 显式固化电源通知句柄存根，防止内存与内核句柄泄漏

; 确保高频缓冲区目录存在
if !DirExist("E:\TimeAudit\log")
    DirCreate("E:\TimeAudit\log")

; 注册系统事件钩子
OnExit(SafeExitHandler)
OnMessage(0x0218, WindowPowerEventHook)

; 📡 【核心修复】向 Windows 内核订阅显示器供电状态
global GUID_CONSOLE_DISPLAY_STATE := Buffer(16)
NumPut("UInt", 0x6FE69556, GUID_CONSOLE_DISPLAY_STATE, 0)
NumPut("UShort", 0x704A, GUID_CONSOLE_DISPLAY_STATE, 4)
NumPut("UShort", 0x47A0, GUID_CONSOLE_DISPLAY_STATE, 6)
NumPut("UChar", 0x8F, GUID_CONSOLE_DISPLAY_STATE, 8)
NumPut("UChar", 0x24, GUID_CONSOLE_DISPLAY_STATE, 9)
NumPut("UChar", 0xC2, GUID_CONSOLE_DISPLAY_STATE, 10)
NumPut("UChar", 0x8D, GUID_CONSOLE_DISPLAY_STATE, 11)
NumPut("UChar", 0x93, GUID_CONSOLE_DISPLAY_STATE, 12)
NumPut("UChar", 0x6F, GUID_CONSOLE_DISPLAY_STATE, 13)
NumPut("UChar", 0xDA, GUID_CONSOLE_DISPLAY_STATE, 14)
NumPut("UChar", 0x47, GUID_CONSOLE_DISPLAY_STATE, 15)

; 存储句柄，以便退出时能在内核彻底注销，防止多次重启脚本后内核队列混乱
hPowerNotify := DllCall("RegisterPowerSettingNotification", "Ptr", A_ScriptHwnd, "Ptr", GUID_CONSOLE_DISPLAY_STATE, "UInt", 0, "Ptr")

; 启动探针（每 2 秒高密切片一次）
SetTimer(CaptureActiveWindow, 2000)

CaptureActiveWindow() {
    global lastProcess, lastTitle, startTime, logPath, maxUnwrittenDuration, lastFiredTime, isScreenOff
    
    ; 1. 【最高优先级】真硬件睡眠/挂起打捞算法
    actualElapsed := DateDiff(A_Now, lastFiredTime, "Seconds")
    if (actualElapsed > 6) {
        preSleepDuration := DateDiff(lastFiredTime, startTime, "Seconds")
        if (lastProcess != "" && preSleepDuration >= 2) {
            CommitToDisk(lastProcess, lastTitle, startTime, preSleepDuration)
        }
        CommitToDisk("System_Sleep", "⚙️ 操作系统进入深睡眠/休眠状态 (风扇停转)", lastFiredTime, actualElapsed)
        startTime := A_Now
        lastFiredTime := A_Now
        isScreenOff := false 
        return
    }
    lastFiredTime := A_Now
    
    ; 2. 【层级漏斗状态机】动态判定当前这一秒电脑到底属于什么工时状态
    currentProcess := ""
    currentTitle := ""
    
    if (isScreenOff) {
        currentProcess := "System_DisplayOff"
        currentTitle := "🖥️ 显示器已熄灭 / 操作系统处于伪睡眠或锁屏状态"
    } 
    else if (A_TimeIdlePhysical >= 180000) {
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
                    currentProcess := WinGetProcessName(activeHWND)
                    currentTitle := WinGetTitle(activeHWND)
                    
                    ; 📊 系统页面组件高精准排他性清洗（完整保留，无阉割）
                    if (currentProcess == "explorer.exe") {
                        currentTitle := (currentTitle == "") ? "Windows 桌面 / 壁纸层" : "文件管理器: " . currentTitle
                    } else if (currentProcess == "SystemSettings.exe") {
                        currentTitle := "Windows 系统设置中心"
                    } else if (currentProcess == "Taskmgr.exe") {
                        currentTitle := "Windows 任务管理器"
                    } else if (currentProcess == "cmd.exe" || currentProcess == "powershell.exe") {
                        currentTitle := "系统控制台终端: " . currentTitle
                    }
                }
            }
        } catch {
            currentProcess := "Unknown"
            currentTitle := "无法获取聚焦窗口"
        }
    }
    
    ; 3. 【统一排他流水线】负责无缝处理状态切换与无损落盘
    duration := DateDiff(A_Now, startTime, "Seconds")
    
    if (currentProcess != lastProcess || currentTitle != lastTitle || duration >= maxUnwrittenDuration) {
        if (lastProcess != "") {
            ; ⚡ 漏洞修正：必须加持边界判定，防止长周期挂机超时强制落盘时二次扣时间
            if (currentProcess == "System_Idle" && lastProcess != "System_Idle" && lastProcess != "System_DisplayOff" && lastProcess != "System_Sleep") {
                actualAppDuration := duration - 180
                if (actualAppDuration >= 2) {
                    CommitToDisk(lastProcess, lastTitle, startTime, actualAppDuration)
                }
                startTime := DateAdd(A_Now, -180, "Seconds") ; 完美吃下3分钟静止期
            } 
            else {
                if (duration >= 2) {
                    CommitToDisk(lastProcess, lastTitle, startTime, duration)
                }
                startTime := A_Now
            }
        } else {
            startTime := A_Now
        }
        
        lastProcess := currentProcess
        lastTitle := currentTitle
    }
}

; 🔒 防崩溃原子化写入管道（强锁 +08 显式时区）
CommitToDisk(proc, title, sTime, duration) {
    global logPath
    if (duration < 2) 
        return
        
    formattedTime := FormatTime(sTime, "yyyy-MM-dd HH:mm:ss") . "+08"
    cleanTitle := StrReplace(title, '"', '""')
    logLine := '"' . formattedTime . '",' . duration . ',"' . proc . '","' . cleanTitle . '"`n'
    
    loop 3 {
        try {
            FileAppend(logLine, logPath, "UTF-8")
            break
        } catch {
            Sleep(50) 
        }
    }
}

; 📡 Windows 电源广播异步钩子
WindowPowerEventHook(wParam, lParam, msg, hwnd) {
    global lastProcess, lastTitle, startTime, isScreenOff, GUID_CONSOLE_DISPLAY_STATE
    
    if (wParam == 0x8013) { ; PBT_POWERSETTINGCHANGE
        ; ⚡ 【完整保留核心校验】只拦截真正的显示器状态变化，防止插拔电源干扰
        guidPart1 := NumGet(lParam, 0, "UInt64")
        guidPart2 := NumGet(lParam, 8, "UInt64")
        myGuidPart1 := NumGet(GUID_CONSOLE_DISPLAY_STATE, 0, "UInt64")
        myGuidPart2 := NumGet(GUID_CONSOLE_DISPLAY_STATE, 8, "UInt64")
         
        if (guidPart1 == myGuidPart1 && guidPart2 == myGuidPart2) { 
            monitorState := NumGet(lParam, 20, "UInt") ; 0=Off, 1=On, 2=Dimmed
            if (monitorState == 0) {
                isScreenOff := true
            } 
            else if (monitorState == 1 || monitorState == 2) { 
                isScreenOff := false
            }
        }
    }
    return true
}

; ⚙️ 关机/退出断头数据打捞与内核清理
SafeExitHandler(ExitReason, ExitCode) {
    global lastProcess, lastTitle, startTime, hPowerNotify
    
    ; 1. 紧急打捞未落盘数据
    if (lastProcess != "") {
        duration := DateDiff(A_Now, startTime, "Seconds")
        CommitToDisk(lastProcess, lastTitle, startTime, duration)
    }
    
    ; 2. 显式向 Windows 内核注销电源通知句柄，释放系统底层链条，确保系统干净稳定
    if (hPowerNotify) {
        DllCall("UnregisterPowerSettingNotification", "Ptr", hPowerNotify)
        hPowerNotify := 0
    }
    return 0 
}