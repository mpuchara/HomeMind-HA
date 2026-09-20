import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
from context_tournament import ContextTournament
from context_tournament_metrics import (
    availability_stats,
    balanced_accuracy,
    install_metrics,
    metric_row,
)
from storage import Store


class FakeEngine:
    def __init__(self, states, scores, policy):
        self.state_map = dict(states)
        self.entity_registry = {}
        self.context_relevance = {'agent-1': dict(scores)}
        self.models = {'agent-1': policy}
        self.runtime = {'agent-1': {'last_prediction': 0.0}}
        self.lock = threading.RLock()
        self._policy = policy


def st(entity_id, value, **attrs):
    return {
        'entity_id': entity_id,
        'state': value,
        'attributes': attrs,
        'last_changed': '2026-09-14T12:00:00+00:00',
        'last_updated': '2026-09-14T12:00:00+00:00',
        'context': {},
    }


class ContextTournamentMetricMathTests(unittest.TestCase):
    def test_binary_uses_balanced_accuracy_and_incremental_gain(self):
        model = {
            'samples': 20,
            'class_totals': [10, 10],
            'active_correct_by_class': [9, 5],
            'shadow_correct_by_class': [9, 8],
        }
        row = metric_row(model, [0.0, 1.0])
        self.assertEqual(row['metric'], 'balanced_accuracy')
        self.assertAlmostEqual(row['baseline_score'], 0.70, places=7)
        self.assertAlmostEqual(row['challenger_score'], 0.85, places=7)
        self.assertAlmostEqual(row['gain'], 0.15, places=7)
        self.assertAlmostEqual(
            row['gain'], row['baseline_loss'] - row['challenger_loss'], places=7
        )

    def test_binary_score_waits_until_both_classes_are_observed(self):
        self.assertIsNone(balanced_accuracy([12, 0], [11, 0]))
        row = metric_row({
            'samples': 12,
            'class_totals': [12, 0],
            'active_correct_by_class': [11, 0],
            'shadow_correct_by_class': [12, 0],
        }, [0.0, 1.0])
        self.assertIsNone(row['baseline_score'])
        self.assertIsNone(row['challenger_score'])
        self.assertIsNone(row['gain'])

    def test_continuous_uses_normalized_mae_and_loss_difference(self):
        model = {
            'samples': 4,
            'active_abs_error_sum': 40.0,
            'shadow_abs_error_sum': 20.0,
        }
        row = metric_row(model, [0.0, 50.0, 100.0])
        self.assertEqual(row['metric'], 'normalized_mae')
        self.assertAlmostEqual(row['baseline_mae'], 10.0, places=7)
        self.assertAlmostEqual(row['challenger_mae'], 5.0, places=7)
        self.assertAlmostEqual(row['baseline_nmae'], 0.10, places=7)
        self.assertAlmostEqual(row['challenger_nmae'], 0.05, places=7)
        self.assertAlmostEqual(row['baseline_score'], 0.90, places=7)
        self.assertAlmostEqual(row['challenger_score'], 0.95, places=7)
        self.assertAlmostEqual(row['gain'], 0.05, places=7)
        self.assertAlmostEqual(
            row['gain'], row['baseline_loss'] - row['challenger_loss'], places=7
        )

    def test_availability_and_observation_span(self):
        availability, days = availability_stats({
            'observation_opportunities': 100,
            'available_observations': 90,
            'first_observed_ts': 1000.0,
            'last_observed_ts': 1000.0 + 2 * 86400.0,
        })
        self.assertAlmostEqual(availability, 0.90, places=7)
        self.assertAlmostEqual(days, 2.0, places=7)


class ContextTournamentMetricIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='context-metrics-')
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.target = 'light.kitchen'
        self.active = 'binary_sensor.kitchen_presence'
        self.challenger = 'binary_sensor.hall_presence'
        self.agent = {
            'id': 'agent-1',
            'target_entity': self.target,
            'target_property': 'power',
            'min_value': 0,
            'max_value': 1,
            'deadband': .5,
            'action_interval': 1,
        }
        self.states = {
            self.target: st(self.target, 'off'),
            self.active: st(self.active, 'off', device_class='occupancy'),
            self.challenger: st(self.challenger, 'on', device_class='occupancy'),
        }
        self.policy = SimpleNamespace(
            schema=SimpleNamespace(entities=[self.active]),
            model_revision='champion-1',
            tournament_revision='champion-1',
        )
        self.engine = FakeEngine(self.states, {self.challenger: .95}, self.policy)
        self.service = ContextTournament(self.store, self.engine)
        self.service.sync_agent(self.agent, policy=self.policy)
        install_metrics(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_row_contains_incremental_value_contract(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        status = self.service.shadow_status(self.agent)
        row = status['challengers'][0]
        self.assertEqual(row['entity_id'], self.challenger)
        self.assertEqual(row['metric'], 'balanced_accuracy')
        self.assertIn('baseline_score', row)
        self.assertIn('challenger_score', row)
        self.assertIn('gain', row)
        self.assertIn('samples', row)
        self.assertIn('availability', row)
        self.assertIn('days_observed', row)
        self.assertAlmostEqual(row['availability'], 1.0, places=7)
        self.assertEqual(row['evaluation_mode'], 'prequential_future_only')
        self.assertEqual(status['evaluation_order'], 'predict -> score -> learn')
        self.assertEqual(status['evaluation_scope'], 'future events after challenger selection')

    def test_unavailable_state_reduces_availability_not_feature_score(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        unavailable = dict(self.states)
        unavailable[self.challenger] = st(self.challenger, 'unavailable', device_class='occupancy')
        self.engine.state_map = dict(unavailable)
        self.service.observe_shadow(self.agent, unavailable, {self.challenger})
        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertAlmostEqual(row['availability'], 0.5, places=7)
        self.assertAlmostEqual(row['feature_score'], 0.95, places=7)

    def test_old_shadow_evidence_is_reset_before_future_proof(self):
        # Simulate evidence accumulated before the strict future-only evaluator existed.
        legacy = self.service._blank_shadow_model(2)
        legacy.update({
            'samples': 40,
            'active_correct': 20,
            'shadow_correct': 38,
            'counts': {'0:8': [0, 40]},
        })
        self.service._save_shadow_model(self.agent['id'], self.challenger, legacy)

        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertEqual(row['samples'], 0)
        self.assertIsNone(row['gain'])
        self.assertEqual(row['evaluation_reason'], 'future_only_upgrade')
        self.assertEqual(row['evaluation_schema_revision'], 1)
        self.assertEqual(row['evaluation_champion_revision'], 'champion-1')

    def test_reselected_challenger_starts_a_new_future_epoch(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        model = self.service._load_shadow_model(self.agent['id'], self.challenger, 2)
        model['samples'] = 25
        model['class_totals'] = [12, 13]
        model['active_correct_by_class'] = [8, 7]
        model['shadow_correct_by_class'] = [10, 10]
        self.service._save_shadow_model(self.agent['id'], self.challenger, model)

        removed = self.service.sync_agent(
            self.agent, policy=self.policy, feature_scores={self.challenger: 0.0}
        )
        self.assertEqual(removed['challenger_features'], [])
        restored = self.service.sync_agent(
            self.agent, policy=self.policy, feature_scores={self.challenger: .95}
        )
        self.assertEqual(restored['challenger_features'], [self.challenger])
        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertEqual(row['samples'], 0)
        self.assertIsNone(row['gain'])
        self.assertEqual(row['evaluation_reason'], 'challenger_selected')

    def test_active_schema_change_invalidates_old_challenger_proof(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        model = self.service._load_shadow_model(self.agent['id'], self.challenger, 2)
        model['samples'] = 18
        self.service._save_shadow_model(self.agent['id'], self.challenger, model)

        revised = self.service.sync_agent(
            self.agent,
            policy=None,
            active_features=[self.active, 'sensor.extra_active_context'],
            feature_scores={self.challenger: .95},
        )
        self.assertEqual(revised['schema_revision'], 2)
        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertEqual(row['samples'], 0)
        self.assertEqual(row['evaluation_schema_revision'], 2)
        self.assertEqual(row['evaluation_reason'], 'active_schema_changed')

    def test_champion_model_change_invalidates_old_challenger_proof(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        model = self.service._load_shadow_model(self.agent['id'], self.challenger, 2)
        model['samples'] = 18
        self.service._save_shadow_model(self.agent['id'], self.challenger, model)

        new_policy = SimpleNamespace(
            schema=SimpleNamespace(entities=[self.active]),
            model_revision='champion-2',
            tournament_revision='champion-2',
        )
        self.engine.models[self.agent['id']] = new_policy
        self.service.sync_agent(self.agent, policy=new_policy, feature_scores={self.challenger: .95})
        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertEqual(row['samples'], 0)
        self.assertEqual(row['evaluation_champion_revision'], 'champion-2')
        self.assertEqual(row['evaluation_reason'], 'champion_model_changed')

    def test_decay_only_model_revision_change_keeps_challenger_epoch(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        model = self.service._load_shadow_model(self.agent['id'], self.challenger, 2)
        model['samples'] = 18
        started = model['evaluation_started_ts']
        self.service._save_shadow_model(self.agent['id'], self.challenger, model)

        decayed_policy = SimpleNamespace(
            schema=SimpleNamespace(entities=[self.active]),
            model_revision='champion-1-decayed',
            tournament_revision='champion-1',
        )
        self.engine.models[self.agent['id']] = decayed_policy
        self.service.sync_agent(
            self.agent, policy=decayed_policy,
            feature_scores={self.challenger: .95}
        )
        row = self.service.shadow_status(self.agent)['challengers'][0]
        self.assertEqual(row['samples'], 18)
        self.assertEqual(row['evaluation_started_ts'], started)
        self.assertEqual(row['evaluation_champion_revision'], 'champion-1')

    def test_metrics_extension_stays_non_controlling(self):
        self.service.observe_shadow(self.agent, dict(self.states), {self.challenger})
        status = self.service.shadow_status(self.agent)
        self.assertEqual(status['mode'], 'shadow_only')
        self.assertFalse(status['controls_device'])
        self.assertFalse(status['rebuilds_policy'])
        self.assertFalse(hasattr(self.service, 'submit'))
        self.assertFalse(hasattr(self.service, 'execute'))
        self.assertEqual(status['score_definition'], 'gain = Loss(active) - Loss(active + sensor)')


if __name__ == '__main__':
    unittest.main()
