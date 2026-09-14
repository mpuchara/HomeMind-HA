import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import *
from control_diagnostics import (
    SchemaAgeTracker,
    feature_tournament_state,
    install_control_diagnostics,
)
from qualification import assess_control_qualification
from storage import Store


class FakeTournamentService:
    def __init__(self, store, state):
        self.store = store
        self._state = dict(state)
        self._history = []
        self._probation = None
        self._promotion = {}

    def state(self, agent_id):
        return dict(self._state)

    def sync_agent(self, agent, *args, **kwargs):
        return dict(self._state)

    def state_for_agent(self, agent):
        return dict(self._state)

    def schema_history(self, agent_id, limit=20):
        return list(self._history)[:limit]

    def schema_probation(self, agent_id):
        return None if self._probation is None else dict(self._probation)

    def promotion_status(self, agent):
        return dict(self._promotion)


class ControlRigorRegressionTests(unittest.TestCase):
    def _agent(self, per_action):
        samples = sum(int(x.get('samples') or 0) for x in per_action.values())
        correct = sum(int(x.get('correct') or 0) for x in per_action.values())
        return {
            'id': 'agent-1',
            'target_property': 'power',
            'benchmark_samples': samples,
            'benchmark_detail': {
                'balanced': True,
                'counts': {
                    'samples': samples,
                    'correct': correct,
                    'per_action': per_action,
                },
            },
        }

    def test_binary_control_still_requires_both_actions(self):
        agent = self._agent({'0': {'samples': 40, 'correct': 40}})
        with patch.dict('settings.OPTIONS', {
            'candidate_benchmark_threshold': .78,
            'candidate_control_min_samples_per_action': 20,
            'candidate_control_wilson_z': 1.96,
        }):
            result = assess_control_qualification(agent)
        self.assertFalse(result['passed'])
        self.assertIn('both binary actions', result['reason'])

    def test_binary_control_still_requires_wilson_bound_per_action(self):
        # 20/20 has a 95% Wilson lower bound ~=83.9%; 19/20 is ~=76.4%.
        # Mean observed accuracy is 97.5%, but the weaker action must still block Control.
        agent = self._agent({
            '0': {'samples': 20, 'correct': 20},
            '1': {'samples': 20, 'correct': 19},
        })
        with patch.dict('settings.OPTIONS', {
            'candidate_benchmark_threshold': .78,
            'candidate_control_min_samples_per_action': 20,
            'candidate_control_wilson_z': 1.96,
        }):
            result = assess_control_qualification(agent)
        self.assertFalse(result['passed'])
        self.assertGreater(result['observed_score'], .95)
        self.assertLess(result['per_action']['1']['lower_bound'], .78)
        self.assertIn('lower confidence bound', result['reason'])

    def test_perfect_balanced_binary_evidence_can_still_pass(self):
        agent = self._agent({
            '0': {'samples': 20, 'correct': 20},
            '1': {'samples': 20, 'correct': 20},
        })
        with patch.dict('settings.OPTIONS', {
            'candidate_benchmark_threshold': .78,
            'candidate_control_min_samples_per_action': 20,
            'candidate_control_wilson_z': 1.96,
        }):
            result = assess_control_qualification(agent)
        self.assertTrue(result['passed'])
        self.assertGreater(result['lower_bound'], .78)


class ControlTournamentDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='control-diagnostics-')
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.state = {
            'agent_id': 'agent-1',
            'active_features': ['binary_sensor.kitchen_presence'],
            'challenger_features': ['binary_sensor.hall_presence'],
            'feature_scores': {'binary_sensor.hall_presence': .7},
            'last_evaluation': 900.0,
            'schema_revision': 3,
            'previous_schema': ['binary_sensor.old_presence'],
        }
        self.service = FakeTournamentService(self.store, self.state)
        self.agent = {
            'id': 'agent-1',
            'target_property': 'power',
            'training_updated_at': None,
            'benchmark_samples': 12,
            'benchmark_source': 'teach-rl-shadow',
            'benchmark_detail': {
                'rebenchmark_contract': 'prequential_shadow',
                'balanced': True,
                'counts': {
                    'samples': 12,
                    'correct': 12,
                    'per_action': {
                        '0': {'samples': 6, 'correct': 6},
                        '1': {'samples': 6, 'correct': 6},
                    },
                },
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_age_changes_only_when_schema_revision_or_signature_changes(self):
        tracker = SchemaAgeTracker(self.store, self.service)
        first = tracker.observe(self.agent, self.state, now=1000.0)
        same = tracker.observe(self.agent, self.state, now=1060.0)
        self.assertEqual(first['schema_revision'], 3)
        self.assertEqual(first['schema_age'], 0.0)
        self.assertEqual(same['schema_age'], 60.0)

        changed_state = dict(self.state)
        changed_state['schema_revision'] = 4
        changed_state['active_features'] = ['binary_sensor.hall_presence']
        changed = tracker.observe(self.agent, changed_state, now=1070.0)
        later = tracker.observe(self.agent, changed_state, now=1100.0)
        self.assertEqual(changed['schema_age'], 0.0)
        self.assertEqual(later['schema_age'], 30.0)

        # The age marker is persistent, not just process-local runtime state.
        tracker2 = SchemaAgeTracker(self.store, self.service)
        persisted = tracker2.observe(self.agent, changed_state, now=1130.0)
        self.assertEqual(persisted['schema_age'], 60.0)

    def test_feature_tournament_state_exposes_probation_without_controlling_anything(self):
        self.service._probation = {'status': 'active', 'samples': 14, 'history_id': 7}
        self.service._promotion = {
            'last_promotion_ts': 800.0,
            'promoted_entity': 'binary_sensor.kitchen_presence',
            'replaced_entity': 'binary_sensor.old_presence',
        }
        with patch.dict('settings.OPTIONS', {'context_tournament_enabled': True}):
            result = feature_tournament_state(self.service, self.agent, self.state)
        self.assertEqual(result['state'], 'schema_probation')
        self.assertEqual(result['challenger_count'], 1)
        self.assertEqual(result['schema_probation']['samples'], 14)
        self.assertEqual(result['last_promoted_entity'], 'binary_sensor.kitchen_presence')

    def test_http_qualification_is_decorated_but_pass_fail_contract_is_unchanged(self):
        core = SimpleNamespace(assess_control_qualification=assess_control_qualification)
        with patch.dict('settings.OPTIONS', {
            'candidate_benchmark_threshold': .78,
            'candidate_control_min_samples_per_action': 20,
            'candidate_control_wilson_z': 1.96,
            'context_tournament_enabled': True,
        }):
            install_control_diagnostics(core, self.service)
            result = core.assess_control_qualification(self.agent)
        # Only 6 samples/action: the original qualification must remain blocked.
        self.assertFalse(result['passed'])
        self.assertEqual(result['schema_revision'], 3)
        self.assertGreaterEqual(result['schema_age'], 0.0)
        self.assertEqual(result['prequential_samples'], 12)
        self.assertEqual(result['feature_tournament_state']['state'], 'evaluating')
        self.assertEqual(result['feature_tournament_state']['challengers'], ['binary_sensor.hall_presence'])

        runtime_state = self.service.state_for_agent(self.agent)
        self.assertIn('schema_age', runtime_state)
        self.assertIn('schema_changed_ts', runtime_state)

    def test_historical_benchmark_is_not_mislabeled_as_prequential(self):
        historical = dict(self.agent)
        historical['benchmark_source'] = 'recorded-behaviour'
        historical['benchmark_detail'] = {
            'balanced': True,
            'counts': self.agent['benchmark_detail']['counts'],
        }
        core = SimpleNamespace(assess_control_qualification=assess_control_qualification)
        install_control_diagnostics(core, self.service)
        result = core.assess_control_qualification(historical)
        self.assertEqual(result['prequential_samples'], 0)


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_executor_side_does_not_depend_on_tournament_or_diagnostics(self):
        root = Path(__file__).resolve().parents[1] / 'adaptive_ai' / 'src'
        for filename in ('executor.py', 'intent.py', 'control_handoff.py'):
            text = (root / filename).read_text(encoding='utf-8')
            self.assertNotIn('context_tournament', text, filename)
            self.assertNotIn('control_diagnostics', text, filename)

    def test_qualification_module_remains_independent_of_feature_tournament(self):
        root = Path(__file__).resolve().parents[1] / 'adaptive_ai' / 'src'
        text = (root / 'qualification.py').read_text(encoding='utf-8')
        self.assertNotIn('context_tournament', text)
        self.assertNotIn('control_diagnostics', text)


if __name__ == '__main__':
    unittest.main()
