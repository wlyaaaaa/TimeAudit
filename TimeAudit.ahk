#Requires AutoHotkey v2.0
Persistent

; === 🛡️ 工业级单流状态机配置 ===
global lastProcess := ""
global lastTitle := ""
global startTime := A_Now           ; 统一采用最直观的本地时间
global lastFiredTime := A_Now       ; 现实世界绝对时间心跳锚点
global isScreenOff := false         ; 显式时区与显示器物理信号标志位
global logPath := "E:\TimeAudit\log\buffer.csv"
global maxUnwrittenDuration := 300  ; 最大未写入缓冲时间（秒）
global maxIdleTime := 60000         ; 【优化】：用户暂离判定阈值，已修改为 60000 毫秒（60秒 / 1分钟）
global hPowerNotify := 0            ; 显式固化电源通知句柄存根，防止内存与内核句柄泄漏

; 确保高频缓冲区目录存在
if !DirExist("E:\TimeAudit\log")
    DirCreate("E:\TimeAudit\log")

; 注册系统事件钩子
OnExit(SafeExitHandler)
OnMessage(0x0218, WindowPowerEventHook)

; 📡 链式单次 NumPut，合并向 Windows 内核注册的 GUID 结构体内存初始化
global GUID_CONSOLE_DISPLAY_STATE := Buffer(16)
NumPut(
    "UInt", 0x6FE69556, 
    "UShort", 0x704A, 
    "UShort", 0x47A0, 
    "UChar", 0x8F, "UChar", 0x24, "UChar", 0xC2, "UChar", 0x8D,
    "UChar", 0x93, "UChar", 0x6F, "UChar", 0xDA, "UChar", 0x47,
    GUID_CONSOLE_DISPLAY_STATE, 0
)

; 存储句柄，以便退出时能在内核彻底注销，防止多次重启脚本后内核队列混乱
hPowerNotify := DllCall("RegisterPowerSettingNotification", "Ptr", A_ScriptHwnd, "Ptr", GUID_CONSOLE_DISPLAY_STATE, "UInt", 0, "Ptr")

; 启动探针（使用单次高优先级定时器，避免重入和文件写入阻塞导致的线程堆积）
SetTimer(CaptureActiveWindow, -2000, 1)

CaptureActiveWindow() {
    global lastProcess, lastTitle, startTime, lastFiredTime, isScreenOff, maxIdleTime, maxUnwrittenDuration
    static ProcessCache := Map() ; 窗口句柄进程名高速本地缓存，避免高频跨进程内核调用
    
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
        SetTimer(CaptureActiveWindow, -2000, 1) ; 重新订阅下一个单次心跳
        return
    }
    lastFiredTime := A_Now
    
    ; 2. 【层级漏斗状态机】动态判定当前这一秒电脑到底属于什么工时状态
    currentProcess := ""
    currentTitle := ""
    
    ; 暂离检测级联：若键鼠静止超设定期限，但系统输出通道有音频振幅（看视频/开会），则豁免暂离状态
    isIdleState := (A_TimeIdlePhysical >= maxIdleTime)
    if (isIdleState && IsSystemAudioPlaying()) {
        isIdleState := false
    }
    
    if (isScreenOff) {
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
                    if ProcessCache.Has(activeHWND) {
                        currentProcess := ProcessCache[activeHWND]
                    } else {
                        currentProcess := WinGetProcessName(activeHWND)
                        ; 内存防爆：限制高速缓存池上限，定期自动熔断
                        if (ProcessCache.Count > 200) {
                            ProcessCache.Clear()
                        }
                        ; 【修复】：由错误的比较符号 = 修正为赋值符号 :=，使进程哈希缓存机制真正并网生效
                        ProcessCache[activeHWND] := currentProcess
                    }
                    
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
    
    ; 3. 【统一排他流水线】负责无缝处理状态切换与无损落盘
    duration := DateDiff(A_Now, startTime, "Seconds")
    
    if (currentProcess != lastProcess || currentTitle != lastTitle || duration >= maxUnwrittenDuration) {
        if (lastProcess != "") {
            ; 将毫秒阈值转换为秒，动态计算与顶部配置绝对对齐的回溯秒数
            idleSeconds := maxIdleTime // 1000  
            
            if (currentProcess == "System_Idle" && lastProcess != "System_Idle" && lastProcess != "System_DisplayOff" && lastProcess != "System_Sleep") {
                actualAppDuration := duration - idleSeconds
                if (actualAppDuration >= 2) {
                    CommitToDisk(lastProcess, lastTitle, startTime, actualAppDuration)
                }
                startTime := DateAdd(A_Now, -idleSeconds, "Seconds") ; 【修复】：完美吃下 60 秒暂离静止期，时间轴严密咬合
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

    SetTimer(CaptureActiveWindow, -2000, 1) ; 动态排队，完全消除多轮心跳并发重叠隐患
}

; 🔒 防崩溃原子化写入管道（强锁 +08 显式时区）
CommitToDisk(proc, title, sTime, duration, forceWrite := false) {
    if (duration < 2) 
        return
        
    formattedTime := FormatTime(sTime, "yyyy-MM-dd HH:mm:ss") . "+08"
    
    ; 核心格式化安全清洗：合并串联式清洗，安全剥离所有换行符并实现双引号安全转义，保持原始输出结构不发生改变
    cleanTitle := StrReplace(RegExReplace(title, "[\r\n]+", " "), '"', '""')
    
    logLine := '"' . formattedTime . '",' . duration . ',"' . proc . '","' . cleanTitle . '"`n'
    
    if (forceWrite) {
        ; 如果是退出/关机阶段的数据打捞，强制执行单次同步物理直写，绕过重试延迟
        try {
            FileAppend(logLine, logPath, "UTF-8")
        }
        return
    }
    
    loop 3 {
        try {
            FileAppend(logLine, logPath, "UTF-8")
            break
        } catch {
            Sleep(50) 
        }
    }
}

; 📡 Windows 原生 COM 接口主输出振幅检测
; 【自愈型高精度物理防打扰】：基于 AHK 原生 ComValue(13) 包装垃圾回收机制，彻底封死因热插拔可能导致的内核内存泄漏
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

; 📡 Windows 电源广播异步钩子（使用 * 省略未用形参，提升 AHK 内部消息分发性能）
WindowPowerEventHook(wParam, lParam, *) {
    global isScreenOff
    
    if (wParam == 0x8013) { ; PBT_POWERSETTINGCHANGE
        ; 只拦截真正的显示器状态变化，防止插拔电源干扰
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

; ⚙️ 关机/退出断头数据打捞与内核清理（使用 * 省略未用形参，提升退出响应）
SafeExitHandler(ExitReason, *) {
    global lastProcess, lastTitle, startTime, hPowerNotify
    
    ; 识别是否属于系统关机或注销，使用强力无锁直写确保落盘
    isSystemShutdown := (ExitReason == "Shutdown" || ExitReason == "Logoff")
    
    ; 1. 紧急打捞未落盘数据
    if (lastProcess != "") {
        duration := DateDiff(A_Now, startTime, "Seconds")
        CommitToDisk(lastProcess, lastTitle, startTime, duration, isSystemShutdown)
    }
    
    ; 2. 显式向 Windows 内核注销电源通知句柄，释放系统底层链条，确保系统干净稳定
    if (hPowerNotify) {
        DllCall("UnregisterPowerSettingNotification", "Ptr", hPowerNotify)
        hPowerNotify := 0
    }
    return 0 
}