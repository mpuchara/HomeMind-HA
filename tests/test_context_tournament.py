import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import *
from context_tournament import ContextTournament, install
from storage import Store


class FakeEngine:
    def __init__(self, states, scores=None, policy=None):
        self.state_map = dict(states)
        self.entity_registry = {}
        self.context_relevance = {'agent-1': dict(scores or {})}
        self.models = {}
        self.lock = threading.RLock()
        self._policy = policy

    def policy(self, agent):
        return self._policy

    def runtime_for(self, agent):
        return {'selected_context_entities': list(self._policy.schema.entities) if self._policy else []}


def make_state(entity_id, value='1', **attrs):
    return {
        'entity_id': entity_id,
        'state': value,
        'attributes': attrs,
        'last_changed': '2026-09-14T12:00:00+00:00',
        'last_updated': '2026-09-14T12:00:00+00:00',
        'context': {},
    }


class ContextTournamentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='context-tournament-')
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.agent = {
            'id': 'agent-1',
            'target_entity': 'light.kitchen',
            'target_property': 'power',
            'min_value': 0,
            'max_value': 1,
        }
        self.active = [f'binary_sensor.active_{i}' for i in range(8)]
        self.candidates = [f'sensor.candidate_{i}' for i in range(6)]
        self.power = 'sensor.mains_power'
        states = {'light.kitchen': make_state('light.kitchen', 'off')}
        states.update({eid: make_state(eid, 'on', device_class='occupancy') for eid in self.active})
        states.update({eid: make_state(eid, str(i + 1)) for i, eid in enumerate(self.candidates)})
        states[self.power] = make_state(self.power, '500', unit_of_measurement='W', device_class='power')
        scores = {eid: 0.95 - i * 0.05 for i, eid in enumerate(self.candidates)}
        scores[self.power] = 1.0
        self.policy = SimpleNamespace(schema=SimpleNamespace(entities=list(self.active)))
        self.engine = FakeEngine(states, scores=scores, policy=self.policy)
        self.service = ContextTournament(self.store, self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def test_fast_agent_keeps_eight_active_features_and_four_separate_challengers(self):
        result = self.service.sync_agent(self.agent, policy=self.policy)
        self.assertEqual(result['active_features'], self.active)
        self.assertEqual(len(result['active_features']), 8)
        self.assertEqual(result['challenger_features'], self.candidates[:4])
        self.assertTrue(set(result['active_features']).isdisjoint(result['challenger_features']))
        self.assertNotIn(self.power, result['challenger_features'])
        self.assertEqual(result['schema_revision'], 1)
        self.assertEqual(result['previous_schema'], [])
        self.assertIsNotNone(result['last_evaluation'])

    def test_configured_challenger_count_is_respected(self):
        with patch.dict('context_tournament.OPTIONS', {'context_challenger_count': 2}):
            result = self.service.sync_agent(self.agent, policy=self.policy)
        self.assertEqual(result['challenger_features'], self.candidates[:2])
        self.assertEqual(result['active_features'], self.active)

    def test_schema_change_increments_revision_and_preserves_previous_schema(self):
        first = self.service.sync_agent(self.agent, active_features=self.active)
        replacement = self.active[:-1] + ['binary_sensor.new_primary']
        self.engine.state_map['binary_sensor.new_primary'] = make_state(
            'binary_sensor.new_primary', 'on', device_class='occupancy'
        )
        second = self.service.sync_agent(self.agent, active_features=replacement)
        self.assertEqual(first['schema_revision'], 1)
        self.assertEqual(second['schema_revision'], 2)
        self.assertEqual(second['previous_schema'], self.active)
        self.assertEqual(second['active_features'], replacement)

    def test_state_survives_service_restart(self):
        written = self.service.sync_agent(self.agent, policy=self.policy)
        restarted = ContextTournament(self.store, self.engine)
        loaded = restarted.state(self.agent['id'])
        self.assertEqual(loaded['active_features'], written['active_features'])
        self.assertEqual(loaded['challenger_features'], written['challenger_features'])
        self.assertEqual(loaded['feature_scores'], written['feature_scores'])
        self.assertEqual(loaded['schema_revision'], written['schema_revision'])
        self.assertEqual(loaded['previous_schema'], written['previous_schema'])
        self.assertEqual(loaded['last_evaluation'], written['last_evaluation'])

    def test_install_exposes_tournament_in_agent_runtime_without_dispatching_actions(self):
        service = install(self.store, self.engine)
        returned_policy = self.engine.policy(self.agent)
        runtime = self.engine.runtime_for(self.agent)
        self.assertIs(returned_policy, self.policy)
        self.assertIs(self.engine.context_tournament, service)
        self.assertIn('context_tournament', runtime)
        tournament = runtime['context_tournament']
        self.assertEqual(tournament['active_features'], self.active)
        self.assertEqual(tournament['challenger_features'], self.candidates[:4])
        # Step 5 is state/ranking only: the service has no Executor/service-call surface.
        self.assertFalse(hasattr(service, 'submit'))
        self.assertFalse(hasattr(service, 'execute'))


if __name__ == '__main__':
    unittest.main()
