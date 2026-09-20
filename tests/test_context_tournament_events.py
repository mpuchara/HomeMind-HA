import sqlite3
import threading
import unittest
from contextlib import contextmanager

from context_tournament_events import EVENT_MESSAGES, install_context_events


class FakeStore:
    def __init__(self, agent):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.agent = dict(agent)
        self.events = []

    @contextmanager
    def conn(self):
        with self.db:
            yield self.db

    def event(self, agent_id, level, kind, message, data=None):
        self.events.append({
            'agent_id': agent_id, 'level': level, 'kind': kind,
            'message': message, 'data': data,
        })

    def get_agent_config(self, agent_id):
        return dict(self.agent) if str(agent_id) == str(self.agent['id']) else None


class FakeService:
    def __init__(self):
        self.agent = {
            'id': 'a1', 'target_entity': 'light.kitchen', 'target_property': 'power',
            'min_value': 0.0, 'max_value': 1.0, 'deadband': 0.01,
        }
        self.store = FakeStore(self.agent)
        self.models = {
            'sensor.new': {
                'samples': 20,
                'class_totals': [10, 10],
                'active_correct_by_class': [8, 8],
                'shadow_correct_by_class': [9, 9],
                'observation_opportunities': 100,
                'available_observations': 90,
                'first_observed_ts': 1000.0,
                'last_observed_ts': 1000.0 + 4.2 * 86400.0,
                'evaluation_started_ts': 900.0,
                'promotion_completed_windows': 0,
                'promotion_window_history': [],
            },
            'sensor.old_challenger': {
                'samples': 12,
                'class_totals': [6, 6],
                'active_correct_by_class': [5, 5],
                'shadow_correct_by_class': [5, 4],
                'observation_opportunities': 50,
                'available_observations': 45,
                'first_observed_ts': 1000.0,
                'last_observed_ts': 1000.0 + 2.0 * 86400.0,
                'evaluation_started_ts': 800.0,
                'promotion_completed_windows': 0,
                'promotion_window_history': [],
            },
        }
        self.current = {
            'agent_id': 'a1',
            'active_features': ['sensor.old'],
            'challenger_features': ['sensor.new'],
            'feature_scores': {'sensor.old': 0.60, 'sensor.new': 0.72},
            'last_evaluation': 1000.0,
            'schema_revision': 3,
            'previous_schema': [],
        }
        self.next_sync = dict(self.current)
        self._sensor_quality_replacement_previews = {
            ('a1', 'sensor.new'): {
                'replaced': 'sensor.old', 'required_gain': 0.03,
                'quality_adjusted_gain': 0.09,
                'challenger_ranking_score': 0.70,
                'incumbent_ranking_score': 0.58,
            }
        }
        self.mutate_on_observe = None

    def state(self, agent_id):
        return dict(self.current)

    def sync_agent(self, agent, **kwargs):
        self.current = dict(self.next_sync)
        return dict(self.current)

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        if self.mutate_on_observe:
            self.mutate_on_observe()
        return {'scored': 1}

    def _load_shadow_model(self, agent_id, entity_id, action_count):
        return self.models.setdefault(entity_id, {})

    def sensor_quality(self, agent_id, entity_id, now=None):
        return {
            'availability': 0.70,
            'unknown_rate': 0.20,
            'unavailable_rate': 0.10,
            'event_frequency': 2.5,
            'stale_time': 120.0,
            'recent_failures': 3,
            'opportunities': 100,
            'available_observations': 70,
            'event_count': 30,
            'sensor_quality': 0.70,
        }

    def schema_history_by_id(self, history_id):
        return {
            'id': history_id,
            'old_schema': ['sensor.old'],
            'new_schema': ['sensor.new'],
            'baseline_score': 0.82,
            'challenger_score': 0.88,
            'evaluation_samples': 143,
        }


class ContextTournamentEventTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        install_context_events(self.service)

    def event(self, kind):
        rows = [row for row in self.service.store.events if row['kind'] == kind]
        self.assertTrue(rows, f'missing event {kind}')
        return rows[-1]

    def test_existing_promotion_event_is_numeric_and_has_fixed_message(self):
        self.service.store.event(
            'a1', 'info', 'context_feature_promoted', 'free form explanation',
            {'promoted': 'sensor.new', 'replaced': 'sensor.old',
             'consecutive_wins': 3, 'completed_windows': 3},
        )
        row = self.event('context_feature_promoted')
        self.assertEqual(row['message'], EVENT_MESSAGES['context_feature_promoted'])
        data = row['data']
        self.assertEqual(data['added'], 'sensor.new')
        self.assertEqual(data['removed'], 'sensor.old')
        self.assertAlmostEqual(data['old_score'], 0.80)
        self.assertAlmostEqual(data['new_score'], 0.90)
        self.assertAlmostEqual(data['gain'], 0.10)
        self.assertEqual(data['samples'], 20)
        self.assertAlmostEqual(data['days'], 4.2)
        self.assertEqual(data['required_wins'], 3)
        self.assertIsInstance(data['schema_revision'], int)

    def test_probation_event_names_and_numeric_contract(self):
        self.service.store.event(
            'a1', 'info', 'context_schema_probation_started', 'old text',
            {'history_id': 7, 'probation_samples': 50},
        )
        started = self.event('context_schema_probation_started')
        self.assertEqual(started['message'], EVENT_MESSAGES['context_schema_probation_started'])
        self.assertAlmostEqual(started['data']['old_score'], 0.82)
        self.assertAlmostEqual(started['data']['new_score'], 0.88)
        self.assertAlmostEqual(started['data']['gain'], 0.06)
        self.assertEqual(started['data']['samples'], 143)
        self.assertGreaterEqual(started['data']['probation_samples'], 30)

        self.service.store.event(
            'a1', 'info', 'context_schema_probation_accepted', 'old text',
            {'history_id': 7, 'samples': 50, 'old_score': 0.82,
             'new_score': 0.86, 'delta': 0.04},
        )
        accepted = self.event('context_schema_accepted')
        self.assertEqual(accepted['message'], EVENT_MESSAGES['context_schema_accepted'])
        self.assertAlmostEqual(accepted['data']['gain'], 0.04)
        self.assertEqual(accepted['data']['samples'], 50)
        self.assertNotIn('context_schema_probation_accepted', [e['kind'] for e in self.service.store.events])

        self.service.store.event(
            'a1', 'warning', 'context_schema_rolled_back', 'old text',
            {'history_id': 7, 'samples': 34, 'old_score': 0.84,
             'new_score': 0.79, 'delta': -0.05, 'restored_schema': ['sensor.old']},
        )
        rolled = self.event('context_schema_rolled_back')
        self.assertAlmostEqual(rolled['data']['rollback_threshold'], 0.81)
        self.assertEqual(rolled['data']['min_rollback_samples'], 30)
        self.assertEqual(rolled['data']['restored_schema_size'], 1)

    def test_challenger_started_is_deduplicated_per_epoch(self):
        self.service.sync_agent(self.service.agent)
        first = self.event('context_challenger_started')
        self.assertEqual(first['data']['entity_id'], 'sensor.new')
        self.assertEqual(first['data']['selection_rank'], 1)
        self.assertAlmostEqual(first['data']['feature_score'], 0.72)
        self.assertEqual(first['data']['schema_revision'], 3)
        self.assertEqual(first['data']['evaluation_reason'], 'challenger_selected')
        self.assertIn('evaluation_champion_revision', first['data'])
        count = len([e for e in self.service.store.events if e['kind'] == 'context_challenger_started'])
        self.service.sync_agent(self.service.agent)
        self.assertEqual(
            len([e for e in self.service.store.events if e['kind'] == 'context_challenger_started']),
            count,
        )

    def test_completed_window_emits_prequential_evaluation_numbers(self):
        self.service.sync_agent(self.service.agent)

        def mutate():
            model = self.service.models['sensor.new']
            model['promotion_completed_windows'] = 1
            model['promotion_consecutive_wins'] = 1
            model['promotion_window_history'] = [{
                'start_ts': 1000.0, 'end_ts': 1000.0 + 86400.0,
                'samples': 45, 'baseline_score': 0.82,
                'challenger_score': 0.87, 'gain': 0.05, 'win': True,
            }]
        self.service.mutate_on_observe = mutate
        self.service.observe_shadow(self.service.agent, {})
        row = self.event('context_challenger_evaluated')
        self.assertEqual(row['message'], EVENT_MESSAGES['context_challenger_evaluated'])
        self.assertAlmostEqual(row['data']['old_score'], 0.82)
        self.assertAlmostEqual(row['data']['new_score'], 0.87)
        self.assertAlmostEqual(row['data']['gain'], 0.05)
        self.assertEqual(row['data']['samples'], 45)
        self.assertEqual(row['data']['window_index'], 1)
        self.assertTrue(row['data']['win'])

    def test_pool_drop_emits_rejected_with_numeric_rank_evidence(self):
        self.service.sync_agent(self.service.agent)
        self.service.next_sync = {
            **self.service.current,
            'challenger_features': [],
            'feature_scores': {'sensor.old': 0.60},
        }
        self.service.sync_agent(self.service.agent)
        row = self.event('context_feature_rejected')
        self.assertEqual(row['message'], EVENT_MESSAGES['context_feature_rejected'])
        self.assertEqual(row['data']['entity_id'], 'sensor.new')
        self.assertEqual(row['data']['reason_code'], 'challenger_pool_changed')
        self.assertAlmostEqual(row['data']['feature_score'], 0.72)
        self.assertEqual(row['data']['challenger_count'], 0)
        self.assertIsInstance(row['data']['samples'], int)
        self.assertIsInstance(row['data']['days'], float)

    def test_quality_block_emits_unreliable_once_per_epoch(self):
        self.service.sync_agent(self.service.agent)

        def mutate():
            self.service.models['sensor.new']['promotion_blocked_reason'] = 'sensor_quality'
        self.service.mutate_on_observe = mutate
        self.service.observe_shadow(self.service.agent, {})
        row = self.event('context_sensor_unreliable')
        data = row['data']
        self.assertEqual(row['message'], EVENT_MESSAGES['context_sensor_unreliable'])
        self.assertAlmostEqual(data['availability'], 0.70)
        self.assertAlmostEqual(data['unknown_rate'], 0.20)
        self.assertAlmostEqual(data['unavailable_rate'], 0.10)
        self.assertAlmostEqual(data['event_frequency'], 2.5)
        self.assertEqual(data['recent_failures'], 3)
        self.assertAlmostEqual(data['required_gain'], 0.03)
        count = len([e for e in self.service.store.events if e['kind'] == 'context_sensor_unreliable'])
        self.service.observe_shadow(self.service.agent, {})
        self.assertEqual(
            len([e for e in self.service.store.events if e['kind'] == 'context_sensor_unreliable']),
            count,
        )

    def test_contract_has_exact_requested_event_names_and_no_generated_text(self):
        expected = {
            'context_challenger_started', 'context_challenger_evaluated',
            'context_feature_promoted', 'context_feature_rejected',
            'context_schema_probation_started', 'context_schema_accepted',
            'context_schema_rolled_back', 'context_sensor_unreliable',
        }
        self.assertEqual(set(self.service.context_event_contract['events']), expected)
        self.assertFalse(self.service.context_event_contract['generated_text'])
        self.assertEqual(self.service.context_event_contract['decision_evidence'], 'structured_numeric')


if __name__ == '__main__':
    unittest.main()
