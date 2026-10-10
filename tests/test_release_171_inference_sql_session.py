"""Real target dispatch, fresh SQL, independent commits and nested Shadow limits."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import agent
import engine as engine_module
from engine import Engine
from storage import Store
from process_agent_pipeline import EXPECTED_INSTALL_ORDER, install_process_agent_wrapper, assert_process_agent_pipeline


def runtime(handler):
    result = Engine.__new__(Engine)
    result.process_agent = handler
    result._inference_tls = threading.local()
    result.stop_event = threading.Event()
    result.wake_event = threading.Event()
    result.lock = threading.RLock()
    result.state_revision = 1
    result.state_map = {}
    result.entity_revisions = {}
    result.context = SimpleNamespace(home=SimpleNamespace(revision=0))
    return result


class InferenceSQLSession171Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='inference-session-171-')
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'test.db')
        self.config = self.store.create_agent(agent(mode='shadow'))
        with self.store.conn() as c:
            c.execute("UPDATE agents SET mode='shadow' WHERE id=?", (self.config['id'],))
        self.opens = []
        self.real_connect = sqlite3.connect

    def connect(self, *args, **kwargs):
        c = self.real_connect(*args, **kwargs)
        if Path(args[0]) == Path(self.store.path):
            self.opens.append((threading.get_ident(), c))
        return c

    def dispatch(self, handler, agents=None, expected_error=False):
        r = runtime(handler)
        with patch.object(engine_module, 'STORE', self.store), patch('storage.sqlite3.connect', side_effect=self.connect):
            r.process_target(agents or [self.config], {'sensor.radar'})
        self.assertFalse(hasattr(r._inference_tls, 'state_revision'))
        self.assertFalse(hasattr(self.store._connection_session, 'connection'))
        with self.store.conn() as c:
            errors = [row[0] for row in c.execute("SELECT message FROM events WHERE kind='agent_error'")]
        self.assertEqual(bool(errors), expected_error, errors)
        return r

    def assert_closed(self, c):
        with self.assertRaises(sqlite3.ProgrammingError):
            c.execute('SELECT 1')

    def test_complete_named_pipeline_keeps_order_and_uses_one_connection(self):
        calls = []
        def read(name):
            with self.store.conn() as c:
                calls.append((name, c.execute('SELECT mode FROM agents WHERE id=?', (self.config['id'],)).fetchone()[0]))
        r = runtime(lambda *_: read('base'))
        for name in EXPECTED_INSTALL_ORDER:
            def factory(next_handler, name=name):
                def handler(a, states, changed):
                    read(name + ':before')
                    result = next_handler(a, states, changed)
                    if name == 'context_tournament_shadow':
                        with self.store.background_sqlite(), self.store.connection_session():
                            read(name + ':after')
                    else:
                        read(name + ':after')
                    return result
                return handler
            install_process_agent_wrapper(r, name, factory)
        assert_process_agent_pipeline(r)
        with patch.object(engine_module, 'STORE', self.store), patch('storage.sqlite3.connect', side_effect=self.connect):
            r.process_target([self.config], {'sensor.radar'})
        self.assertEqual([name for name, mode in calls],
                         [name + ':before' for name in reversed(EXPECTED_INSTALL_ORDER)] + ['base'] +
                         [name + ':after' for name in EXPECTED_INSTALL_ORDER])
        self.assertTrue(all(mode == 'shadow' for name, mode in calls))
        self.assertEqual(len(self.opens), 1)
        self.assert_closed(self.opens[0][1])

    def test_external_disabled_config_is_visible_between_queries(self):
        seen = []
        def handler(*_):
            for i in range(2):
                with self.store.conn() as c:
                    seen.append(c.execute('SELECT enabled FROM agents WHERE id=?', (self.config['id'],)).fetchone()[0])
                    self.assertFalse(c.in_transaction)
                if i == 0:
                    with closing(self.real_connect(self.store.path)) as external, external:
                        external.execute('UPDATE agents SET enabled=0 WHERE id=?', (self.config['id'],))
                    with closing(self.real_connect(self.store.path, isolation_level=None)) as checkpoint:
                        busy, log, copied = checkpoint.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                        self.assertEqual(busy, 0)
                        self.assertEqual(log, copied)
        self.dispatch(handler)
        self.assertEqual(seen, [1, 0])
        self.assertEqual(len(self.opens), 1)

    def test_committed_write_visible_before_handoff_and_survives_later_failure(self):
        seen = []
        def handler(*_):
            with self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('committed','yes')")
            with closing(self.real_connect(self.store.path)) as external:
                seen.append(external.execute("SELECT value FROM app_meta WHERE key='committed'").fetchone()[0])
            with self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('rolled-back','no')")
                raise ValueError('later failed')
        self.dispatch(handler, expected_error=True)
        self.assertEqual(seen, ['yes'])
        with self.store.conn() as c:
            self.assertEqual(dict(c.execute("SELECT key,value FROM app_meta WHERE key IN ('committed','rolled-back')")), {'committed': 'yes'})
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE kind='agent_error'").fetchone()[0], 1)
        for _, c in self.opens:
            self.assert_closed(c)

    def test_shadow_scope_changes_timeout_once_and_restores_after_exception(self):
        def handler(*_):
            anchor = self.store._connection_session.connection
            sql = []
            anchor.set_trace_callback(sql.append)
            with self.assertRaisesRegex(ValueError, 'shadow failed'):
                with self.store.background_sqlite(), self.store.connection_session() as nested:
                    self.assertIs(nested, anchor)
                    with self.store.connection_session():
                        for i in range(20):
                            with self.store.conn() as c:
                                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                                c.execute('SELECT 1').fetchone()
                    raise ValueError('shadow failed')
            self.assertEqual([s for s in sql if s.startswith('PRAGMA busy_timeout=')],
                             ['PRAGMA busy_timeout=250', 'PRAGMA busy_timeout=30000'])
            self.assertEqual(anchor.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            self.assertEqual(self.store._connection_session.connection_busy_timeout_ms, 30000)
        self.dispatch(handler)
        self.assertEqual(len(self.opens), 1)

    def test_shadow_writer_still_times_out_at_250ms_and_can_retry(self):
        def handler(*_):
            blocker = self.real_connect(self.store.path)
            try:
                blocker.execute('BEGIN IMMEDIATE')
                started = time.monotonic()
                with self.assertRaises(sqlite3.OperationalError), self.store.background_sqlite(), self.store.connection_session(), self.store.conn() as c:
                    c.execute("INSERT INTO app_meta VALUES('retry','yes')")
                self.assertLess(time.monotonic() - started, 2.)
                self.assertFalse(self.store._connection_session.connection.in_transaction)
            finally:
                blocker.rollback()
                blocker.close()
            with self.store.background_sqlite(), self.store.connection_session(), self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('retry','yes')")
            with closing(self.real_connect(self.store.path)) as external:
                self.assertEqual(external.execute("SELECT value FROM app_meta WHERE key='retry'").fetchone()[0], 'yes')
        self.dispatch(handler)
        self.assertEqual(len(self.opens), 1)

    def test_nested_active_writer_keeps_owner_and_hides_uncommitted_data(self):
        def handler(*_):
            with self.assertRaises(ValueError), self.store.conn() as owner:
                owner.execute("INSERT INTO app_meta VALUES('private','no')")
                with self.store.background_sqlite(), self.store.connection_session(), self.store.conn() as reader:
                    self.assertIsNot(reader, owner)
                    self.assertEqual(reader.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                    self.assertIsNone(reader.execute("SELECT value FROM app_meta WHERE key='private'").fetchone())
                    self.assertEqual(owner.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                self.assertTrue(owner.in_transaction)
                raise ValueError('owner rollback')
        self.dispatch(handler)
        self.assertEqual(len(self.opens), 2)

    def test_concurrent_targets_use_separate_connections_and_clean_up(self):
        barrier = threading.Barrier(2)
        observed = []
        r = runtime(lambda *_: None)
        def handler(*_):
            with self.store.conn() as c:
                observed.append((threading.get_ident(), c))
            barrier.wait(3)
            with self.store.background_sqlite(), self.store.connection_session(), self.store.conn() as c:
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                self.assertIs(c, observed[-1][1] if observed[-1][0] == threading.get_ident() else observed[0][1])
        r.process_agent = handler
        with patch.object(engine_module, 'STORE', self.store), patch('storage.sqlite3.connect', side_effect=self.connect):
            threads = [threading.Thread(target=r.process_target, args=([self.config], {'sensor.radar'})) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
                self.assertFalse(t.is_alive())
        self.assertEqual(len(observed), 2)
        self.assertIsNot(observed[0][1], observed[1][1])
        self.assertEqual(len(self.opens), 2)
        with self.store.conn() as c:
            self.assertEqual(list(c.execute("SELECT message FROM events WHERE kind='agent_error'")), [])
        for _, c in self.opens:
            self.assert_closed(c)

    def test_one_failed_agent_does_not_leak_connection_to_next_agent(self):
        visited = []
        def handler(a, *_):
            with self.store.conn() as c:
                visited.append(c)
                c.execute('SELECT 1').fetchone()
            if a['id'] == 'first':
                raise ValueError('first failed')
        self.dispatch(handler, [agent(id='first'), agent(id='second')], expected_error=True)
        self.assertEqual(len(visited), 2)
        self.assertIsNot(visited[0], visited[1])
        for c in visited:
            self.assert_closed(c)

    def test_stopped_target_does_not_open_connection(self):
        r = runtime(lambda *_: self.fail('stopped pipeline ran'))
        r.stop_event.set()
        with patch.object(engine_module, 'STORE', self.store), patch('storage.sqlite3.connect', side_effect=self.connect):
            r.process_target([self.config])
        self.assertEqual(self.opens, [])

    def test_nested_background_session_restores_training_worker_timeout(self):
        self.store.training_worker_process = True
        with self.store.connection_session() as c:
            with self.store.background_sqlite(), self.store.connection_session():
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
            self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 60000)

    def test_session_inside_borrowed_background_query_keeps_its_timeout(self):
        with self.store.connection_session() as anchor:
            with self.store.background_sqlite(), self.store.conn() as c:
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                with self.store.connection_session(), self.store.conn() as nested:
                    self.assertIs(c, nested)
                    self.assertEqual(nested.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                self.assertEqual(self.store._connection_session.connection_busy_timeout_ms, 250)
            self.assertEqual(anchor.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            self.assertEqual(self.store._connection_session.connection_busy_timeout_ms, 30000)

    def test_paired_real_dispatch_and_candidate_explore_reads_match(self):
        with patch.dict(os.environ):
            from tools.benchmark_inference_sql_session import run_mode
        previous = run_mode('previous', passes=3, shadow_queries=8)
        current = run_mode('current', passes=3, shadow_queries=8)
        self.assertEqual(previous['connections_per_decision'], 5)
        self.assertEqual(current['connections_per_decision'], 1)
        self.assertEqual(previous['result_sha256'], current['result_sha256'])


if __name__ == '__main__':
    unittest.main()
