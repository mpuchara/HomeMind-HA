import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from support import *
import context_tournament_promotion as promotion
import context_tournament_policy_candidate as pool_module
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

    def test_full_pool_is_one_durable_vote_batch_and_only_new_labels_are_rescored(self):
        old_chooser = promotion._choose_schema_after_promotion
        old_migrate = promotion._migrate_schema
        try:
            with tempfile.TemporaryDirectory(prefix='pool-batch-') as directory:
                store = Store(Path(directory) / 'pool.db')
                a = store.create_agent(agent(name='full observed pool'))
                entities = {f'sensor.context_{i:03d}' for i in range(96)}
                states = {eid: state(eid, i) for i, eid in enumerate(sorted(entities))}
                states[a['target_entity']] = state(a['target_entity'], 'off')
                policy = MultiHorizonPolicy(a, states, {}, set())
                engine = SimpleNamespace(
                    models={a['id']: policy}, state_map=states, entity_registry={},
                    context_relevance={}, context=None, temporal_history=None,
                    state_revision=1, runtime={a['id']: {}}, lock=threading.RLock(),
                )
                tournament = dict(agent_id=a['id'], active_features=[],
                                  challenger_features=[], feature_scores={},
                                  schema_revision=1, previous_schema=[])
                service = self.FakeService(store, engine, tournament)
                service._eligible_entities = lambda *args: entities
                install_policy_candidates(service)
                service.sync_agent(a, policy=policy)
                statements = []
                original_conn = store.conn

                @contextmanager
                def traced_connection():
                    with original_conn() as connection:
                        connection.set_trace_callback(statements.append)
                        yield connection

                with patch.object(store, 'conn', traced_connection), patch.object(
                    pool_module, 'semantic_predictive_score',
                    wraps=pool_module.semantic_predictive_score,
                ) as score:
                    service.observe_shadow(a, states, entities)
                    self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
                    self.assertEqual(score.call_count, 96)
                    statements.clear()
                    score.reset_mock()
                    # Availability observations remain votes, but are not labels.
                    service.observe_shadow(a, states, entities)
                    self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
                    self.assertEqual(score.call_count, 0)
                    statements.clear()
                    states[a['target_entity']] = state(a['target_entity'], 'on')
                    service.observe_shadow(a, states, {a['target_entity']})
                    self.assertEqual(score.call_count, 96)

                # Inspect durable data from a fresh connection: all sensors retained
                # three availability votes and exactly one independent target label.
                with original_conn() as connection:
                    rows = connection.execute(
                        'SELECT opportunities,available_count,samples_json,screening_json '
                        'FROM context_tournament_observed_pool WHERE agent_id=?',
                        (a['id'],),
                    ).fetchall()
                import json
                self.assertEqual(len(rows), 96)
                for row in rows:
                    self.assertEqual(row['opportunities'], 3)
                    self.assertEqual(row['available_count'], 3)
                    samples = json.loads(row['samples_json'])
                    self.assertEqual(len(samples), 1)
                    self.assertEqual(samples[0]['label'], 1.0)
                    self.assertEqual(json.loads(row['screening_json']),
                                     pool_module.semantic_predictive_score(samples))
        finally:
            promotion._choose_schema_after_promotion = old_chooser
            promotion._migrate_schema = old_migrate


if __name__ == '__main__':
    unittest.main()
