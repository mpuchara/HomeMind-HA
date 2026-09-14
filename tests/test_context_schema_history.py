import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
from context import ExplicitFeatureSchema
from context_schema_history import install_schema_history
from context_tournament_promotion import PROMOTION_EPOCH_VERSION, install_promotion
from policy import MultiHorizonPolicy
from storage import Store


class HistoryOnlyService:
    def __init__(self, store):
        self.store = store
        self._state = {'active_features': ['sensor.a']}

    def state(self, agent_id):
        return dict(self._state)

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        return None


class SchemaHistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='schema-history-')
        self.store = Store(Path(self.temp.name) / 'history.db')
        self.service = HistoryOnlyService(self.store)
        install_schema_history(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_table_contract_and_status_lifecycle(self):
        history_id = self.service.record_schema_history(
            agent_id='agent-1',
            created_ts=1234.5,
            old_schema=['sensor.a', 'sensor.b'],
            new_schema=['sensor.a', 'sensor.c'],
            reason='sensor_tournament_promotion',
            baseline_score=0.84,
            challenger_score=0.91,
            evaluation_samples=48,
            promoted_entity='sensor.c',
            removed_entity='sensor.b',
            status='promoted',
        )
        row = self.service.schema_history_by_id(history_id)
        self.assertEqual(row['agent_id'], 'agent-1')
        self.assertEqual(row['old_schema'], ['sensor.a', 'sensor.b'])
        self.assertEqual(row['new_schema'], ['sensor.a', 'sensor.c'])
        self.assertEqual(row['reason'], 'sensor_tournament_promotion')
        self.assertAlmostEqual(row['baseline_score'], 0.84)
        self.assertAlmostEqual(row['challenger_score'], 0.91)
        self.assertEqual(row['evaluation_samples'], 48)
        self.assertEqual(row['promoted_entity'], 'sensor.c')
        self.assertEqual(row['removed_entity'], 'sensor.b')
        self.assertEqual(row['status'], 'promoted')

        self.assertTrue(self.service.set_schema_history_status(history_id, 'accepted'))
        self.assertEqual(self.service.schema_history_by_id(history_id)['status'], 'accepted')
        self.assertTrue(self.service.set_schema_history_status(history_id, 'rolled_back'))
        self.assertEqual(self.service.schema_history_by_id(history_id)['status'], 'rolled_back')
        with self.assertRaises(ValueError):
            self.service.set_schema_history_status(history_id, 'unknown')

    def test_history_is_ordered_per_agent(self):
        first = self.service.record_schema_history(
            agent_id='agent-1', old_schema=['a'], new_schema=['b'], reason='one', created_ts=10,
        )
        second = self.service.record_schema_history(
            agent_id='agent-1', old_schema=['b'], new_schema=['c'], reason='two', created_ts=20,
        )
        self.service.record_schema_history(
            agent_id='agent-2', old_schema=['x'], new_schema=['y'], reason='other', created_ts=30,
        )
        rows = self.service.schema_history('agent-1')
        self.assertEqual([row['id'] for row in rows], [second, first])


class FakeTournamentService:
    def __init__(self, store, engine, tournament, model):
        self.store = store
        self.engine = engine
        self._state = dict(tournament)
        self._model = model
        self._auto_promotion_installed = False

    def state(self, agent_id):
        return dict(self._state)

    def sync_agent(self, agent, **kwargs):
        policy = kwargs.get('policy') or self.engine.models[agent['id']]
        old = list(self._state.get('active_features') or [])
        new = list(policy.schema.entities)
        if new != old:
            self._state['previous_schema'] = old
            self._state['schema_revision'] = int(self._state.get('schema_revision') or 0) + 1
        self._state['active_features'] = new
        self._state['challenger_features'] = [
            x for x in self._state.get('challenger_features', []) if x not in set(new)
        ]
        return dict(self._state)

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        return {'predictions': [], 'scored': 0}

    def shadow_status(self, agent):
        return {'challengers': [{'entity_id': x} for x in self._state.get('challenger_features', [])]}

    def _load_shadow_model(self, agent_id, challenger, action_count):
        return self._model

    def _save_shadow_model(self, agent_id, challenger, model):
        self._model = model


class SchemaHistoryPromotionIntegrationTests(unittest.TestCase):
    def test_successful_tournament_promotion_is_audited_with_evidence(self):
        with tempfile.TemporaryDirectory(prefix='schema-history-promotion-') as temp:
            store = Store(Path(temp) / 'promotion.db')
            created = store.create_agent(agent(name='Schema history promotion'))
            active = [f'binary_sensor.active_{i}' for i in range(8)]
            challenger = 'binary_sensor.new_presence'
            states = {'light.kitchen': state('light.kitchen', 'off')}
            states.update({eid: state(eid, 'off', device_class='occupancy') for eid in active})
            states[challenger] = state(challenger, 'on', device_class='occupancy')
            policy = MultiHorizonPolicy(created, states, {}, set())
            policy.schema = ExplicitFeatureSchema(policy.dims, active)
            policy.selection_meta = {'selection_reasons': {eid: ['historical'] for eid in active}}
            engine = SimpleNamespace(models={created['id']: policy}, wake_event=threading.Event())
            now = time.time()
            model = {
                'version': 1,
                'action_count': 2,
                'counts': {},
                'samples': 40,
                'active_correct': 28,
                'shadow_correct': 36,
                'last_scored_ts': now - 10,
                'class_totals': [20, 20],
                'active_correct_by_class': [14, 14],
                'shadow_correct_by_class': [18, 18],
                'active_abs_error_sum': 0.0,
                'shadow_abs_error_sum': 0.0,
                'observation_opportunities': 100,
                'available_observations': 100,
                'first_observed_ts': now - 3.1 * 86400.0,
                'last_observed_ts': now,
                'evaluation_started_ts': now - 3.1 * 86400.0,
                'promotion_epoch_version': PROMOTION_EPOCH_VERSION,
                'promotion_window_hours': 24.0,
                'promotion_window_start_ts': now,
                'promotion_window_end_ts': now + 86400.0,
                'promotion_window_samples': 0,
                'promotion_window_class_totals': [0, 0],
                'promotion_window_active_correct_by_class': [0, 0],
                'promotion_window_shadow_correct_by_class': [0, 0],
                'promotion_window_active_abs_error_sum': 0.0,
                'promotion_window_shadow_abs_error_sum': 0.0,
                'promotion_seen_samples': 40,
                'promotion_seen_class_totals': [20, 20],
                'promotion_seen_active_correct_by_class': [14, 14],
                'promotion_seen_shadow_correct_by_class': [18, 18],
                'promotion_seen_active_abs_error_sum': 0.0,
                'promotion_seen_shadow_abs_error_sum': 0.0,
                'promotion_consecutive_wins': 3,
                'promotion_completed_windows': 3,
                'promotion_window_history': [],
            }
            scores = {eid: 0.9 - i * 0.1 for i, eid in enumerate(active)}
            scores[challenger] = 0.99
            tournament = {
                'agent_id': created['id'],
                'active_features': list(active),
                'challenger_features': [challenger],
                'feature_scores': scores,
                'schema_revision': 1,
                'previous_schema': [],
            }
            service = FakeTournamentService(store, engine, tournament, model)
            install_promotion(service)
            install_schema_history(service)

            service.observe_shadow(created, {}, set())

            rows = service.schema_history(created['id'])
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row['old_schema'], active)
            self.assertEqual(row['new_schema'], list(policy.schema.entities))
            self.assertEqual(row['reason'], 'sensor_tournament_promotion')
            self.assertAlmostEqual(row['baseline_score'], 0.70, places=7)
            self.assertAlmostEqual(row['challenger_score'], 0.90, places=7)
            self.assertEqual(row['evaluation_samples'], 40)
            self.assertEqual(row['promoted_entity'], challenger)
            self.assertEqual(row['removed_entity'], active[-1])
            self.assertEqual(row['status'], 'promoted')


if __name__ == '__main__':
    unittest.main()
