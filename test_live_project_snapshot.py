import datetime as dt
from subprocess import CalledProcessError, CompletedProcess

from live_project_snapshot import daily_records
import live_project_snapshot as live
import timeaudit_diagnostic_summary as summary
from test_timeaudit_diagnostic_summary import aggregate_fixture


def test_public_artifact_names_the_json_definition(tmp_path, monkeypatch):
    folder = tmp_path / 'grafana_dashboards'
    folder.mkdir()
    live.write(folder / 'board.json', dict(uid='board', title='合成看板', panels=[]))
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    monkeypatch.setattr(live.subprocess, 'check_output', lambda *a, **k: '2026-11-15T10:00:00+08:00')
    rows, artifacts = live.dashboard_records()
    assert artifacts[0]['title'] == '看板定义 JSON · 合成看板'
    assert artifacts[0]['open']['href'].endswith('/grafana_dashboards/board.json')
    assert rows[0]['definition_href'] == artifacts[0]['open']['href']
    assert rows[0]['local_open'] == dict(value=None, visibility='owner', guard='owner', hidden_what='本机看板地址')


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
    assert result[-1]["until"] == until.isoformat()
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
    earliest = dict(text="活动 2026-06-08；窗口 2026-06-17", activity="2026-06-08", window="2026-06-17")
    assert live.read(retained)["metrics"] == dict(hardware=456, lifecycle=456, registry=456, earliest=earliest)
    assert all(live.read(retained)["times"].values())


def test_static_retains_each_old_measurement_only_when_its_read_fails(tmp_path, monkeypatch):
    config, output, retained = (tmp_path/name for name in ("config.json", "source.json", "retained.json"))
    live.write(config, dict(retained_output=str(retained), static_output=str(output)))
    prior = dict(metrics=dict(hardware=123, lifecycle=77, registry=0),
                 times=dict(hardware="2026-11-16T01:00:00+08:00", registry="2026-11-15T02:00:00+08:00"))
    live.write(retained, prior)
    monkeypatch.setattr(live, "source_size", lambda: dict(docs_chars=12, code_lines=34))

    def get_rows(sql, *args):
        if "fact_system_hardware" in sql or "fact_process_lifecycle_events" in sql:
            raise TimeoutError
        if "dim_process_registry" in sql:
            raise OSError
        return [["2026-06-07", "2026-06-16"]]

    monkeypatch.setattr(live, "query", get_rows)
    live.export_snapshot(None, None, None, output, static=True, config_path=config)
    result = live.read(retained)
    earliest = dict(text="活动 2026-06-07；窗口 2026-06-16", activity="2026-06-07", window="2026-06-16")
    assert result["metrics"] == dict(hardware=123, lifecycle=None, registry=0, earliest=earliest)
    assert {key: result["times"][key] for key in prior["times"]} == prior["times"]
    assert result["times"]["lifecycle"] is None
    assert result["scopes"]["hardware"] == "本次读取超时；沿用上次读数"
    assert result["scopes"]["lifecycle"] == "本次读取超时"
    assert result["scopes"]["registry"] == "取数失败；沿用上次读数"
    assert result["times"]["earliest"]
    assert not live.read(output)["metrics"]


def test_static_count_retries_a_lost_database_connection(monkeypatch):
    calls = []

    def get_rows(sql, timeout):
        calls.append((sql, timeout))
        if len(calls) == 1:
            raise CalledProcessError(2, 'psql')
        return [456]

    monkeypatch.setattr(live, 'query', get_rows)
    assert live.retained_count('fact_process_lifecycle_events') == 456
    assert calls == [('SELECT count(*) FROM public.fact_process_lifecycle_events;', 300)] * 2


def test_live_snapshot_preserves_other_values_when_sources_are_missing(tmp_path, monkeypatch):
    config, output, mirror, owner = (tmp_path / name for name in ("config.json", "output.json", "live.json", "owner.json"))
    live.write(config, dict(live_output=str(mirror), owner_output=str(owner),
                           clipboard_db=str(tmp_path / "missing.sqlite"), grafana_url="http://localhost"))
    raw = aggregate_fixture()
    raw.update(main_working_set_mib_max=100.0, hardware_sample_count=3,
               distinct_sample_seconds=3, requested_window_seconds=86400)
    monkeypatch.setattr(summary, "query_aggregate", lambda *args, **kwargs: raw)
    monkeypatch.setattr(live, "dashboard_records", lambda: ([], []))
    monkeypatch.setattr(live.subprocess, "run", lambda *args, **kwargs: CompletedProcess(
        args=[], returncode=3, stdout="", stderr="psql:<stdin>:1: ERROR:  57014\n"))
    query_database = live.query

    def query(sql, timeout):
        if "pg_database_size" in sql:
            return query_database(sql, timeout)
        if "fact_process_context" in sql:
            return [[12, 34, None]]
        return []

    monkeypatch.setattr(live, "query", query)
    assert summary.main(["--hours", "24", "--live-output", str(output), "--live-config", str(config)]) == 0
    assert live.read(output) == live.read(mirror)
    for path in (mirror, owner):
        snapshot = live.read(path)
        assert snapshot["metrics"]["raw_since"] is snapshot["times"]["raw_since"] is None
        assert snapshot["metrics"]["db_bytes"] is None
        assert snapshot["scopes"]["db_bytes"] == "本次读取超时"
        assert snapshot["metrics"]["activity"] == 34
        assert (snapshot["metrics"]["samples24"], snapshot["metrics"]["empty24"]) == (3, 86397)
        assert snapshot["metrics"]["memory"] == 104.8576
