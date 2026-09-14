import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from context_schema_probation import (
    MIN_ROLLBACK_SAMPLES,
    ROLLBACK_MARGIN,
    _blank_stats,
    _score_stats,
    install_schema_probation,
    probation_decision,
)


class ProbationDecisionTests(unittest.TestCase):
    def test_waits_before_minimum_observations(self):
        stats = _blank_stats(2)
        for i in range(MIN_ROLLBACK_SAMPLES - 1):
            actual = float(i % 2)
            stats = _score_stats(stats, [0.0, 1.0], actual, actual, 1.0 - actual)
        decision = probation_decision(stats, [0.0, 1.0], 50)
        self.assertEqual(decision['decision'], 'wait')
        self.assertEqual(decision['samples'], MIN_ROLLBACK_SAMPLES - 1)

    def test_rolls_back_when_new_policy_is_more_than_three_points_worse(self):
        stats = _blank_stats(2)
        for i in range(30):
            actual = float(i % 2)
            old = actual
            new = actual if i not in (0, 1, 2, 3) else 1.0 - actual
            stats = _score_stats(stats, [0.0, 1.0], actual, old, new)
        decision = probation_decision(stats, [0.0, 1.0], 50)
        self.assertEqual(decision['decision'], 'rollback')
        self.assertGreater(decision['old_score'] - decision['new_score'], ROLLBACK_MARGIN)

    def test_exact_three_point_boundary_does_not_rollback(self):
        stats = {
            'samples': 100,
            'class_totals': [50, 50],
            'active_correct_by_class': [42, 42],
            'shadow_correct_by_class': [40, 41],
            'active_abs_error_sum': 0.0,
            'shadow_abs_error_sum': 0.0,
        }
        # old=.84, new=.81 => exactly -3pp, strict '< old - .03' must not rollback.
        decision = probation_decision(stats, [0.0, 1.0], 120)
        self.assertAlmostEqual(decision['old_score'], 0.84)
        self.assertAlmostEqual(decision['new_score'], 0.81)
        self.assertEqual(decision['decision'], 'wait')

    def test_accepts_after_target_samples_when_not_degraded(self):
        stats = _blank_stats(2)
        for i in range(50):
            actual = float(i % 2)
            stats = _score_stats(stats, [0.0, 1.0], actual, actual, actual)
        decision = probation_decision(stats, [0.0, 1.0], 50)
        self.assertEqual(decision['decision'], 'accept')
        self.assertEqual(decision['samples'], 50)

    def test_continuous_uses_same_normalized_mae_contract(self):
        stats = _blank_stats(3)
        for actual in ([0.0, 50.0, 100.0] * 10):
            stats = _score_stats(stats, [0.0, 50.0, 100.0], actual, actual, 50.0)
        decision = probation_decision(stats, [0.0, 50.0, 100.0], 50)
        self.assertEqual(decision['metric'], 'normalized_mae')
        self.assertEqual(decision['decision'], 'rollback')


class FakeStore:
    def __init__(self, root, agent):
        self.path = str(Path(root) / 'probation.db')
        self.lock = threading.RLock()
        self.agent = dict(agent)
        self.model = None
        self.events = []

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get_agent_config(self, agent_id):
        return dict(self.agent) if agent_id == self.agent['id'] else None

    def get_model(self, agent_id):
        return json.loads(json.dumps(self.model)) if self.model is not None else None

    def save_model(self, agent_id, model):
        self.model = json.loads(json.dumps(model))

    def event(self, agent_id, level, kind, message, data):
        self.events.append((agent_id, level, kind, message, data))


class FakeLivePolicy:
    def __init__(self, raw):
        self.raw = json.loads(json.dumps(raw))
        self.schema = SimpleNamespace(entities=list(raw['schema']['entities']))

    def serialize(self):
        return json.loads(json.dumps(self.raw))


class FakeClonePolicy:
    def __init__(self, agent, state_map, registry, hint_entities, model=None,
                 relevance_scores=None, context_engine=None):
        self.model = dict(model or {})
        self.state_map = state_map

    def features(self, state_map, temporal, at_ts=None):
        self.state_map = state_map
        return {}, {}, {}

    def predict(self, features):
        state = str((self.state_map.get('light.test') or {}).get('state') or 'off').lower()
        current = 1.0 if state == 'on' else 0.0
        mode = self.model.get('prediction_mode')
        value = 1.0 - current if mode == 'flip' else current
        return {'value': value}, 1.0, [], 1, 1.0, 0.0


class FakeService:
    def __init__(self, root):
        self.agent = {
            'id': 'agent-a', 'mode': 'shadow', 'enabled': True,
            'target_entity': 'light.test', 'target_property': 'power',
            'min_value': 0.0, 'max_value': 1.0, 'deadband': 0.5,
            'action_interval': 1.0, 'input_entities': ['*'],
        }
        self.old_model = {
            'version': 10, 'schema': {'version': 11, 'dims': 128, 'entities': ['binary_sensor.old']},
            'prediction_mode': 'flip', 'selection_meta': {'selection_reasons': {'binary_sensor.old': ['test']}},
            'model_revision': 'old', 'dims': 128, 'actions': [0.0, 1.0], 'horizons': [1], 'heads': {},
        }
        self.new_model = {
            'version': 10, 'schema': {'version': 11, 'dims': 128, 'entities': ['binary_sensor.new']},
            'prediction_mode': 'same', 'selection_meta': {'selection_reasons': {'binary_sensor.new': ['test']}},
            'model_revision': 'new', 'dims': 128, 'actions': [0.0, 1.0], 'horizons': [1], 'heads': {},
        }
        self.store = FakeStore(root, self.agent)
        self.store.model = json.loads(json.dumps(self.old_model))
        self.engine = SimpleNamespace(
            models={'agent-a': FakeLivePolicy(self.old_model)},
            runtime={'agent-a': {'last_change_origin': 'manual_user'}},
            state_map={}, entity_registry={}, context=None, temporal_history=object(),
            lock=threading.RLock(), wake_event=threading.Event(),
        )
        self.engine.policy = self._policy
        self._history = []
        self.promoted = False

    def _policy(self, agent):
        raw = self.store.get_model(agent['id'])
        policy = FakeLivePolicy(raw)
        self.engine.models[agent['id']] = policy
        return policy

    def schema_history(self, agent_id, limit=50):
        rows = [dict(x) for x in self._history if x['agent_id'] == agent_id]
        return list(reversed(rows))[:limit]

    def set_schema_history_status(self, history_id, status):
        for row in self._history:
            if row['id'] == history_id:
                row['status'] = status
                return True
        return False

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        if not self.promoted:
            self.promoted = True
            self.store.model = json.loads(json.dumps(self.new_model))
            self.engine.models[agent['id']] = FakeLivePolicy(self.new_model)
            self._history.append({
                'id': 1, 'agent_id': agent['id'],
                'old_schema': ['binary_sensor.old'], 'new_schema': ['binary_sensor.new'],
                'reason': 'sensor_tournament_promotion', 'status': 'promoted',
                'promoted_entity': 'binary_sensor.new', 'removed_entity': 'binary_sensor.old',
            })
        return {'ok': True}

    def shadow_status(self, agent):
        return {'mode': 'shadow_only'}


class ProbationIntegrationTests(unittest.TestCase):
    def test_underperforming_promoted_policy_restores_exact_previous_model(self):
        with tempfile.TemporaryDirectory() as root, patch(
            'context_schema_probation.MultiHorizonPolicy', FakeClonePolicy
        ):
            service = FakeService(root)
            install_schema_probation(service)
            agent = service.agent

            # Promotion event: preserve old policy and prepare first paired prediction.
            state = {'light.test': {'entity_id': 'light.test', 'state': 'off', 'attributes': {}}}
            service.engine.state_map = state
            service.observe_shadow(agent, state, {'light.test'})
            probation = service.schema_probation('agent-a')
            self.assertEqual(probation['status'], 'active')
            self.assertEqual(probation['previous_schema'], ['binary_sensor.old'])
            self.assertEqual(probation['previous_model']['model_revision'], 'old')

            # Alternate the target. Old champion predicts the next state (flip), while
            # the promoted policy predicts the current state (same), so it loses paired
            # prequential evidence and must roll back at sample 30.
            for i in range(1, 31):
                value = 'on' if i % 2 else 'off'
                state = {'light.test': {'entity_id': 'light.test', 'state': value, 'attributes': {}}}
                service.engine.state_map = state
                service.observe_shadow(agent, state, {'light.test'})

            probation = service.schema_probation('agent-a')
            self.assertEqual(probation['status'], 'rolled_back')
            self.assertEqual(service._history[0]['status'], 'rolled_back')
            self.assertEqual(service.store.model['model_revision'], 'old')
            self.assertEqual(service.engine.models['agent-a'].schema.entities, ['binary_sensor.old'])
            rollback_events = [e for e in service.store.events if e[2] == 'context_schema_rolled_back']
            self.assertEqual(len(rollback_events), 1)
            self.assertEqual(rollback_events[0][4]['samples'], 30)


if __name__ == '__main__':
    unittest.main()
