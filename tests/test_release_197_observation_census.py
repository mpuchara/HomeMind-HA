"""Census shared through the production observer, with fresh state and RAM growth."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
import test_context_tournament_observed_pool_runtime as pool_fixture
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
from context_tournament import install as install_tournament
from context_row_buffer import ContextRowBuffer
from settings import OPTIONS
from storage import Store


class ObservationCensus197Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'pool.db')
        self.agent = self.store.create_agent(support.agent())
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion)
        self.addCleanup(setattr, promotion, '_migrate_schema', promotion._migrate_schema)

    def fixture(self, buffered=False):
        engine = SimpleNamespace(models={}, state_map={}, entity_registry={}, context=None,
                                 temporal_history=None, lock=threading.RLock())
        service = pool_fixture.ObservedPoolRuntimeTests.FakeService(self.store, engine,
            {'schema_revision': 1, 'active_features': [], 'challenger_features': []})
        service._eligible_entities = lambda a, active: set(engine.state_map) - {self.agent['target_entity']}
        service.observe_shadow = lambda *a, **kw: service.state(self.agent['id'])
        if buffered:
            service.register_context_rows = lambda name, sql: ContextRowBuffer(self.store, name, sql)
            service.background_persistence_available = lambda: True
        candidates.install_policy_candidates(service)
        return service, engine

    def insert(self, n, prefix='sensor'):
        with self.store.conn() as c:
            c.executemany('INSERT INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) VALUES(?,?,1)',
                          [(self.agent['id'], f'{prefix}.{i}') for i in range(n)])

    @staticmethod
    def census_queries(trace):
        return [sql for sql in trace if 'FROM context_tournament_observed_pool' in sql
                and ('SELECT COUNT(*)' in sql or sql.startswith('SELECT entity_id'))]

    def test_real_process_wrapper_shares_census_across_outer_and_inner_decorators(self):
        order = []
        authoritative = {'desired': 0, 'confidence': .9}
        engine = SimpleNamespace(models={}, state_map={}, entity_registry={}, context=None, temporal_history=None,
            policy=lambda *_: None, runtime_for=lambda *_: {},
            process_agent=lambda *_: order.append('authoritative') or authoritative)
        service = install_tournament(self.store, engine)
        service._eligible_entities = lambda *_: set()
        service.observe_shadow = lambda *a: service.state(self.agent['id'])
        candidates.install_policy_candidates(service)
        self.insert(12)
        inner = service.observe_shadow
        def outer(*a):
            order.append('shadow')
            for _ in range(2): self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 12)
            inner(*a)
            for _ in range(2): self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 12)
        service.observe_shadow = outer
        with self.store.connection_session() as c:
            trace = []; c.set_trace_callback(trace.append)
            result = engine.process_agent(self.agent, {})
            c.set_trace_callback(None)
        self.assertIs(result, authoritative)
        self.assertEqual(order, ['authoritative', 'shadow'])
        self.assertEqual(len(self.census_queries(trace)), 1)
        self.assertEqual(service.observation_pool_cache_snapshot()['persistent_entries'], 0)

    def test_nested_scopes_reuse_one_query_but_next_frame_is_fresh(self):
        service, _ = self.fixture()
        self.insert(1)
        with self.store.connection_session() as c:
            trace = []; c.set_trace_callback(trace.append)
            with service.observation_pool_scope():
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 1)
                with service.observation_pool_scope():
                    service.observe_shadow(self.agent)
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 1)
            self.assertEqual(len(self.census_queries(trace)), 1)
            self.insert(1, 'new')
            with service.observation_pool_scope():
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 2)
            self.assertEqual(len(self.census_queries(trace)), 2)
            c.set_trace_callback(None)

    def test_schema_and_promotion_fields_are_fresh_inside_scope(self):
        service, _ = self.fixture()
        with service.observation_pool_scope():
            first = service.state(self.agent['id'])
            service._state.update(schema_revision=17, active_features=['binary_sensor.new'], previous_schema=['binary_sensor.old'])
            after = service.state(self.agent['id'])
            self.assertEqual(first['schema_revision'], 1)
            self.assertEqual(after['schema_revision'], 17)
            self.assertEqual(after['active_features'], ['binary_sensor.new'])
            self.assertEqual(after['previous_schema'], ['binary_sensor.old'])

    def test_exception_releases_frame_and_does_not_poison_next_read(self):
        service, _ = self.fixture()
        with self.assertRaisesRegex(ValueError, 'failed'), service.observation_pool_scope():
            self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 0)
            raise ValueError('failed')
        self.insert(2)
        self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 2)
        with service.observation_pool_scope():
            self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 2)

    def grow_pool(self, buffered):
        service, engine = self.fixture(buffered=buffered)
        engine.state_map = {self.agent['target_entity']: support.state(self.agent['target_entity'], 'off'),
                            'binary_sensor.new': support.state('binary_sensor.new', 'on', device_class='occupancy')}
        with self.store.connection_session() as c:
            trace = []; c.set_trace_callback(trace.append)
            with service.observation_pool_scope():
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 0)
                service.observe_shadow(self.agent, engine.state_map)
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 1)
            c.set_trace_callback(None)
        self.assertEqual(len(self.census_queries(trace)), 1 if buffered else 2)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM context_tournament_observed_pool').fetchone()[0], 0 if buffered else 1)

    def test_new_pending_sensor_is_counted_without_second_sql_query(self):
        self.grow_pool(True)

    def test_synchronous_insert_invalidates_earlier_count_in_same_frame(self):
        self.grow_pool(False)

    def test_limit_change_has_separate_census_and_preserves_bound(self):
        service, _ = self.fixture()
        self.insert(80)
        with service.observation_pool_scope(), patch.dict(OPTIONS, context_observed_pool_limit=16):
            self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 32)
            with patch.dict(OPTIONS, context_observed_pool_limit=32):
                self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 64)
            self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 32)

    def test_threads_use_independent_observation_scopes(self):
        service, _ = self.fixture()
        self.insert(3)
        barrier = threading.Barrier(2)
        traces, errors = [], []
        def observe():
            try:
                with self.store.connection_session() as c, service.observation_pool_scope():
                    trace = []; c.set_trace_callback(trace.append)
                    self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 3)
                    barrier.wait(3)
                    self.assertEqual(service.state(self.agent['id'])['observed_pool_count'], 3)
                    traces.append(trace)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=observe) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual([len(self.census_queries(trace)) for trace in traces], [1, 1])


if __name__ == '__main__':
    unittest.main()
