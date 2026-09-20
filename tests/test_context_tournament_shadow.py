import tempfile
import threading
import unittest
import json
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from support import *
from context_tournament import ContextTournament, install
from storage import Store
from context_tournament_quality import install_sensor_quality
import context_tournament_promotion as promotion


class FakeEngine:
    def __init__(self, states, scores, policy):
        self.state_map = dict(states)
        self.entity_registry = {}
        self.context_relevance = {'agent-1': dict(scores)}
        self.models = {}
        self.runtime = {}
        self.lock = threading.RLock()
        self._policy = policy
        self.policy_calls = 0
        self.active_prediction = 0.0
        self.next_origin = 'manual_user'
        self._last_target = None
        self.executor_calls = 0

    def policy(self, agent):
        self.policy_calls += 1
        return self._policy

    def runtime_for(self, agent):
        return {'selected_context_entities': list(self._policy.schema.entities)}

    def process_agent(self, agent, state_map, changed_entities=None):
        current = 1.0 if str(state_map[agent['target_entity']]['state']).lower() != 'off' else 0.0
        rt = self.runtime.setdefault(agent['id'], {})
        if self._last_target is not None and current != self._last_target:
            rt['last_change_origin'] = self.next_origin
        self._last_target = current
        rt['last_prediction'] = float(self.active_prediction)
        # This fake production path intentionally does not call an Executor. If the
        # tournament tried to control independently, the tests would have no such surface.
        return 'production-result'


def st(entity_id, value, **attrs):
    return {
        'entity_id': entity_id,
        'state': value,
        'attributes': attrs,
        'last_changed': '2026-09-14T12:00:00+00:00',
        'last_updated': '2026-09-14T12:00:00+00:00',
        'context': {},
    }


class ContextTournamentShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='context-shadow-')
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
        self.policy = SimpleNamespace(schema=SimpleNamespace(entities=[self.active]))
        self.engine = FakeEngine(self.states, {self.challenger: .95}, self.policy)
        self.service = install(self.store, self.engine)
        # One normal champion-policy lookup establishes tournament membership.
        self.engine.policy(self.agent)
        self.assertEqual(self.service.state(self.agent['id'])['challenger_features'], [self.challenger])
        self.engine.policy_calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def _process(self, target_state, origin='manual_user'):
        self.engine.next_origin = origin
        self.states[self.target] = st(self.target, target_state)
        self.engine.state_map = dict(self.states)
        return self.engine.process_agent(self.agent, dict(self.states), {self.target})

    def test_shadow_runs_without_rebuilding_policy_or_touching_schema(self):
        before = list(self.policy.schema.entities)
        for value in ('off', 'on', 'off', 'on'):
            self._process(value)
        self.assertEqual(self.engine.policy_calls, 0)
        self.assertEqual(self.policy.schema.entities, before)
        self.assertEqual(self.service.state(self.agent['id'])['active_features'], before)
        self.assertFalse(hasattr(self.service, 'submit'))
        self.assertFalse(hasattr(self.service, 'execute'))

    def test_sensor_quality_persists_updates_and_shadow_continues_after_restart(self):
        # The quality extension patches a module-level promotion hook; isolate it.
        with patch.object(promotion, '_choose_schema_after_promotion',
                          promotion._choose_schema_after_promotion):
            install_sensor_quality(self.service)
            with patch('context_tournament_quality.time.time', return_value=200000.0):
                self.assertEqual(self._process('off'), 'production-result')
            with self.store.conn() as c:
                rows = c.execute('SELECT * FROM context_tournament_sensor_quality').fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row['opportunities'], 1)
                self.assertEqual(row['available_count'], 1)
                self.assertEqual(row['first_observed_ts'], 200000.0)
                self.assertEqual(json.loads(row['failure_timestamps_json']), [])

            self.states[self.challenger] = st(self.challenger, 'unknown')
            with patch('context_tournament_quality.time.time', return_value=200010.0):
                self.engine.process_agent(self.agent, self.states, {self.challenger})
            with self.store.conn() as c:
                row = c.execute('SELECT * FROM context_tournament_sensor_quality WHERE entity_id=?',
                                (self.challenger,)).fetchone()
            self.assertEqual(row['opportunities'], 2)
            self.assertEqual(row['available_count'], 1)
            self.assertEqual(row['unknown_count'], 1)
            self.assertEqual(row['event_count'], 1)
            self.assertEqual(row['last_observed_ts'], 200000.0)
            self.assertEqual(row['last_event_ts'], 200010.0)
            self.assertEqual(row['updated_ts'], 200010.0)
            self.assertEqual(json.loads(row['failure_timestamps_json']), [200010.0])

            # A fresh service must reload persisted counters, not start from zero.
            restarted = ContextTournament(self.store, self.engine)
            install_sensor_quality(restarted)
            self.states[self.challenger] = st(self.challenger, 'unavailable')
            with patch('context_tournament_quality.time.time', return_value=200020.0):
                restarted.observe_shadow(self.agent, self.states, {self.challenger})
            stats = restarted.sensor_quality(self.agent['id'], self.challenger, 200020.0)
            self.assertEqual(stats['opportunities'], 3)
            self.assertAlmostEqual(stats['availability'], 1 / 3)
            self.assertAlmostEqual(stats['unknown_rate'], 1 / 3)
            self.assertAlmostEqual(stats['unavailable_rate'], 1 / 3)
            self.assertEqual(stats['event_count'], 2)
            self.assertEqual(stats['recent_failures'], 2)

            self.states[self.challenger] = st(self.challenger, 'on')
            self._process('off')
            self._process('on')
            status = self.service.shadow_status(self.agent)
            self.assertEqual(status['challengers'][0]['samples'], 1)
            self.assertEqual(self.policy.schema.entities, [self.active])

    def test_challenger_predicts_in_shadow_and_learns_prequentially(self):
        # First prediction has no challenger evidence, so shadow conservatively equals
        # the active prediction (OFF).
        self._process('off')
        status = self.engine.runtime_for(self.agent)['context_tournament']['shadow_evaluation']
        row = status['challengers'][0]
        self.assertEqual(row['active_prediction'], 0.0)
        self.assertEqual(row['prediction'], 0.0)
        self.assertEqual(row['samples'], 0)
        self.assertFalse(status['controls_device'])
        self.assertFalse(status['rebuilds_policy'])

        # A manual ON transition scores the *previous* shadow prediction first, then
        # teaches the small residual table. The new shadow prediction can now differ from
        # champion while remaining non-controlling.
        self._process('on', origin='manual_user')
        status = self.engine.runtime_for(self.agent)['context_tournament']['shadow_evaluation']
        row = status['challengers'][0]
        self.assertEqual(row['samples'], 1)
        self.assertEqual(row['active_accuracy'], 0.0)
        self.assertEqual(row['shadow_accuracy'], 0.0)
        self.assertEqual(row['active_prediction'], 0.0)
        self.assertEqual(row['prediction'], 1.0)
        self.assertEqual(self.engine.policy_calls, 0)

    def test_own_command_transition_is_not_shadow_training_evidence(self):
        self._process('off')
        self._process('on', origin='own_command')
        status = self.engine.runtime_for(self.agent)['context_tournament']['shadow_evaluation']
        row = status['challengers'][0]
        self.assertEqual(row['samples'], 0)
        self.assertIsNone(row['active_accuracy'])
        self.assertIsNone(row['shadow_accuracy'])

    def test_shadow_model_persists_without_persisting_pending_prediction(self):
        self._process('off')
        self._process('on', origin='manual_user')
        current = self.engine.runtime_for(self.agent)['context_tournament']['shadow_evaluation']
        self.assertEqual(current['challengers'][0]['samples'], 1)

        restarted_engine = FakeEngine(self.states, {self.challenger: .95}, self.policy)
        restarted = ContextTournament(self.store, restarted_engine)
        loaded = restarted.state(self.agent['id'])
        self.assertEqual(loaded['challenger_features'], [self.challenger])
        status = restarted.shadow_status(self.agent)
        self.assertEqual(status['challengers'][0]['samples'], 1)
        self.assertIsNone(status['challengers'][0]['prediction'])

    def test_shadow_status_is_diagnostics_only(self):
        self._process('off')
        payload = self.engine.runtime_for(self.agent)['context_tournament']
        shadow = payload['shadow_evaluation']
        self.assertEqual(shadow['mode'], 'shadow_only')
        self.assertFalse(shadow['controls_device'])
        self.assertFalse(shadow['rebuilds_policy'])
        self.assertEqual(len(payload['active_features']), 1)
        self.assertEqual(len(payload['challenger_features']), 1)
        self.assertNotIn(self.challenger, payload['active_features'])


if __name__ == '__main__':
    unittest.main()
