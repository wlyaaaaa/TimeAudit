import datetime as dt

from live_project_snapshot import daily_records
import live_project_snapshot as live


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


def test_static_keeps_bytes_and_mtime_while_retained_facts_refresh(tmp_path, monkeypatch):
    config, output = tmp_path/"config.json", tmp_path/"static.json"
    retained, mirror = tmp_path/"retained.json", tmp_path/"mirror.json"
    live.write(config, dict(retained_output=str(retained), static_output=str(mirror)))
    monkeypatch.setattr(live, "source_size", lambda: dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), docs_chars=12, code_lines=34))
    monkeypatch.setattr(live, "query", lambda sql, *args: [["2026-06-07", "2026-06-16"]] if "json_build_array" in sql else [123])
    live.export_snapshot(None, None, None, output, static=True, config_path=config)
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (output, mirror)]
    monkeypatch.setattr(live, "query", lambda sql, *args: [["2026-06-08", "2026-06-17"]] if "json_build_array" in sql else [456])
    live.export_snapshot(None, None, None, output, static=True, config_path=config)
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in (output, mirror)]
    assert live.read(output)["metrics"] == live.read(output)["times"] == live.read(output)["scopes"] == {}
    assert live.read(retained)["metrics"] == dict(hardware=456, lifecycle=456, registry=456, earliest="活动 2026-06-08；窗口 2026-06-17")
    assert all(live.read(retained)["times"].values())
