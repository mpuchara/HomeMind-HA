import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
from context import ExplicitFeatureSchema
from context_tournament_promotion import (
    PROMOTION_EPOCH_VERSION,
    advance_windows,
    install_promotion,
    promotion_eligibility,
    tournament_config,
)
from policy import MultiHorizonPolicy
from settings import DEFAULT_OPTIONS
from storage import Store


class PromotionMathTests(unittest.TestCase):
    def test_default_thresholds_match_012_contract(self):
        cfg = tournament_config(DEFAULT_OPTIONS)
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['min_samples'], 40)
        self.assertEqual(cfg['min_days'], 3)
        self.assertAlmostEqual(cfg['min_gain'], 0.03)
        self.assertEqual(cfg['consecutive_wins'], 3)
        self.assertEqual(cfg['evaluation_hours'], 24)
        self.assertEqual(cfg['cooldown_hours'], 24)

    def _qualified_model(self, now=400000.0):
        return {
            'samples': 40,
            'class_totals': [20, 20],
            'active_correct_by_class': [14, 14],
            'shadow_correct_by_class': [18, 18],
            'observation_opportunities': 100,
            'available_observations': 100,
            'first_observed_ts': now - 3 * 86400.0,
            'last_observed_ts': now,
            'promotion_consecutive_wins': 3,
            'promotion_completed_windows': 3,
        }

    def test_promotion_requires_every_gate(self):
        now = 400000.0
        model = self._qualified_model(now)
        ready = promotion_eligibility(model, [0.0, 1.0], now, None, DEFAULT_OPTIONS)
        self.assertTrue(ready['ready'])
        self.assertAlmostEqual(ready['gain'], 0.20, places=7)

        cases = [
            ('samples', lambda m: m.update(samples=39, class_totals=[20, 19])),
            ('days', lambda m: m.update(first_observed_ts=now - 2.9 * 86400.0)),
            ('gain', lambda m: m.update(shadow_correct_by_class=[14, 14])),
            ('consecutive_wins', lambda m: m.update(promotion_consecutive_wins=2)),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                candidate = self._qualified_model(now)
                mutate(candidate)
                self.assertFalse(
                    promotion_eligibility(candidate, [0.0, 1.0], now, None, DEFAULT_OPTIONS)['ready']
                )

    def test_cooldown_blocks_otherwise_ready_promotion(self):
        now = 400000.0
        model = self._qualified_model(now)
        status = promotion_eligibility(
            model, [0.0, 1.0], now, now - 23 * 3600.0, DEFAULT_OPTIONS
        )
        self.assertFalse(status['ready'])
        self.assertFalse(status['checks']['cooldown'])
        self.assertAlmostEqual(status['cooldown_remaining_seconds'], 3600.0, places=5)

    def test_three_non_overlapping_windows_must_win_consecutively(self):
        options = dict(DEFAULT_OPTIONS)
        options['context_tournament_evaluation_hours'] = 24
        options['context_tournament_min_gain'] = 0.03
        cfg = tournament_config(options)
        model = {
            'samples': 0,
            'evaluation_started_ts': 0.0,
            'promotion_epoch_version': PROMOTION_EPOCH_VERSION,
            'promotion_window_hours': 24.0,
            'promotion_window_start_ts': 0.0,
            'promotion_window_end_ts': 86400.0,
            'promotion_window_samples': 0,
            'promotion_window_class_totals': [0, 0],
            'promotion_window_active_correct_by_class': [0, 0],
            'promotion_window_shadow_correct_by_class': [0, 0],
            'promotion_window_active_abs_error_sum': 0.0,
            'promotion_window_shadow_abs_error_sum': 0.0,
            'promotion_consecutive_wins': 0,
            'promotion_completed_windows': 0,
            'promotion_window_history': [],
        }
        for window in range(3):
            model['promotion_window_samples'] = 20
            model['promotion_window_class_totals'] = [10, 10]
            model['promotion_window_active_correct_by_class'] = [7, 7]
            model['promotion_window_shadow_correct_by_class'] = [9, 9]
            advance_windows(model, [0.0, 1.0], (window + 1) * 86400.0, cfg)
        self.assertEqual(model['promotion_completed_windows'], 3)
        self.assertEqual(model['promotion_consecutive_wins'], 3)
        self.assertEqual([x['win'] for x in model['promotion_window_history']], [True, True, True])

    def test_losing_middle_window_resets_streak(self):
        options = dict(DEFAULT_OPTIONS)
        cfg = tournament_config(options)
        model = {
            'samples': 0,
            'evaluation_started_ts': 0.0,
            'promotion_epoch_version': PROMOTION_EPOCH_VERSION,
            'promotion_window_hours': 24.0,
            'promotion_window_start_ts': 0.0,
            'promotion_window_end_ts': 86400.0,
            'promotion_window_samples': 0,
            'promotion_window_class_totals': [0, 0],
            'promotion_window_active_correct_by_class': [0, 0],
            'promotion_window_shadow_correct_by_class': [0, 0],
            'promotion_consecutive_wins': 0,
            'promotion_completed_windows': 0,
            'promotion_window_history': [],
        }
        gains = [(7, 9), (9, 7), (7, 9)]
        for window, (active_correct, shadow_correct) in enumerate(gains):
            model['promotion_window_samples'] = 20
            model['promotion_window_class_totals'] = [10, 10]
            model['promotion_window_active_correct_by_class'] = [active_correct, active_correct]
            model['promotion_window_shadow_correct_by_class'] = [shadow_correct, shadow_correct]
            advance_windows(model, [0.0, 1.0], (window + 1) * 86400.0, cfg)
        self.assertEqual(model['promotion_consecutive_wins'], 1)
        self.assertEqual([x['win'] for x in model['promotion_window_history']], [True, False, True])


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


class PromotionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='context-promotion-')
        self.store = Store(Path(self.temp.name) / 'promotion.db')
        created = self.store.create_agent(agent(name='Promotion test'))
        self.agent = created
        self.active = [f'binary_sensor.active_{i}' for i in range(8)]
        self.challenger = 'binary_sensor.new_presence'
        states = {'light.kitchen': state('light.kitchen', 'off')}
        states.update({eid: state(eid, 'off', device_class='occupancy') for eid in self.active})
        states[self.challenger] = state(self.challenger, 'on', device_class='occupancy')
        self.policy = MultiHorizonPolicy(self.agent, states, {}, set())
        self.policy.schema = ExplicitFeatureSchema(self.policy.dims, self.active)
        self.policy.selection_meta = {'selection_reasons': {eid: ['historical'] for eid in self.active}}
        self.engine = SimpleNamespace(
            models={self.agent['id']: self.policy},
            wake_event=threading.Event(),
        )
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
        scores = {eid: 0.9 - i * 0.1 for i, eid in enumerate(self.active)}
        scores[self.challenger] = 0.99
        tournament = {
            'agent_id': self.agent['id'],
            'active_features': list(self.active),
            'challenger_features': [self.challenger],
            'feature_scores': scores,
            'schema_revision': 1,
            'previous_schema': [],
        }
        self.service = FakeTournamentService(self.store, self.engine, tournament, model)
        install_promotion(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_ready_challenger_is_promoted_without_exceeding_fast_limit(self):
        self.service.observe_shadow(self.agent, {}, set())
        active = list(self.policy.schema.entities)
        self.assertEqual(len(active), 8)
        self.assertIn(self.challenger, active)
        # Lowest-ranked existing feature is displaced only to preserve the hard schema cap.
        self.assertNotIn(self.active[-1], active)
        saved = self.store.get_model(self.agent['id'])
        self.assertIn(self.challenger, (saved.get('schema') or {}).get('entities', []))
        status = self.service.promotion_status(self.agent)
        self.assertEqual(status['promoted_entity'], self.challenger)
        self.assertEqual(status['replaced_entity'], self.active[-1])
        self.assertTrue(self.engine.wake_event.is_set())

    def test_disabled_tournament_does_not_promote(self):
        from context_tournament_promotion import OPTIONS as promotion_options
        old = promotion_options.get('context_tournament_enabled', True)
        promotion_options['context_tournament_enabled'] = False
        try:
            self.service.observe_shadow(self.agent, {}, set())
            self.assertNotIn(self.challenger, self.policy.schema.entities)
        finally:
            promotion_options['context_tournament_enabled'] = old


if __name__ == '__main__':
    unittest.main()
