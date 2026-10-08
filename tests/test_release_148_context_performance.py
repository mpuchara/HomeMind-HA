import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import agent, state
from storage import Store
from policy import MultiHorizonPolicy
import context_tournament_policy_candidate as pool_module
import context_tournament_promotion as promotion
from context_tournament_policy_candidate import install_policy_candidates
from context_tournament_quality import install_sensor_quality
import test_context_tournament_observed_pool_runtime as pool_tests


class ContextPerformance148Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='context-148-')
        self.store = Store(Path(self.directory.name) / 'test.db')
        self.old_choose = promotion._choose_schema_after_promotion
        self.old_migrate = promotion._migrate_schema

    def tearDown(self):
        promotion._choose_schema_after_promotion = self.old_choose
        promotion._migrate_schema = self.old_migrate
        self.directory.cleanup()

    def test_session_reuses_connection_but_commits_before_external_handoff(self):
        a = self.store.create_agent(agent(mode='control'))
        errors = []
        observed = []

        def witness():
            try:
                with self.store.conn() as connection:
                    observed.append(connection.execute(
                        'SELECT mode FROM agents WHERE id=?', (a['id'],)
                    ).fetchone()[0])
            except Exception as exc:
                errors.append(exc)

        original_connect = sqlite3.connect
        with patch('storage.sqlite3.connect', wraps=original_connect) as connect:
            with self.store.connection_session() as connection:
                with self.store.connection_session() as nested:
                    self.assertIs(nested, connection)
                with self.store.conn() as first:
                    self.assertIs(first, connection)
                self.store.update_agent(a['id'], {'mode': 'shadow'})
                # The separate thread represents an Executor/HA handoff observer.
                # It sees the durable mode while the connection session is open.
                thread = threading.Thread(target=witness)
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(connect.call_count, 2)  # owner + independent witness
        self.assertEqual(errors, [])
        self.assertEqual(observed, ['shadow'])

    def test_session_rollback_does_not_undo_prior_commit_and_cleans_thread_binding(self):
        with self.assertRaisesRegex(ValueError, 'abort'):
            with self.store.connection_session():
                with self.store.conn() as connection:
                    connection.execute("INSERT INTO app_meta(key,value) VALUES('committed','yes')")
                with self.store.conn() as connection:
                    connection.execute("INSERT INTO app_meta(key,value) VALUES('rolled-back','no')")
                    raise ValueError('abort')
        self.assertFalse(hasattr(self.store._connection_session, 'connection'))
        with self.store.conn() as connection:
            values = dict(connection.execute(
                "SELECT key,value FROM app_meta WHERE key IN ('committed','rolled-back')"
            ).fetchall())
        self.assertEqual(values, {'committed': 'yes'})

    def test_nested_reader_cannot_see_or_commit_outer_uncommitted_write(self):
        with self.store.connection_session():
            with self.assertRaisesRegex(ValueError, 'outer abort'):
                with self.store.conn() as outer:
                    outer.execute("INSERT INTO app_meta(key,value) VALUES('private-write','pending')")
                    with self.store.conn() as reader:
                        self.assertIsNot(reader, outer)
                        self.assertIsNone(reader.execute(
                            "SELECT value FROM app_meta WHERE key='private-write'"
                        ).fetchone())
                    self.assertTrue(outer.in_transaction)
                    raise ValueError('outer abort')
            with self.store.conn() as reader:
                self.assertIsNone(reader.execute(
                    "SELECT value FROM app_meta WHERE key='private-write'"
                ).fetchone())

    def pool(self):
        a = self.store.create_agent(agent(name='counter persistence'))
        entities = {f'sensor.context_{i:03d}' for i in range(96)}
        states = {eid: state(eid, i) for i, eid in enumerate(sorted(entities))}
        states[a['target_entity']] = state(a['target_entity'], 'off')
        policy = MultiHorizonPolicy(a, states, {}, set())
        engine = SimpleNamespace(
            models={a['id']: policy}, state_map=states, entity_registry={}, context_relevance={},
            context=None, temporal_history=None, state_revision=1, runtime={a['id']: {}},
            lock=threading.RLock(),
        )
        service = pool_tests.ObservedPoolRuntimeTests.FakeService(self.store, engine, dict(
            agent_id=a['id'], active_features=[], challenger_features=[],
            feature_scores={}, schema_revision=1, previous_schema=[],
        ))
        service._eligible_entities = lambda *args: entities
        install_policy_candidates(service)
        service.sync_agent(a, policy=policy)
        return a, entities, states, service

    def test_counter_ticks_preserve_examples_and_recover_deleted_cached_rows(self):
        a, entities, states, service = self.pool()
        service.observe_shadow(a, states, entities)
        states[a['target_entity']] = state(a['target_entity'], 'on')
        service.observe_shadow(a, states, {a['target_entity']})
        with self.store.conn() as connection:
            before = {row['entity_id']: dict(row) for row in connection.execute(
                'SELECT * FROM context_tournament_observed_pool WHERE agent_id=?', (a['id'],)
            )}
        with patch.object(pool_module.json, 'dumps', wraps=json.dumps) as dumps:
            service.observe_shadow(a, states, entities)
            # Warm counter-only ticks do not serialize any historical JSON.
            self.assertEqual(dumps.call_count, 0)
        with self.store.conn() as connection:
            after = {row['entity_id']: dict(row) for row in connection.execute(
                'SELECT * FROM context_tournament_observed_pool WHERE agent_id=?', (a['id'],)
            )}
            removed = sorted(entities)[0]
            connection.execute(
                'DELETE FROM context_tournament_observed_pool WHERE agent_id=? AND entity_id=?',
                (a['id'], removed),
            )
        for eid in entities:
            self.assertEqual(after[eid]['opportunities'], before[eid]['opportunities'] + 1)
            for field in ('history_json', 'samples_json', 'screening_json'):
                self.assertEqual(after[eid][field], before[eid][field])
        service.observe_shadow(a, states, entities)
        with self.store.conn() as connection:
            recovered = connection.execute(
                'SELECT * FROM context_tournament_observed_pool WHERE agent_id=? AND entity_id=?',
                (a['id'], removed),
            ).fetchone()
        self.assertEqual(recovered['opportunities'], 4)
        self.assertEqual(recovered['samples_json'], before[removed]['samples_json'])
        self.assertEqual(len(json.loads(recovered['samples_json'])), 1)

    def test_state_count_is_bounded_without_computing_sensor_health(self):
        a, entities, states, service = self.pool()
        service.observe_shadow(a, states, entities)
        with self.store.conn() as connection:
            connection.executemany(
                'INSERT INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) '
                'VALUES(?,?,?)', [(a['id'], f'sensor.extra_{i}', i) for i in range(200)],
            )
        with patch.object(pool_module, 'sensor_health_from_row', side_effect=AssertionError('full pool scan')):
            self.assertEqual(service.state(a['id'])['observed_pool_count'], 192)

    def test_quality_batch_preserves_all_votes_before_promotion_observer(self):
        a = self.store.create_agent(agent())
        entities = [f'binary_sensor.occupancy_{i}' for i in range(8)]
        states = {eid: state(eid, 'on', device_class='occupancy') for eid in entities}
        states[entities[-1]] = state(entities[-1], 'unavailable')
        engine = SimpleNamespace(state_map=states)
        service = pool_tests.ObservedPoolRuntimeTests.FakeService(self.store, engine, dict(
            active_features=entities[:4], challenger_features=entities[4:]
        ))
        witnesses = []

        def observe(*args):
            with self.store.conn() as connection:
                witnesses.append([dict(row) for row in connection.execute(
                    'SELECT * FROM context_tournament_sensor_quality WHERE agent_id=?', (a['id'],)
                )])
            return 'production-result'

        service.observe_shadow = observe
        install_sensor_quality(service)
        statements = []
        original_conn = self.store.conn

        @contextmanager
        def traced():
            with original_conn() as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(self.store, 'conn', traced):
            result = service.observe_shadow(a, states, set(entities))
        self.assertEqual(result, 'production-result')
        self.assertEqual(sum(s == 'COMMIT' for s in statements), 1)
        self.assertEqual(len(witnesses[0]), 8)
        for row in witnesses[0]:
            self.assertEqual(row['opportunities'], 1)
            self.assertEqual(row['event_count'], 1)
            self.assertEqual(row['unavailable_count'], int(row['entity_id'] == entities[-1]))
            self.assertEqual(row['available_count'], int(row['entity_id'] != entities[-1]))


if __name__ == '__main__':
    unittest.main()
