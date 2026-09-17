import random
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
import context_tournament_promotion as promotion
from context import ExplicitFeatureSchema
from context_tournament_policy_candidate import (
    CONTRACT_VERSION,
    install_policy_candidates,
    plan_target_schema,
    promotion_gate,
    redundancy_score,
    semantic_predictive_score,
)
from manual_context_learning import _migrate_schema
from policy import MultiHorizonPolicy
from storage import Store


class SemanticScreeningTests(unittest.TestCase):
    def test_sensor_useful_only_as_precursor_is_screened_by_lag_group(self):
        samples = []
        for i in range(24):
            label = float(i % 2)
            # Current value is deliberately uninformative; the lagged value carries the
            # future target state and therefore deserves investigation as a precursor.
            samples.append({
                'episode': str(i), 'label': label,
                'value': float((i // 2) % 2), 'quality': 1.0,
                'lag_1': label, 'lag_3': label, 'lag_10': 1.0 - label,
                'trend': 0.0, 'time_since_edge': 0.5, 'interactions': {},
            })
        result = semantic_predictive_score(samples)
        self.assertGreater(result['score'], 0.8)
        self.assertIn(result['best_group'], {'lag_1', 'lag_3', 'lag_10'})
        self.assertNotEqual(result['best_group'], 'value')

    def test_dependency_visible_only_in_pair_is_screened_by_selected_interaction(self):
        samples = []
        # Balanced XOR/equality pattern: x alone has zero marginal correlation with the
        # label, but x * primary is perfectly predictive.
        pattern = [(-1.0, -1.0), (-1.0, 1.0), (1.0, -1.0), (1.0, 1.0)] * 8
        for i, (x, primary) in enumerate(pattern):
            label = 1.0 if x == primary else 0.0
            samples.append({
                'episode': str(i), 'label': label, 'value': x, 'quality': 1.0,
                'lag_1': None, 'lag_3': None, 'lag_10': None, 'trend': None,
                'time_since_edge': 0.5,
                'interactions': {'binary_sensor.primary': x * primary},
            })
        result = semantic_predictive_score(samples)
        self.assertLess(abs(result['groups'].get('value', 0.0)), 0.1)
        self.assertEqual(result['best_group'], 'interaction:binary_sensor.primary')
        self.assertGreater(result['score'], 0.9)

    def test_duplicate_sensor_is_detected_as_redundant(self):
        a = [{'episode': str(i), 'value': float(i % 2)} for i in range(20)]
        b = [{'episode': str(i), 'value': float(i % 2)} for i in range(20)]
        result = redundancy_score(a, b)
        self.assertEqual(result['samples'], 20)
        self.assertGreater(result['score'], 0.99)

    def test_random_historical_screen_does_not_become_promotion_evidence(self):
        rng = random.Random(42)
        samples = []
        for i in range(48):
            samples.append({
                'episode': str(i), 'label': float(i % 2), 'value': rng.uniform(-1.0, 1.0),
                'quality': 1.0, 'lag_1': rng.uniform(-1.0, 1.0),
                'lag_3': rng.uniform(-1.0, 1.0), 'lag_10': rng.uniform(-1.0, 1.0),
                'trend': rng.uniform(-1.0, 1.0), 'time_since_edge': rng.random(),
                'interactions': {},
            })
        # Screening is allowed to be non-zero by chance; future predictive gain remains
        # authoritative and a neutral paired result must block promotion.
        screening = semantic_predictive_score(samples)
        self.assertIn(screening['status'], {'screened', 'neutral'})
        gate = promotion_gate(
            model={'candidate_policy': {'schema': {'entities': ['sensor.random']}},
                   'candidate_target_schema': ['sensor.random'], 'candidate_training_samples': 48},
            gain=0.0,
            health={'health_ready': True, 'availability': 1.0, 'health': 1.0,
                    'change_count': 48, 'own_action_leak_rate': 0.0},
            redundancy=None, duplicate_of=None, test_count=64,
            active_schema=['binary_sensor.primary'], target_schema=['binary_sensor.primary', 'sensor.random'],
        )
        self.assertFalse(gate['ready'])
        self.assertEqual(gate['reason'], 'predictive_gain')
        self.assertGreater(gate['required_gain'], 0.03)


class SchemaPlanningTests(unittest.TestCase):
    def test_broken_primary_can_be_replaced_but_healthy_primary_is_protected(self):
        a = agent(name='broken primary')
        states = {'light.kitchen': state('light.kitchen', 'off')}
        active = [f'binary_sensor.active_{i}' for i in range(8)]
        states.update({eid: state(eid, 'off', device_class='occupancy') for eid in active})
        states['binary_sensor.new'] = state('binary_sensor.new', 'on', device_class='occupancy')
        policy = MultiHorizonPolicy(a, states, {}, set())
        policy.schema = ExplicitFeatureSchema(policy.dims, active)
        policy.selection_meta = {
            'primary_occupancy_sensor': active[0],
            'primary_local_sensor': active[0],
            'primary_local_sensors': [active[0]],
            'selection_reasons': {eid: ['historical'] for eid in active},
        }
        tournament = {'feature_scores': {eid: 0.8 for eid in active}}

        def health(eid):
            if eid == active[0]:
                return {'health_ready': True, 'availability': 0.2}
            return {'health_ready': True, 'availability': 0.99}

        plan = plan_target_schema(a, policy, 'binary_sensor.new', tournament, health)
        self.assertEqual(plan['replaced'], active[0])
        self.assertTrue(plan['replacement_is_primary'])
        self.assertTrue(plan['primary_broken'])
        self.assertIn('binary_sensor.new', plan['schema'])

    def test_new_sensor_without_history_needs_more_data(self):
        gate = promotion_gate(
            model={'candidate_policy': {'schema': {'entities': ['sensor.new']}},
                   'candidate_target_schema': ['sensor.new'], 'candidate_training_samples': 1},
            gain=0.25,
            health={'health_ready': False, 'availability': 1.0, 'health': 1.0,
                    'change_count': 1, 'own_action_leak_rate': 0.0},
            redundancy=None, duplicate_of=None, test_count=1,
            active_schema=[], target_schema=['sensor.new'],
        )
        self.assertFalse(gate['ready'])
        self.assertEqual(gate['reason'], 'sensor_health')

    def test_control_without_independent_evidence_cannot_promote(self):
        gate = promotion_gate(
            model={'candidate_policy': {'schema': {'entities': ['sensor.precursor']}},
                   'candidate_target_schema': ['sensor.precursor'], 'candidate_training_samples': 0},
            gain=None,
            health={'health_ready': True, 'availability': 1.0, 'health': 1.0,
                    'change_count': 20, 'own_action_leak_rate': 0.0},
            redundancy=None, duplicate_of=None, test_count=4,
            active_schema=['binary_sensor.primary'],
            target_schema=['binary_sensor.primary', 'sensor.precursor'],
        )
        self.assertFalse(gate['ready'])
        self.assertIn(gate['reason'], {'candidate_policy_untrained', 'predictive_gain'})

    def test_own_action_effect_leakage_blocks_even_with_apparent_gain(self):
        gate = promotion_gate(
            model={'candidate_policy': {'schema': {'entities': ['sensor.lux']}},
                   'candidate_target_schema': ['sensor.lux'], 'candidate_training_samples': 50},
            gain=0.30,
            health={'health_ready': True, 'availability': 1.0, 'health': 1.0,
                    'change_count': 20, 'own_action_leak_rate': 0.75},
            redundancy=None, duplicate_of=None, test_count=3,
            active_schema=['binary_sensor.primary'],
            target_schema=['binary_sensor.primary', 'sensor.lux'],
        )
        self.assertFalse(gate['ready'])
        self.assertEqual(gate['reason'], 'own_action_leakage')


class ExactPolicyPromotionTests(unittest.TestCase):
    class FakeService:
        def __init__(self, store, engine, tournament, shadow_model):
            self.store = store
            self.engine = engine
            self._state = dict(tournament)
            self._model = shadow_model
            self.lock = threading.RLock()
            self._cache = {str(tournament['agent_id']): dict(tournament)}
            self._shadow_models = {}
            self._policy_candidate_installed = False

        def state(self, agent_id):
            return dict(self._state)

        def sync_agent(self, agent, **kwargs):
            return dict(self._state)

        def observe_shadow(self, agent, state_map=None, changed_entities=None):
            return {'predictions': [], 'scored': 0}

        def shadow_status(self, agent):
            return {'challengers': []}

        def _load_shadow_model(self, agent_id, challenger, action_count):
            return self._model

        def _save_shadow_model(self, agent_id, challenger, model):
            self._model = model

        def _shadow_predict_index(self, model, active_idx, bucket):
            return int(active_idx)

        def _score_shadow_sample(self, agent_id, challenger, pending, actual_idx, action_count, now):
            return None

        def _blank_shadow_model(self, action_count):
            return {'version': 1, 'action_count': action_count, 'counts': {}, 'samples': 0,
                    'active_correct': 0, 'shadow_correct': 0, 'last_scored_ts': None}

        def _eligible_entities(self, agent, active):
            return set()

    def test_promotion_copies_trained_candidate_weights_not_zero_column(self):
        temp = tempfile.TemporaryDirectory(prefix='stage09-exact-policy-')
        old_chooser = promotion._choose_schema_after_promotion
        old_migrate = promotion._migrate_schema
        try:
            store = Store(Path(temp.name) / 'stage09.db')
            a = store.create_agent(agent(name='exact policy'))
            active = [f'binary_sensor.active_{i}' for i in range(7)]
            challenger = 'binary_sensor.precursor'
            states = {'light.kitchen': state('light.kitchen', 'off')}
            states.update({eid: state(eid, 'off', device_class='occupancy') for eid in active})
            states[challenger] = state(challenger, 'on', device_class='occupancy')
            live = MultiHorizonPolicy(a, states, {}, set())
            live.schema = ExplicitFeatureSchema(live.dims, active)
            live.selection_meta = {'selection_reasons': {eid: ['historical'] for eid in active}}

            candidate = MultiHorizonPolicy(a, states, {}, set(), model=live.serialize())
            target_schema = active + [challenger]
            meta = dict(candidate.selection_meta)
            meta['sensor_tournament_promoted'] = challenger
            _migrate_schema(candidate, target_schema, meta)
            value_idx = next(
                idx for idx, labels in candidate.schema.labels().items()
                if f'{challenger}:value' in labels
            )
            horizon = min(candidate.horizons)
            candidate.heads[horizon].a[0][value_idx] = 2.0
            candidate.heads[horizon].b[0][value_idx] = 3.25
            candidate.model_revision = 'trained-stage09-candidate'

            tournament = {
                'agent_id': a['id'], 'active_features': list(active),
                'challenger_features': [challenger], 'feature_scores': {challenger: 0.9},
                'schema_revision': 1, 'previous_schema': [],
            }
            shadow_model = {
                'candidate_contract_version': CONTRACT_VERSION,
                'candidate_target_schema': list(target_schema),
                'candidate_replaced_entity': None,
                'candidate_policy': candidate.serialize(),
                'candidate_training_samples': 50,
                'evaluation_champion_revision': live.model_revision,
            }
            engine = SimpleNamespace(
                models={a['id']: live}, state_map=states, entity_registry={}, context_relevance={},
                context=None, temporal_history=None, state_revision=10, runtime={}, lock=threading.RLock(),
            )
            service = self.FakeService(store, engine, tournament, shadow_model)
            install_policy_candidates(service)

            promotion_meta = dict(meta)
            result = promotion._migrate_schema(live, target_schema, promotion_meta)
            self.assertTrue(result['exact_candidate_policy'])
            self.assertEqual(live.model_revision, 'trained-stage09-candidate')
            self.assertAlmostEqual(live.heads[horizon].b[0][value_idx], 3.25)
            # The old bug would leave the just-added feature at its prior/zero weight.
            self.assertNotEqual(live.heads[horizon].b[0][value_idx], 0.0)
        finally:
            promotion._choose_schema_after_promotion = old_chooser
            promotion._migrate_schema = old_migrate
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
