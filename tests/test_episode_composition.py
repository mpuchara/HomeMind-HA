import unittest
from pathlib import Path

from support import ROOT
import fast_queue_main as runtime


class EpisodeCompositionTests(unittest.TestCase):
    def tearDown(self):
        runtime.set_engine_extension_hook("after_fast_light", "test", None)
        runtime.set_engine_extension_hook("after_candidate_shadow_context", "test", None)

    def test_named_hooks_are_deterministic_and_removable(self):
        calls = []

        def hook(value):
            calls.append(value)
            return value + 1

        runtime.set_engine_extension_hook("after_fast_light", "test", hook)
        self.assertEqual(runtime._apply_engine_extension_hooks("after_fast_light", 4), 5)
        self.assertEqual(calls, [4])
        runtime.set_engine_extension_hook("after_fast_light", "test", None)
        self.assertEqual(runtime._apply_engine_extension_hooks("after_fast_light", 7), 7)
        self.assertEqual(calls, [4])

    def test_production_composition_points_are_before_controlling_layers(self):
        source = (ROOT / "adaptive_ai/src/fast_queue_main.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index('install_fast_light_objective(tournament)'),
            source.index('_apply_engine_extension_hooks("after_fast_light", tournament)'),
        )
        self.assertLess(
            source.index('_apply_engine_extension_hooks("after_fast_light", tournament)'),
            source.index('install_context_tournament_promotion(tournament)'),
        )
        self.assertLess(
            source.index('candidates = install_candidate_shadow_context(candidates)'),
            source.index('_apply_engine_extension_hooks("after_candidate_shadow_context", candidates)'),
        )
        self.assertLess(
            source.index('_apply_engine_extension_hooks("after_candidate_shadow_context", candidates)'),
            source.index('candidates = install_candidate_atomic_promote(candidates)'),
        )

    def test_final_entrypoint_uses_hooks_not_installer_monkey_patch(self):
        source = (ROOT / "adaptive_ai/src/preference_queue_main.py").read_text(encoding="utf-8")
        self.assertIn('set_engine_extension_hook("after_fast_light"', source)
        self.assertIn('"after_candidate_shadow_context", "preference_and_episode"', source)
        self.assertNotIn('shadow_context_module.install =', source)
        self.assertNotIn('fast_light_module.install =', source)


if __name__ == "__main__":
    unittest.main()
