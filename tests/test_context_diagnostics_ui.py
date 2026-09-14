import unittest
from pathlib import Path

from context_ui_diagnostics import last_context_update


ROOT = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self):
        self.rows = [{
            'id': 7,
            'agent_id': 'agent-1',
            'created_ts': 1000.0,
            'promoted_entity': 'camera.kitchen_score',
            'removed_entity': 'sensor.old_presence',
            'status': 'accepted',
        }]

    def promotion_status(self, agent):
        return {
            'last_promotion_ts': 1000.0,
            'promoted_entity': 'camera.kitchen_score',
            'replaced_entity': 'sensor.old_presence',
            'details': {'gain': 0.052, 'samples': 117},
        }

    def schema_history(self, agent_id, limit=10):
        return list(self.rows[:limit])


class ContextUIDiagnosticsTests(unittest.TestCase):
    def test_last_context_update_has_gain_samples_and_status(self):
        row = last_context_update(FakeService(), {'id': 'agent-1'})
        self.assertEqual(row['promoted_entity'], 'camera.kitchen_score')
        self.assertEqual(row['removed_entity'], 'sensor.old_presence')
        self.assertAlmostEqual(row['expected_gain'], 0.052)
        self.assertEqual(row['validation_samples'], 117)
        self.assertEqual(row['status'], 'accepted')
        self.assertEqual(row['history_id'], 7)

    def test_settings_diagnostics_render_requested_context_sections(self):
        source = (ROOT / 'adaptive_ai/src/static/p0.js').read_text(encoding='utf-8')
        for label in (
            'Active context', 'Primary sensor:', 'Context challengers',
            'Last context evaluation:', 'Schema age:', 'Schema revision:',
            'Next evaluation:', 'Context updated', 'Expected gain:',
            'Validation samples:',
        ):
            self.assertIn(label, source)
        self.assertIn('evaluation_window_end_ts', source)
        self.assertIn('last_context_update', source)

    def test_agent_card_does_not_gain_context_tournament_buttons(self):
        source = (ROOT / 'adaptive_ai/src/static/p0.js').read_text(encoding='utf-8')
        self.assertNotIn('data-a="context"', source)
        self.assertNotIn('data-a="challenger"', source)
        self.assertNotIn('data-a="tournament"', source)
        self.assertIn('data-a="settings"', source)

    def test_ui_diagnostics_stay_out_of_control_boundary(self):
        for rel in (
            'adaptive_ai/src/executor.py',
            'adaptive_ai/src/intent.py',
            'adaptive_ai/src/control_handoff.py',
        ):
            text = (ROOT / rel).read_text(encoding='utf-8')
            self.assertNotIn('context_ui_diagnostics', text)


if __name__ == '__main__':
    unittest.main()
