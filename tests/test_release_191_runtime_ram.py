"""Bounded RAM coalescing, commit barriers and observation-scoped metadata."""
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from storage import Store
from context_row_buffer import ContextRowBuffer
from context_tournament import ContextTournament
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
import test_context_tournament_observed_pool_runtime as pool_fixture


class RuntimeRam191Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'ram.db')
        with self.store.conn() as db:
            db.execute('CREATE TABLE ram_rows(aid TEXT,eid TEXT,v INTEGER,PRIMARY KEY(aid,eid))')
        self.statement = 'INSERT INTO ram_rows VALUES(?,?,?) ON CONFLICT(aid,eid) DO UPDATE SET v=excluded.v'

    def buffer(self, **kw):
        return ContextRowBuffer(self.store, 'ram_rows', self.statement, **kw)

    def disk(self):
        with self.store.conn() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM ram_rows ORDER BY eid')]

    def test_thirty_seconds_coalesce_all_votes_without_resetting_deadline(self):
        buf = self.buffer()
        with patch('context_row_buffer.time.monotonic', return_value=100.):
            buf.submit([('a', 'e', 1)])
        with patch('context_row_buffer.time.monotonic', return_value=129.):
            for i in range(2, 301):
                buf.submit([('a', 'e', i)])
        self.assertEqual(buf.flush_if_due(129.), 0)
        self.assertEqual(self.disk(), [])
        self.assertEqual(buf.flush_if_due(130.), 1)
        self.assertEqual(self.disk(), [('a', 'e', 300)])
        self.assertEqual(buf.snapshot()['bytes'], 0)

    def test_capacity_flushes_to_existing_database_without_exceeding_ram_budget(self):
        buf = self.buffer(max_rows=2, max_bytes=4096)
        for i in range(20):
            buf.submit([('a', str(i), i)])
            status = buf.snapshot()
            self.assertLessEqual(status['bytes'], status['max_bytes'])
            self.assertLessEqual(status['pending'] + status['inflight'], status['max_rows'])
        buf.flush()
        self.assertEqual(len(self.disk()), 20)
        self.assertGreater(buf.snapshot()['overflow_flushes'], 0)

    def test_oversized_row_rejected_before_partial_admission(self):
        buf = self.buffer(max_bytes=1024)
        buf.submit([('a', 'old', 1)])
        before = buf.snapshot()
        with self.assertRaisesRegex(ValueError, 'RAM buffer budget'):
            buf.submit([('a', 'valid', 2), ('a', 'x' * 2000, 3)])
        self.assertEqual(buf.snapshot(), before)

    def test_failed_commit_retains_latest_snapshot_and_exact_reserved_bytes(self):
        buf = self.buffer()
        buf.submit([('a', 'one', 1), ('a', 'two', 1)])
        @contextmanager
        def fail():
            buf.submit([('a', 'one', 2)])
            raise RuntimeError('disk busy')
            yield
        with patch.object(self.store, 'conn', fail):
            with self.assertRaisesRegex(RuntimeError, 'disk busy'):
                buf.flush()
        self.assertEqual(buf.snapshot()['bytes'], sum(buf._row_bytes(row) for row in buf.pending.values()))
        self.assertEqual(buf.snapshot()['inflight'], 0)
        buf.flush()
        self.assertEqual(self.disk(), [('a', 'one', 2), ('a', 'two', 1)])
        self.assertEqual(buf.snapshot()['bytes'], 0)

    def test_periodic_flush_defers_counters_but_flushes_models_and_shutdown_barrier(self):
        buf = self.buffer()
        with patch('context_row_buffer.time.monotonic', return_value=100.):
            buf.submit([('a', 'e', 1)])
            service = object.__new__(ContextTournament)
            service.lock = threading.RLock()
            service._context_row_buffers = {'ram': buf}
            service._flush_shadow_models = Mock(return_value=1)
            service._flush_background_context(force=False)
            self.assertEqual(self.disk(), [])
            service._flush_shadow_models.assert_called_once()
            service._flush_background_context()
            self.assertEqual(self.disk(), [('a', 'e', 1)])

    def pool_service(self, observer):
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion)
        self.addCleanup(setattr, promotion, '_migrate_schema', promotion._migrate_schema)
        agent = self.store.create_agent(support.agent())
        engine = SimpleNamespace(models={}, state_map={}, entity_registry={}, context=None, temporal_history=None)
        service = pool_fixture.ObservedPoolRuntimeTests.FakeService(self.store, engine, {'schema_revision': 1})
        service._eligible_entities = lambda *a: set()
        service.observe_shadow = lambda *a, **kw: observer(service, agent)
        candidates.install_policy_candidates(service)
        return service, agent

    def test_observation_count_uses_one_query_but_keeps_schema_fresh_and_next_event_fresh(self):
        def observe(service, agent):
            rows = [service.state(agent['id']) for _ in range(5)]
            service._state['schema_revision'] = 2
            rows.append(service.state(agent['id']))
            return rows
        service, agent = self.pool_service(observe)
        calls = []
        original = self.store.conn
        @contextmanager
        def trace():
            with original() as db:
                db.set_trace_callback(calls.append)
                yield db
        with patch.object(self.store, 'conn', trace):
            result = service.observe_shadow(agent)
        self.assertEqual([row['observed_pool_count'] for row in result], [0] * 6)
        self.assertEqual(result[-1]['schema_revision'], 2)
        self.assertEqual(sum('SELECT COUNT(*)' in sql for sql in calls), 1)
        with self.store.conn() as db:
            db.execute('INSERT INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) VALUES(?,?,?)',
                       (agent['id'], 'sensor.new', 1.))
        self.assertEqual(service.observe_shadow(agent)[0]['observed_pool_count'], 1)
        self.assertEqual(service.state(agent['id'])['observed_pool_count'], 1)

    def test_observation_exception_does_not_leave_a_stale_ram_count(self):
        def observe(service, agent):
            service.state(agent['id'])
            raise RuntimeError('observer failed')
        service, agent = self.pool_service(observe)
        with self.assertRaisesRegex(RuntimeError, 'observer failed'):
            service.observe_shadow(agent)
        with self.store.conn() as db:
            db.execute('INSERT INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) VALUES(?,?,?)',
                       (agent['id'], 'sensor.new', 1.))
        self.assertEqual(service.state(agent['id'])['observed_pool_count'], 1)


if __name__ == '__main__':
    unittest.main()
