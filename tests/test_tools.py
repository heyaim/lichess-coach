"""Unit tests for the logic that is easy to get subtly wrong: request pacing,
attempt counting, and the tools that parse lichess responses. Standard
library only; no network."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess  # noqa: E402
import chess.engine  # noqa: E402

import core  # noqa: E402
import coach_mcp  # noqa: E402
import debrief  # noqa: E402


class FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ndjson(entries):
    return "\n".join(json.dumps(e) for e in entries)


class StatsTests(unittest.TestCase):
    def test_attempt_counted_once_regardless_of_theme_count(self):
        state = {}
        core.stats_record_attempt(state, ["fork", "middlegame", "short"], won=False)
        core.stats_record_attempt(state, ["endgame"], won=True)
        self.assertEqual(state["attempt_totals"], {"tries": 2, "fails": 1})
        self.assertEqual(state["theme_stats"]["fork"], {"tries": 1, "fails": 1})


class RestRequestTests(unittest.TestCase):
    def setUp(self):
        core._last_rest[0] = 0.0

    def test_429_waits_a_minute_and_retries_once(self):
        sleeps = []
        with mock.patch.object(core.requests, "request",
                               side_effect=[FakeResp(429), FakeResp(200)]) as req, \
                mock.patch.object(core.time, "sleep", side_effect=sleeps.append):
            r = core.rest_request("GET", "http://x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_count, 2)
        self.assertIn(61, sleeps)

    def test_second_429_is_returned_rather_than_retried_forever(self):
        with mock.patch.object(core.requests, "request",
                               side_effect=[FakeResp(429), FakeResp(429)]) as req, \
                mock.patch.object(core.time, "sleep"):
            r = core.rest_request("GET", "http://x")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(req.call_count, 2)

    def test_dropped_connection_retries_once_then_raises(self):
        err = core.requests.ConnectionError("dropped")
        with mock.patch.object(core.requests, "request", side_effect=[err, FakeResp(200)]), \
                mock.patch.object(core.time, "sleep"):
            self.assertEqual(core.rest_request("GET", "http://x").status_code, 200)
        with mock.patch.object(core.requests, "request", side_effect=[err, err]), \
                mock.patch.object(core.time, "sleep"):
            with self.assertRaises(core.requests.ConnectionError):
                core.rest_request("GET", "http://x")


class FailedPuzzlesTests(unittest.TestCase):
    def call(self, text, args=None):
        with mock.patch.object(core, "get_token", return_value="t"), \
                mock.patch.object(core, "rest_request", return_value=FakeResp(200, text)):
            return coach_mcp.tool_failed_puzzles(args or {})

    def test_solved_since_uses_newest_first_order(self):
        entries = [  # the feed is newest first
            {"date": 40, "win": True, "puzzle": {"id": "AAAAA", "themes": ["fork"]}},
            {"date": 30, "win": False, "puzzle": {"id": "AAAAA", "themes": ["fork"]}},
            {"date": 20, "win": False, "puzzle": {"id": "BBBBB", "themes": ["pin"]}},
            {"date": 10, "win": True, "puzzle": {"id": "BBBBB", "themes": ["pin"]}},
        ]
        out = self.call(ndjson(entries))
        by_id = {f["id"]: f for f in out["failed_puzzles"]}
        self.assertTrue(by_id["AAAAA"]["solved_since"])
        self.assertFalse(by_id["BBBBB"]["solved_since"])

    def test_repeat_fails_dedupe_to_newest_and_bad_lines_are_skipped(self):
        entries = [{"date": 50, "win": False, "puzzle": {"id": "CCCCC", "themes": []}},
                   {"date": 5, "win": False, "puzzle": {"id": "CCCCC", "themes": []}}]
        out = self.call(ndjson(entries) + "\n{not json\n")
        self.assertEqual(out["total_recorded_fails"], 1)
        self.assertEqual(len(out["failed_puzzles"]), 1)

    def test_bad_max_is_a_plain_error(self):
        with mock.patch.object(core, "get_token", return_value="t"):
            self.assertEqual(coach_mcp.tool_failed_puzzles({"max": "lots"}),
                             {"error": "max must be a whole number"})


class RecentActivityTests(unittest.TestCase):
    def call(self, payload):
        with mock.patch.object(coach_mcp, "get_account", return_value={"username": "u"}), \
                mock.patch.object(core, "get_token", return_value="t"), \
                mock.patch.object(core, "rest_request",
                                  return_value=FakeResp(200, "", payload)):
            return coach_mcp.tool_recent_activity({})

    def test_tolerates_nulls_and_counts_attempts(self):
        feed = [{"interval": None,
                 "puzzles": {"score": {"win": 3, "loss": 2, "rp": None}},
                 "practice": None, "storm": None}]
        out = self.call(feed)
        self.assertEqual(out["days"][0]["puzzles"]["attempts"], 5)
        self.assertNotIn("storm", out["days"][0])

    def test_non_list_body_is_a_plain_error(self):
        self.assertIn("error", self.call({"oops": 1}))


class DashboardTests(unittest.TestCase):
    def test_orders_themes_by_performance_and_reads_replay_wins(self):
        data = {"global": {"nb": 10, "firstWins": 7, "replayWins": 2, "performance": 900},
                "themes": {
                    "mate": {"theme": "Checkmate", "results": {
                        "nb": 5, "firstWins": 4, "replayWins": 1, "performance": 950}},
                    "fork": {"theme": "Fork", "results": {
                        "nb": 5, "firstWins": 3, "replayWins": 1, "performance": 700}},
                    "odd": {"theme": "Odd", "results": None}}}
        with mock.patch.object(core, "get_token", return_value="t"), \
                mock.patch.object(core, "rest_request", return_value=FakeResp(200, "", data)):
            out = coach_mcp.tool_puzzle_dashboard({"days": 7})
        self.assertEqual(out["overall"]["replay_wins"], 2)
        self.assertEqual(out["weakest_by_performance"][0]["theme"], "fork")
        self.assertEqual(out["strongest_by_performance"][0]["theme"], "mate")


class IntArgTests(unittest.TestCase):
    def test_clamps_and_rejects(self):
        self.assertEqual(coach_mcp._int_arg({"max": -5}, "max", 5, 1, 100), 1)
        self.assertEqual(coach_mcp._int_arg({"max": "500"}, "max", 5, 1, 100), 100)
        with self.assertRaises(ValueError):
            coach_mcp._int_arg({"max": "abc"}, "max", 5, 1, 100)


class DeadEngine:
    def analyse(self, *args, **kwargs):
        raise chess.engine.EngineTerminatedError("engine process died")


class EngineDeathTests(unittest.TestCase):
    def test_dead_engine_surfaces_from_game_analysis(self):
        # A dead engine must raise, not silently degrade the debrief, so the
        # server can respawn it and retry.
        game = {"moves": ["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7#"],
                "server_analysis": [{"eval": 30}, {"eval": 20}, {"eval": 10},
                                    {"eval": 20}, {"eval": 30}, {"mate": 1}],
                "color": chess.BLACK, "opening_ply": 8, "clocks_cs": None,
                "initial_sec": None, "increment_sec": None}
        with self.assertRaises(chess.engine.EngineTerminatedError):
            debrief.analyze_game(game, DeadEngine(), 0.01, 0.01)


if __name__ == "__main__":
    unittest.main()
