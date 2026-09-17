import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PreferenceCompositionContractTests(unittest.TestCase):
    def test_final_entrypoint_installs_service_not_process_agent_patch(self):
        source = (ROOT / "adaptive_ai/src/preference_queue_main.py").read_text(encoding="utf-8")
        self.assertIn("LightingPreferenceModel", source)
        self.assertIn("core.ENGINE.decision_composer = PreferenceDecisionComposer", source)
        self.assertNotIn("ENGINE.process_agent =", source)
        self.assertNotIn("engine.process_agent =", source)

    def test_engine_owns_composition_before_actionintent_and_executor(self):
        source = (ROOT / "adaptive_ai/src/engine.py").read_text(encoding="utf-8")
        compose = source.index("composed = composer.compose(")
        intent = source.index("intent = ActionIntent.create(", compose)
        dispatch = source.index("return self.executor.submit(intent", intent)
        self.assertLess(compose, intent)
        self.assertLess(intent, dispatch)
        self.assertIn("decision_source=decision_source", source[intent:dispatch])

    def test_preference_source_is_additive_actionintent_metadata(self):
        source = (ROOT / "adaptive_ai/src/intent.py").read_text(encoding="utf-8")
        self.assertIn("decision_source: str = 'historical_policy_bootstrap'", source)
        self.assertIn("def export(self):", source)


if __name__ == "__main__":
    unittest.main()
