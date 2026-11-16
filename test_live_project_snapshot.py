import datetime as dt

from live_project_snapshot import daily_records


def test_daily_records_clip_and_union_overlap_across_beijing_midnight():
    until = dt.datetime.fromisoformat("2026-11-16T02:00:00+08:00")
    rows = [
        ["2026-11-15T23:30:00+08:00", "2026-11-16T01:00:00+08:00", False],
        ["2026-11-16T00:30:00+08:00", "2026-11-16T01:30:00+08:00", False],
        ["2026-11-16T01:00:00+08:00", "2026-11-16T01:15:00+08:00", True],
        ["2026-11-16T01:10:00+08:00", "2026-11-16T01:20:00+08:00", True],
    ]
    result = daily_records(rows, until)
    assert len(result) == 7
    assert result[-1]["missed_seconds"] == 1800+1200
    assert result[-1]["intervals"] == 4
    assert result[-2]["missed_seconds"] == 86400-1800
    assert result[0]["missed_seconds"] == 86400
    assert daily_records([], until)[-1]["missed_seconds"] == 7200
