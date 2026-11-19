import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, main

from live_usage import apply_power, read_focus, read_power


class LiveUsageTest(TestCase):
    def test_clean_pair_can_fill_only_an_unknown_gap(self):
        start = datetime(2026, 11, 19, tzinfo=timezone.utc)
        end = start + timedelta(minutes=10)
        stamp = lambda n: start.timestamp() + n
        event = lambda n, k: dict(at=(start + timedelta(seconds=n)).isoformat(), id=k)
        rows = [event(500, 41), event(410, 12), event(380, 13), event(210, 12), event(190, 13), event(50, 12)]
        source = read_power(start, end, lambda *_: rows)
        self.assertEqual(source, dict(spans=[(stamp(190), stamp(210))], markers=[stamp(50), stamp(410)]))
        usage = [dict(start=stamp(0), end=stamp(175), state=None),
                 dict(start=stamp(175), end=stamp(195), state='睡眠'),
                 dict(start=stamp(195), end=stamp(600), state=None)]
        result = apply_power(usage, source)
        self.assertEqual([(r['start'], r['end'], r['state']) for r in result],
                         [(stamp(0), stamp(175), None), (stamp(175), stamp(195), '睡眠'),
                          (stamp(195), stamp(210), '关机离线'), (stamp(210), stamp(600), None)])

    def test_live_focus_uses_exact_grafana_contract_without_outputting_titles(self):
        path = next((Path(__file__).parent / 'grafana_dashboards').glob('adfkm96*.json'))
        panel = json.loads(path.read_text('utf8'))['elements']['panel-8']['spec']
        grafana = panel['data']['spec']['queries'][0]['spec']['query']['spec']['rawSql']
        def query(sql, start, stop):
            self.assertNotIn('$__time', sql)
            self.assertIn("SELECT 25 * 60 AS min_seconds", sql)
            self.assertIn('FROM focus_rows', sql)
            self.assertNotIn('sample_title', sql.partition('SELECT jsonb_build_object')[2])
            return [dict(min_seconds=1500, rows=[], recorded_to=None)]
        self.assertEqual(read_focus(query, None, None, grafana), dict(min_seconds=1500, rows=[], recorded_to=None))


if __name__ == '__main__':
    main()
