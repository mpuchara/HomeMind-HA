import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
import context_tournament_promotion as promotion
from context_tournament_policy_candidate import install_policy_candidates
from policy import MultiHorizonPolicy
from storage import Store


class ObservedPoolRuntimeTests(unittest.TestCase):
    class FakeService:
        def __init__(self, store, engine, tournament):
            self.store = store
            self.engine = engine
            self._state = dict(tournament)
            self.lock = threading.RLock()
            self._policy_candidate_installed = False
            self._model = {'version': 1, 'action_count': 2, 'counts': {}, 'samples': 0,
                           'active_correct': 0, 'shadow_correct': 0, 'last_scored_ts': None}

        def state(self, agent_id):
            return dict(self._state)

        def sync_agent(self, agent, **kwargs):
            scores = kwargs.get('feature_scores')
            if scores is not None:
                self._state['feature_scores'] = dict(scores)
            return dict(self._state)

        def observe_shadow(self, agent, state_map=None, changed_entities=None):
            return {'predictions': [], 'scored': 0}

        def shadow_status(self, agent):
            return {'challengers': []}

        def _load_shadow_model(self, agent_id, challenger, action_count):
            return self._model

        def _save_shadow_model(self, agent_id, challenger, model):
            self._model = model

        def _score_shadow_sample(self, *args, **kwargs):
            return None

        def _shadow_predict_index(self, model, active_idx, bucket):
            return int(active_idx)

        def _blank_shadow_model(self, action_count):
            return {'version': 1, 'action_count': action_count, 'counts': {}, 'samples': 0,
                    'active_correct': 0, 'shadow_correct': 0, 'last_scored_ts': None}

        def _eligible_entities(self, agent, active):
            return {'binary_sensor.precursor'}

    def test_observed_pool_insert_executes_and_first_sample_is_not_action_leak(self):
        temp = tempfile.TemporaryDirectory(prefix='stage09-pool-')
        old_chooser = promotion._choose_schema_after_promotion
        old_migrate = promotion._migrate_schema
        try:
            store = Store(Path(temp.name) / 'pool.db')
            a = store.create_agent(agent(name='pool runtime'))
            states = {
                'light.kitchen': state('light.kitchen', 'off'),
                'binary_sensor.primary': state('binary_sensor.primary', 'off', device_class='occupancy'),
                'binary_sensor.precursor': state('binary_sensor.precursor', 'on', device_class='occupancy'),
            }
            policy = MultiHorizonPolicy(a, states, {}, set())
            engine = SimpleNamespace(
                models={a['id']: policy}, state_map=states, entity_registry={}, context_relevance={},
                context=None, temporal_history=None, state_revision=1, runtime={a['id']: {}},
                lock=threading.RLock(),
            )
            tournament = {
                'agent_id': a['id'], 'active_features': list(policy.schema.entities),
                'challenger_features': ['binary_sensor.precursor'],
                'feature_scores': {'binary_sensor.precursor': 0.5},
                'schema_revision': 1, 'previous_schema': [],
            }
            service = self.FakeService(store, engine, tournament)
            install_policy_candidates(service)
            service.sync_agent(a, policy=policy)
            service.observe_shadow(a, states, {'binary_sensor.precursor'})

            with store.conn() as c:
                row = c.execute(
                    'SELECT opportunities,available_count,change_count,own_action_leak_hits '
                    'FROM context_tournament_observed_pool WHERE agent_id=? AND entity_id=?',
                    (a['id'], 'binary_sensor.precursor'),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row['opportunities']), 1)
            self.assertEqual(int(row['available_count']), 1)
            self.assertEqual(int(row['change_count']), 0)
            self.assertEqual(int(row['own_action_leak_hits']), 0)
        finally:
            promotion._choose_schema_after_promotion = old_chooser
            promotion._migrate_schema = old_migrate
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
