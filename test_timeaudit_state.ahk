#Requires AutoHotkey v2.0
#SingleInstance Off
#Include %A_ScriptDir%\timeaudit_state.ahk

class TestSink {
    __New() {
        this.fail := false
        this.events := []
    }
    Write(events) {
        if this.fail
            return false
        for event in events
            this.events.Push(event.Clone())
        return true
    }
}
Assert(value, message) {
    if !value
        throw Error(message)
}
At(seconds) => DateAdd("20260101000000", seconds, "Seconds")
CheckIntervals(events) {
    previous := "20260101000000"
    for event in events {
        Assert(event.beginUtc >= previous, "overlap")
        Assert(event.endUtc > event.beginUtc, "nonpositive_duration")
        previous := event.endUtc
    }
}
try {
    sink := TestSink()
    state := TimeAuditState(ObjBindMethod(sink, "Write"), At(0), 0)
    loop 51 {
        second := (A_Index - 1) * 2
        state.Step(At(second), second * 1000, second >= 100 ? "System_Idle" : "app", second < 80 ? "first" : "second", 60)
    }
    state.Finish(At(104), 104000)
    CheckIntervals(sink.events)
    Assert(sink.events[-1].beginUtc >= At(80), "idle_rewound_committed_time")

    sink := TestSink()
    state := TimeAuditState(ObjBindMethod(sink, "Write"), At(0), 0)
    state.Step(At(0), 0, "app", "title")
    state.Step(At(2), 2000, "app", "title")
    state.Step(At(10), 10000, "app", "title")
    Assert(sink.events[-1].process = "System_CollectionGap", "stall_became_sleep")
    state.Step(At(12), 12000, "System_Sleep", "Windows suspend notification")
    state.Step(At(100), 100000, "", "")
    Assert(sink.events[-1].process = "System_Sleep", "real_sleep_missing")
    CheckIntervals(sink.events)

    sink := TestSink()
    sink.fail := true
    state := TimeAuditState(ObjBindMethod(sink, "Write"), At(0), 0, 2)
    loop 6 {
        second := (A_Index - 1) * 2
        state.Step(At(second), second * 1000, "app", "title" . A_Index)
    }
    Assert(state.committedUntil = At(0), "failed_write_advanced_commit")
    Assert(state.pending.Length <= 2, "unbounded_queue")
    Assert(state.overflowEvents > 0, "overflow_not_visible")
    sink.fail := false
    state.Step(At(12), 12000, "app", "restored")
    Assert(state.pending.Length = 0, "retry_did_not_drain")
    Assert(sink.events[-1].process = "System_CollectionGap", "overflow_mislabelled")
    CheckIntervals(sink.events)

    sink := TestSink()
    state := TimeAuditState(ObjBindMethod(sink, "Write"), At(0), 0)
    state.Step(At(0), 0, "app", "a")
    state.Step(At(2), 2000, "app", "b")
    state.Step(At(1), 4000, "app", "c")
    state.Step(At(4), 6000, "app", "d")
    Assert(state.clockAdjustments > 0, "clock_change_not_visible")
    CheckIntervals(sink.events)

    if A_Args.Length {
        directory := A_Args[1]
        DirCreate(directory)
        spool := TimeAuditSpool(directory . "\buffer.csv")
        Assert(spool.Write([{process: "app", title: 'quote " and 中文', beginUtc: At(0), endUtc: At(2)}]), "atomic_spool_failed")
        count := 0
        loop files directory . "\*.processing" {
            count += 1
            text := FileRead(A_LoopFileFullPath, "UTF-8")
            Assert(InStr(text, "+0000"), "utc_missing")
            Assert(InStr(text, 'quote "" and 中文'), "csv_escape_failed")
        }
        Assert(count = 1, "wrong_segment_count")
        bad := TimeAuditSpool(directory . "\does-not-exist\buffer.csv")
        Assert(!bad.Write([{process: "app", title: "x", beginUtc: At(0), endUtc: At(2)}]), "failed_write_not_reported")
    }
    FileAppend('{"tests":6,"status":"pass","engine":"native-ahk-v2"}', "*", "UTF-8")
    ExitApp(0)
} catch as err {
    FileAppend("FAIL " . err.Message . " at " . err.Line, "*", "UTF-8")
    ExitApp(1)
}