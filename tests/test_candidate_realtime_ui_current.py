import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_correct_generation_history import _physical_current_points


ROOT = Path(__file__).resolve().parents[1]


class PhysicalCurrentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "current-history.db")
        self.manager = SimpleNamespace(store=self.store)
        self.agent = {
            "target_entity": "light.bathroom",
            "target_property": "power",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _row(self, ts, state):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO entity_history(entity_id,ts,state,attributes_json,context_user_id,source)
                   VALUES(?,?,?,?,?,?)""",
                ("light.bathroom", float(ts), state, "{}", None, "test"),
            )

    def test_physical_current_comes_from_target_history_edges(self):
        self._row(90, "off")
        self._row(110, "on")
        self._row(140, "off")
        points = _physical_current_points(self.manager, self.agent, 100, 160)
        self.assertEqual(points, [
            {"ts": 100.0, "value": 0.0},
            {"ts": 110.0, "value": 1.0},
            {"ts": 140.0, "value": 0.0},
            {"ts": 160.0, "value": 0.0},
        ])

    def test_factual_current_is_drawn_continuously_between_real_state_edges(self):
        source = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn("const currentPath=path(series.current.points,{breakOnStale:false})", source)
        self.assertIn("breakOnStale&&last&&ts-last>stale", source)

    def test_chart_source_never_uses_candidate_observation_current_as_truth(self):
        source = (ROOT / "adaptive_ai/src/agent_correct_generation_history.py").read_text(encoding="utf-8")
        self.assertIn('"current": {"label": "Current", "points": _physical_current_points(manager, agent, start, end)}', source)
        self.assertNotIn('"current": {"label": "Current", "points": _values(child_points, "current")}', source)
        self.assertIn('"current_source": "home_assistant_entity_history"', source)


class RealtimeCandidateUiContractTests(unittest.TestCase):
    def test_lightweight_candidate_endpoint_exposes_realtime_snapshots(self):
        backend = (ROOT / "adaptive_ai/src/agent_candidate_card_summary.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/candidate-live"', backend)
        self.assertIn('"candidates": live_decision_snapshots(manager)', backend)

    def test_candidate_tiles_consume_live_event_without_waiting_for_full_refresh(self):
        source = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn("fetch('api/candidate-live'", source)
        self.assertIn("setInterval(refreshLiveDecisions,250)", source)
        self.assertIn("applyLiveCandidate(candidate)", source)
        self.assertIn("cached.shadow_current=s.current", source)
        self.assertIn("cached.parent_desired=s.parent_desired", source)
        self.assertIn("cached.candidate_desired=s.candidate_desired", source)


if __name__ == "__main__":
    unittest.main()
