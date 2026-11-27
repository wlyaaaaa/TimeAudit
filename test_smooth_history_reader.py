import datetime as dt
import unittest
from unittest.mock import patch

import game_live_reader as games
import smooth_history_reader as reader


class SmoothHistoryTest(unittest.TestCase):
    def test_measured_spikes_and_simultaneous_program_hangs_keep_evidence(self):
        now = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
        game_path = r"C:\Games\Game.exe"
        start = now - dt.timedelta(minutes=1)
        old = now - dt.timedelta(days=2)
        focus = [[s.isoformat(), (s+d).isoformat(), "game.exe", pid, game_path]
                 for s,d,pid in [(start,dt.timedelta(seconds=6),8),(old,dt.timedelta(minutes=1),7)]]
        usage = [entry[:3]+[None,None] for entry in focus]
        usage.append([start.isoformat(), (start+dt.timedelta(seconds=4)).isoformat(), "idle", None, None])
        t = start.timestamp()
        row = dict(games=[[0,t,68,start.isoformat(),26,2],[0,t+3,125,start.isoformat(),8,1]],
                   hung=[[t,1,3,"first.exe"],[t,2,4,"second.exe"],[t+3,1,3,"first.exe"]],
                   sampled=None, bands=[[t,t+6,None,60.0]], apps=None, last_frame=start.isoformat())
        with patch.object(games,"query",return_value=usage+focus), patch.object(games,"classify",return_value=("game","Game")), patch.object(reader,"query",return_value=[row]):
            result = reader.read_smooth_history(now)
        game, first, second = result["stutters"]["events"]
        self.assertEqual((game["kind"], game["max_frame_ms"], game["end"]), ("game",125,t+6))
        self.assertEqual((first["samples"],second["samples"]), (2,1))
        self.assertNotEqual(first["id"],second["id"])
        self.assertEqual(result["summary"]["program_hangs"],2)
        self.assertIsNone(result["fps"]["sources"][0]["process_name"])
        history = result["game_history"]
        self.assertEqual((history["game_name"],history["seconds"]), ("Game",6))
        latest, previous = history["latest_session"], history["previous_session"]
        self.assertAlmostEqual(latest["avg_fps"], 34 / 3)
        self.assertEqual(latest["game_events"], 1)
        self.assertIsNone(latest["game_events_reason"])
        self.assertIsNone(latest["low_fps"])
        self.assertEqual(latest["low_fps_reason"], "raw_frame_times_unavailable")
        self.assertEqual(previous["start"], reader.format_utc(old))
        for field in ("fps_reason", "low_fps_reason", "game_events_reason"):
            self.assertEqual(previous[field], "no_frame_samples")
        self.assertFalse(history["complete"])

    def test_session_zero_requires_a_sample_in_every_game_window(self):
        now = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
        recent, earlier = (now - dt.timedelta(seconds=n) for n in (60, 120))
        evidence = [(s, s+dt.timedelta(seconds=6), pid, r"C:\Games\Game.exe", "game", "Game")
                    for s, pid in ((recent, 8), (earlier, 7))]
        t, e = recent.timestamp(), earlier.timestamp()
        samples = [[0,t,20,recent.isoformat(),50,1], [0,t+3,20,recent.isoformat(),50,1],
                   [1,e,20,earlier.isoformat(),50,1]]
        row = dict(games=samples, hung=None, sampled=None, bands=None, apps=None, last_frame=None)
        with (patch.object(reader, "read_game_evidence", return_value=evidence),
              patch.object(reader, "query", return_value=[row])):
            history = reader.read_smooth_history(now)["game_history"]
        latest, previous = history["latest_session"], history["previous_session"]
        self.assertEqual(latest["game_events"], 0)
        self.assertEqual(previous["avg_fps"], 50)
        self.assertIsNone(previous["game_events"])
        self.assertEqual(previous["game_events_reason"], "frame_coverage_gap")
        unknown = reader.session_performance(dict(previous, start=None), [], {}, {})
        self.assertIsNone(unknown["avg_fps"])
        self.assertIsNone(unknown["game_events"])
        self.assertEqual(unknown["fps_reason"], "session_start_unknown")

    def test_unobserved_ranges_remain_unknown(self):
        now = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
        row = dict(games=None, hung=None, sampled=None, bands=None, apps=None, last_frame=None)
        with patch.object(reader,"read_game_evidence",return_value=[]), patch.object(reader,"query",return_value=[row]):
            result = reader.read_smooth_history(now)
        self.assertIsNone(result["summary"]["game_events"])
        self.assertIsNone(result["summary"]["program_hangs"])
        self.assertFalse(result["summary"]["hang_complete"])
        self.assertIsNone(result["game_history"]["played"])
        self.assertEqual(result["summary"]["game_uncovered_seconds"],86400)
