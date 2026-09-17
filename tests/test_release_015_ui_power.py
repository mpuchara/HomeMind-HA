"""0.14.15 regressions for action UX, request semantics and Raspberry Pi load."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import ROOT


class Release015UiPowerTests(unittest.TestCase):
    def test_read_timeout_never_aborts_mutating_requests(self):
        source = (ROOT/'adaptive_ai/src/static/home.js').read_text(encoding='utf-8')
        self.assertIn("if(method!=='GET'&&method!=='HEAD')return nativeFetch(input,fetchInit);", source)
        self.assertIn('Adaptive AI read timeout', source)
        self.assertNotIn('setTimeout(()=>controller.abort(),6000)', source)

    def test_autonomous_is_explained_and_has_live_discard_path(self):
        source = (ROOT/'adaptive_ai/src/static/runtime_activity_ui.js').read_text(encoding='utf-8')
        self.assertIn('one-shot action, not a mode', source)
        self.assertIn('Discard Candidate', source)
        self.assertIn("method:'DELETE'", source)
        self.assertIn('Candidate active', source)
        self.assertIn('autonomous_continuation', source)

    def test_settings_explore_is_not_exposed_before_a_model_exists(self):
        source = (ROOT/'adaptive_ai/src/static/settings.js').read_text(encoding='utf-8')
        self.assertIn('const hasLearnedModel', source)
        self.assertIn('Explore / Eksperymenty', source)
        self.assertIn("typeof window.openExplore!=='function'", source)

    def test_candidate_live_http_is_collapsed_when_idle(self):
        source = (ROOT/'adaptive_ai/src/static/runtime_activity_ui.js').read_text(encoding='utf-8')
        self.assertIn("path.endsWith('api/candidate-live')", source)
        self.assertIn("if(!document.querySelector('.candidate-agent'))", source)
        self.assertIn('now-candidateLiveCache.at<1000', source)

    def test_archive_training_is_duty_cycled_and_candidate_idle_poll_is_relaxed(self):
        import rpi_low_power_runtime as low_power

        class Store:
            def __init__(self):
                self.events = []
            def archive_iter(self, *args, **kwargs):
                yield from range(512)
            def event(self, *args):
                self.events.append(args)

        class Manager:
            def __init__(self):
                self.poll_seconds = 0.5
                self.maintenance_calls = 0
            def _maintenance(self):
                self.maintenance_calls += 1

        store = Store()
        manager = Manager()
        core = SimpleNamespace(
            STORE=store,
            OPTIONS={
                'agent_training_chunk_hours': 24,
                'history_background_pause_ms': 500,
                'training_cpu_duty_cycle': 0.55,
            },
        )
        old_name = threading.current_thread().name
        try:
            threading.current_thread().name = 'adaptive-ai-index-test'
            with patch.object(low_power.time, 'sleep') as sleeper:
                result = low_power.install(core, manager)
                self.assertIs(result, manager)
                self.assertEqual(list(store.archive_iter()), list(range(512)))
                self.assertGreaterEqual(sleeper.call_count, 2)
        finally:
            threading.current_thread().name = old_name

        self.assertEqual(core.OPTIONS['agent_training_chunk_hours'], 6)
        self.assertEqual(core.OPTIONS['history_background_pause_ms'], 1500)
        self.assertGreaterEqual(manager.poll_seconds, 3.0)
        manager._maintenance()
        manager._maintenance()
        self.assertEqual(manager.maintenance_calls, 1)
        snapshot = core.LOW_POWER_RUNTIME()
        self.assertGreaterEqual(snapshot['throttle_batches'], 2)
        self.assertGreater(snapshot['throttle_sleep_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
