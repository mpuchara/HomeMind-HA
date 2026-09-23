"""0.14.47 regressions for Correct Current, bounded training and deep Candidate events."""
import inspect
import unittest

from support import ROOT
from history import HistoryManager
import agent_candidate_shadow_runtime as shadow_runtime
from agent_correct_generation_history import build_correct_history


class Release047TrainingCorrectEventsTests(unittest.TestCase):
    def test_training_window_is_recent_and_resume_clamps_forward(self):
        source = inspect.getsource(HistoryManager._training_bounds)
        self.assertIn('agent_training_history_days', source)
        self.assertIn('end_ts - days * 86400.0', source)
        run = inspect.getsource(HistoryManager._run_agent_indexing)
        self.assertIn('max(archive_start, float(persisted_start))', run)
        self.assertIn('agent_training_pause_ms', run)

    def test_explicit_history_refresh_has_no_background_pause(self):
        source = inspect.getsource(HistoryManager._refresh_agent_history)
        self.assertIn('agent_training_pause_ms', source)
        self.assertNotIn('history_background_pause_ms', source)

    def test_correct_candidate_current_uses_physical_history(self):
        source = inspect.getsource(build_correct_history)
        candidate = source.split('child_history =', 1)[1]
        self.assertIn('current_points = _current_rows(manager, agent, start, end)', candidate)
        self.assertIn('"current": {"label": "Current", "points": _values(current_points, "current")}', candidate)

    def test_candidate_active_roots_use_lineage_root_id(self):
        source = inspect.getsource(shadow_runtime.install)
        refresh = source.split('def _refresh_active_candidate_parents',1)[1].split('def _schema_entities',1)[0]
        self.assertIn('SELECT DISTINCT root_agent_id', refresh)
        self.assertIn("generation_type='candidate'", refresh)
        self.assertNotIn('SELECT DISTINCT parent_agent_id FROM agent_candidates\n                ).fetchall()', refresh)

    def test_release_defaults_are_fast_but_short_sliced(self):
        settings=(ROOT/'adaptive_ai/src/settings.py').read_text(encoding='utf-8')
        config=(ROOT/'adaptive_ai/config.yaml').read_text(encoding='utf-8')
        self.assertIn('"agent_training_history_days": 7', settings)
        self.assertIn('"agent_training_pause_ms": 0', settings)
        self.assertIn('"training_cpu_duty_cycle": 0.65', settings)
        self.assertIn('"training_max_continuous_work_ms": 35', settings)
        self.assertIn('history_background_pause_ms: 1500', config)
        self.assertIn('agent_training_pause_ms: 0', config)


if __name__ == '__main__':
    unittest.main()
