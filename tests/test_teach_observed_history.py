import tempfile
import threading
import unittest
from pathlib import Path

from storage import Store
from teach_observed_history import DESIRED_STALE_SECONDS, install


class FakeTeaching:
    def __init__(self):
        self.lock = threading.RLock()
        self.buffer = []

    def flush(self):
        return None


class FakeEngine:
    def __init__(self):
        self.teaching = FakeTeaching()


class FakeService:
    def __init__(self):
        self._points = [
            {"ts": 100.0, "current": 0.0, "desired": 0.0},
            {"ts": 110.0, "current": 1.0, "desired": 0.0},
            {"ts": 120.0, "current": 1.0, "desired": 0.0},
        ]

    def history(self, agent, start, end):
        return {
            "points": [dict(x) for x in self._points],
            "start": float(start), "end": float(end), "reduced": False,
            "desired_source": "base_rl_policy_replay", "labels": [],
        }

    def point(self, agent, timestamp):
        current = 0.0 if float(timestamp) < 110.0 else 1.0
        return {"ts": float(timestamp), "current": current, "desired": 0.0, "context_complete": True}


class ObservedDesiredHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="teach-observed-")
        self.store = Store(Path(self.temp.name) / "test.db")
        self.engine = FakeEngine()
        self.service = FakeService()
        install(self.store, self.engine, self.service)
        self.agent = {"id": "agent-1"}

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, ts, current, desired):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO decision_history(agent_id,ts,current,desired) VALUES(?,?,?,?)",
                (self.agent["id"], float(ts), float(current), float(desired)),
            )

    def test_history_replaces_policy_replay_with_recorded_live_desired(self):
        # FakeService deliberately claims the old replay was OFF everywhere.
        self.insert(99, 0, 1)
        self.insert(115, 1, 0)
        result = self.service.history(self.agent, 100, 120)
        by_ts = {round(p["ts"], 3): p for p in result["points"]}
        self.assertEqual(by_ts[100.0]["desired"], 1.0)
        self.assertEqual(by_ts[110.0]["desired"], 1.0)
        self.assertIn(115.0, by_ts)  # exact runtime Desired transition is retained
        self.assertEqual(by_ts[115.0]["desired"], 0.0)
        self.assertEqual(by_ts[120.0]["desired"], 0.0)
        self.assertEqual(result["desired_source"], "observed_runtime_decision_history")
        self.assertNotEqual(result["desired_source"], "base_rl_policy_replay")

    def test_point_uses_recorded_desired_visible_on_card(self):
        self.insert(105, 0, 1)
        point = self.service.point(self.agent, 110)
        self.assertEqual(point["desired"], 1.0)
        self.assertEqual(point["desired_source"], "observed_runtime_decision_history")

    def test_missing_recorded_desired_is_gap_not_invented_replay(self):
        point = self.service.point(self.agent, 110)
        self.assertIsNone(point["desired"])
        history = self.service.history(self.agent, 100, 120)
        self.assertTrue(all(p["desired"] is None for p in history["points"]))

    def test_old_decision_does_not_bridge_addon_downtime(self):
        self.insert(100, 0, 1)
        late = 100 + DESIRED_STALE_SECONDS + 5
        self.assertIsNone(self.service.point(self.agent, late)["desired"])

    def test_unflushed_runtime_buffer_is_visible_immediately(self):
        self.engine.teaching.buffer.append((self.agent["id"], 109.0, 1.0, 1.0))
        point = self.service.point(self.agent, 110)
        self.assertEqual(point["desired"], 1.0)


class ObservedDesiredUIContract(unittest.TestCase):
    def test_legend_colors_match_rendered_series(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "adaptive_ai/src/static/teach_observed_ui.js").read_text(encoding="utf-8")
        self.assertIn("current:'#73dbec'", text)
        self.assertIn("desired:'#c2a6ff'", text)
        self.assertIn("teach:'#ffd166'", text)
        self.assertIn("Desired obserwowany (karta agenta)", text)

    def test_ui_no_longer_describes_desired_as_policy_replay(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "adaptive_ai/src/static/teach_observed_ui.js").read_text(encoding="utf-8")
        self.assertIn("decyzję faktycznie obserwowaną na karcie agenta", text)
        self.assertIn("Desired obserwowany:", text)


if __name__ == "__main__":
    unittest.main()
