import tempfile
import unittest
from pathlib import Path

from support import ROOT
from storage import Store


def payload(name="Demo"):
    return {
        "name": name,
        "target_entity": "switch.demo",
        "target_property": "power",
        "min_value": 0,
        "max_value": 1,
        "confidence_threshold": 0.78,
        "deadband": 0.5,
        "action_interval": 1,
        "exploration_step": 1,
        "input_entities": ["*"],
    }


class PostTrainingShadowLifecycleTests(unittest.TestCase):
    def make_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Store(Path(temp.name) / "adaptive_ai.db")

    def test_completed_nonqualified_model_enters_shadow_but_stays_paused_for_control(self):
        store = self.make_store()
        agent = store.create_agent(payload())
        store.save_model(agent["id"], {"version": 1, "schema": {"entities": []}})
        store.set_training_state(
            agent["id"], "paused", score=0.55, samples=30,
            source="recorded-behaviour", detail={"reason": "below threshold"},
            shadow_after_completion=True,
        )
        row = store.get_agent_config(agent["id"])
        self.assertEqual(row["training_state"], "paused")
        self.assertEqual(row["mode"], "shadow")

    def test_completed_shadow_requires_a_persisted_model(self):
        store = self.make_store()
        agent = store.create_agent(payload())
        store.set_training_state(
            agent["id"], "paused", score=0.55, samples=30,
            shadow_after_completion=True,
        )
        row = store.get_agent_config(agent["id"])
        self.assertEqual(row["mode"], "paused")

    def test_error_or_interrupted_pause_remains_fully_paused(self):
        store = self.make_store()
        agent = store.create_agent(payload())
        store.save_model(agent["id"], {"version": 1, "schema": {"entities": []}})
        store.update_agent(agent["id"], {"mode": "shadow"})
        store.set_training_state(agent["id"], "paused", detail={"reason": "worker interrupted"})
        row = store.get_agent_config(agent["id"])
        self.assertEqual(row["training_state"], "paused")
        self.assertEqual(row["mode"], "paused")

    def test_qualified_completion_enters_shadow(self):
        store = self.make_store()
        agent = store.create_agent(payload())
        store.save_model(agent["id"], {"version": 1, "schema": {"entities": []}})
        store.set_training_state(
            agent["id"], "qualified", score=0.90, samples=40,
            shadow_after_completion=True,
        )
        row = store.get_agent_config(agent["id"])
        self.assertEqual(row["training_state"], "qualified")
        self.assertEqual(row["mode"], "shadow")

    def test_history_marks_completed_results_for_shadow_and_keeps_failed_runtime_alive(self):
        source = (ROOT / "adaptive_ai/src/history.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("shadow_after_completion=True"), 2)
        failed = source[source.index('state = "qualified" if passed else "paused"'):]
        failed = failed[:failed.index("qualification_summary = {") + 500]
        self.assertNotIn('self.engine.runtime.pop(agent["id"], None)', failed)
        self.assertIn("Control remains blocked; Shadow stays active", failed)

    def test_settings_exposes_explicit_shadow_and_pause_controls(self):
        source = (ROOT / "adaptive_ai/src/static/settings.js").read_text(encoding="utf-8")
        self.assertIn('data-tool="shadow"', source)
        self.assertIn("shadow:()=>setMode(id,'shadow')", source)
        self.assertIn('data-tool="paused"', source)

    def test_control_still_requires_qualified_training_state(self):
        source = (ROOT / "adaptive_ai/src/main.py").read_text(encoding="utf-8")
        self.assertIn('if existing.get("training_state") != "qualified":', source)
        self.assertIn("Control requires a completed historical benchmark", source)


if __name__ == "__main__":
    unittest.main()
