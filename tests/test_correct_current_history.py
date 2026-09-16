import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_correct_generation_history import _physical_current_points


ROOT = Path(__file__).resolve().parents[1]


class FactualCurrentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "current-history.db")
        self.manager = SimpleNamespace(store=self.store)
        self.agent = {"target_entity": "light.bathroom", "target_property": "power"}

    def tearDown(self):
        self.temp.cleanup()

    def _row(self, ts, state):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO entity_history(entity_id,ts,state,attributes_json,context_user_id,source)
                   VALUES(?,?,?,?,?,?)""",
                ("light.bathroom", float(ts), state, "{}", None, "test"),
            )

    def test_current_comes_from_real_target_edges(self):
        self._row(90, "off")
        self._row(110, "on")
        self._row(140, "off")
        self.assertEqual(
            _physical_current_points(self.manager, self.agent, 100, 160),
            [
                {"ts": 100.0, "value": 0.0},
                {"ts": 110.0, "value": 1.0},
                {"ts": 140.0, "value": 0.0},
                {"ts": 160.0, "value": 0.0},
            ],
        )

    def test_candidate_observation_current_is_not_chart_ground_truth(self):
        source = (ROOT / "adaptive_ai/src/agent_correct_generation_history.py").read_text(encoding="utf-8")
        self.assertIn('"current_source": "home_assistant_entity_history"', source)
        self.assertIn('_physical_current_points(manager, agent, start, end)', source)
        self.assertNotIn('_values(child_points, "current")', source)

    def test_physical_current_does_not_break_on_inference_staleness(self):
        source = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn('const currentPath=path(series.current.points,{breakOnStale:false})', source)
        self.assertIn('breakOnStale&&last&&ts-last>stale', source)


if __name__ == "__main__":
    unittest.main()
