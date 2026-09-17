#Requires AutoHotkey v2.0

; Pure interval state plus an injectable atomic spool writer. All times are UTC.
class TimeAuditState {
    __New(writer, nowUtc, tickMs, maxEvents := 2048, maxChars := 2097152) {
        this.writer := writer
        this.startUtc := nowUtc
        this.lastUtc := nowUtc
        this.lastTick := tickMs
        this.process := ""
        this.title := ""
        this.pending := []
        this.pendingChars := 0
        this.maxEvents := maxEvents
        this.maxChars := maxChars
        this.overflowStart := ""
        this.overflowEnd := ""
        this.writeFailures := 0
        this.overflowEvents := 0
        this.clockAdjustments := 0
        this.clockRecovery := false
        this.committedUntil := nowUtc
    }

    Step(nowUtc, tickMs, process, title, idleSeconds := 0) {
        ; A backward clock must not overwrite intervals already assigned to UTC.
        if (nowUtc < this.lastUtc) {
            if !this.clockRecovery
                this.clockAdjustments += 1
            this.clockRecovery := true
            this.lastTick := tickMs
            this.Flush()
            return
        }
        elapsed := DateDiff(nowUtc, this.lastUtc, "Seconds")
        monotonicElapsed := Max(0, (tickMs - this.lastTick) / 1000)
        clockChanged := this.clockRecovery || Abs(elapsed - monotonicElapsed) > 3
        if clockChanged && !this.clockRecovery
            this.clockAdjustments += 1
        this.clockRecovery := false

        if ((elapsed > 6 || clockChanged) && this.process != "System_Sleep") {
            this.Append(this.process, this.title, this.startUtc, this.lastUtc)
            this.Append("System_CollectionGap", "Collection gap; cause not established", this.lastUtc, nowUtc)
            this.startUtc := nowUtc
            this.process := ""
            this.title := ""
        }

        duration := DateDiff(nowUtc, this.startUtc, "Seconds")
        if (process != this.process || title != this.title || duration >= 30) {
            boundary := nowUtc
            if (process = "System_Idle" && this.process != "" && this.process != "System_Idle"
                && this.process != "System_DisplayOff" && this.process != "System_LockScreen"
                && this.process != "System_Sleep") {
                boundary := Max(this.startUtc, DateAdd(nowUtc, -idleSeconds, "Seconds"))
            }
            this.Append(this.process, this.title, this.startUtc, boundary)
            this.startUtc := boundary
            this.process := process
            this.title := title
        }
        this.lastUtc := nowUtc
        this.lastTick := tickMs
        this.Flush()
    }

    Append(process, title, beginUtc, endUtc) {
        if (process = "" || endUtc <= beginUtc)
            return
        event := {process: process, title: title, beginUtc: beginUtc, endUtc: endUtc}
        size := StrLen(process) + StrLen(title) + 100
        if (this.overflowStart != "" || this.pending.Length >= this.maxEvents || this.pendingChars + size > this.maxChars) {
            ; Preserve the time range, not an unbounded queue of private titles.
            ; Loss of fine detail is explicit and never attributed to an app.
            if (this.overflowStart = "")
                this.overflowStart := beginUtc
            this.overflowEnd := Max(this.overflowEnd = "" ? beginUtc : this.overflowEnd, endUtc)
            this.overflowEvents += 1
            return
        }
        this.pending.Push(event)
        this.pendingChars += size
    }

    Flush() {
        if (!this.pending.Length && this.overflowStart = "")
            return true
        batch := this.pending.Clone()
        if (this.overflowStart != "")
            batch.Push({process: "System_CollectionGap", title: "Persistence backlog exceeded; interval detail unavailable", beginUtc: this.overflowStart, endUtc: this.overflowEnd})
        ok := false
        try ok := this.writer.Call(batch)
        if !ok {
            this.writeFailures += 1
            return false
        }
        this.committedUntil := batch[-1].endUtc
        this.pending := []
        this.pendingChars := 0
        this.overflowStart := ""
        this.overflowEnd := ""
        return true
    }

    Finish(nowUtc, tickMs) {
        this.Step(nowUtc, tickMs, "", "")
        return this.Flush()
    }
}

class TimeAuditSpool {
    __New(basePath) {
        this.basePath := basePath
        this.instance := A_NowUTC . "." . DllCall("GetCurrentProcessId") . "." . DllCall("GetTickCount64", "UInt64")
        this.sequence := 0
    }

    Write(events) {
        payload := ""
        for event in events {
            stamp := FormatTime(event.beginUtc, "yyyy-MM-dd HH:mm:ss") . "+0000"
            duration := DateDiff(event.endUtc, event.beginUtc, "Seconds")
            process := StrReplace(RegExReplace(event.process, "[\r\n]+", " "), '"', '""')
            title := StrReplace(RegExReplace(event.title, "[\r\n]+", " "), '"', '""')
            payload .= '"' . stamp . '",' . duration . ',"' . process . '","' . title . '"`n'
        }
        target := this.basePath . ".ahk." . this.instance . "." . this.sequence . ".processing"
        temporary := target . ".tmp"
        stream := ""
        try {
            if FileExist(target) {
                if (FileRead(target, "UTF-8") != payload)
                    return false
            } else {
                stream := FileOpen(temporary, "w", "UTF-8-RAW")
                bytes := StrPut(payload, "UTF-8") - 1
                data := Buffer(bytes + 1)
                StrPut(payload, data, "UTF-8")
                if (stream.RawWrite(data, bytes) != bytes)
                    throw Error("short_write")
                if !DllCall("FlushFileBuffers", "Ptr", stream.Handle, "Int")
                    throw Error("flush_failed")
                stream.Close()
                stream := ""
                FileMove(temporary, target, 0)
            }
            this.sequence += 1
            return true
        } catch {
            if IsObject(stream)
                stream.Close()
            return false
        }
    }
}