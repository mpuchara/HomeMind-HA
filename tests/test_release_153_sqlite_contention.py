"""Exercise real WAL contention, metadata publication and bounded trace retention."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import support  # Configure the temporary data directory before importing Store.
from storage import Store
from context_row_buffer import ContextRowBuffer
from sqlite_background import background_sqlite
from telemetry import RUNTIME_DEBUG, RuntimeDebugTrace
from context_tournament import ContextTournament
from types import SimpleNamespace


class SQLiteContention153Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'db.sqlite')
        with self.store.conn() as c:
            c.execute('CREATE TABLE test_rows(aid TEXT,eid TEXT,n INTEGER,PRIMARY KEY(aid,eid))')

    def test_background_timeout_restores_nested_and_default_policy(self):
        def timeout():
            with self.store.conn() as c:
                return c.execute('PRAGMA busy_timeout').fetchone()[0]
        self.assertEqual(timeout(), 30000)
        with background_sqlite(self.store):
            self.assertEqual(timeout(), 250)
            with self.store.background_sqlite():
                self.assertEqual(timeout(), 250)
            self.assertEqual(timeout(), 250)
        self.assertEqual(timeout(), 30000)
        self.store.training_worker_process = True
        self.assertEqual(timeout(), 60000)

    def test_timeout_is_thread_local(self):
        result = []
        with background_sqlite(self.store):
            def other():
                with self.store.conn() as c:
                    result.append(c.execute('PRAGMA busy_timeout').fetchone()[0])
            t = threading.Thread(target=other)
            t.start(); t.join(3)
        self.assertEqual(result, [30000])

    def test_borrowed_connection_timeout_restored_after_failure(self):
        with self.store.connection_session() as anchor:
            with self.assertRaises(RuntimeError), self.store.background_sqlite(), self.store.conn() as c:
                self.assertIs(c, anchor)
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
                c.execute("INSERT INTO test_rows VALUES('a','sensor',1)")
                raise RuntimeError('rollback')
            self.assertEqual(anchor.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            self.assertEqual(anchor.execute('SELECT count(*) FROM test_rows').fetchone()[0], 0)

    def test_real_external_writer_timeout_retains_batch_and_retries_latest(self):
        buffer = ContextRowBuffer(self.store, 'test_rows',
            'INSERT INTO test_rows VALUES(?,?,?) ON CONFLICT(aid,eid) DO UPDATE SET n=excluded.n')
        buffer.submit([('a', 'sensor', 1)])
        blocker = sqlite3.connect(self.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError), self.store.background_sqlite():
                buffer.flush()
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(buffer.snapshot()['pending'], 1)
            buffer.submit([('a', 'sensor', 2)])
        finally:
            blocker.rollback(); blocker.close()
        self.assertEqual(buffer.flush(), 1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT n FROM test_rows').fetchone()[0], 2)

    def test_metadata_read_does_not_wait_for_durable_lock(self):
        self.store.meta_set('key', 'committed')
        held, release, read = threading.Event(), threading.Event(), threading.Event()
        def writer():
            with self.store.lock:
                held.set(); release.wait(3)
        t = threading.Thread(target=writer); t.start()
        self.assertTrue(held.wait(2))
        values = []
        def reader():
            values.append(self.store.meta_get('key')); read.set()
        r = threading.Thread(target=reader); r.start()
        try:
            self.assertTrue(read.wait(.5), 'RAM metadata blocked behind durable Store lock')
        finally:
            release.set(); t.join(3); r.join(3)
        self.assertEqual(values, ['committed'])

    def test_failed_metadata_write_never_publishes_uncommitted_value(self):
        self.store.meta_set('key', 'old')
        blocker = sqlite3.connect(self.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError), self.store.background_sqlite():
                self.store.meta_set('key', 'new')
            self.assertEqual(self.store.meta_get('key'), 'old')
        finally:
            blocker.rollback(); blocker.close()
        self.store.meta_set('key', 'new')
        self.assertEqual(Store(self.store.path).meta_get('key'), 'new')

    def test_connection_trace_preserves_sqlite_error_and_call_site(self):
        blocker = sqlite3.connect(self.store.path)
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError), self.store.background_sqlite(), self.store.conn() as c:
                c.execute("INSERT INTO test_rows VALUES('a','sensor',1)")
            row = next(r for r in RUNTIME_DEBUG.export()['critical_spans']
                       if 'test_connection_trace_preserves' in r['fields'].get('caller', ''))
            self.assertEqual(row['operation'], 'sqlite_transaction')
            self.assertEqual(row['status'], 'error')
            self.assertEqual(row['fields']['sqlite_errorname'], 'SQLITE_BUSY')
            self.assertEqual(row['fields']['busy_timeout_ms'], 250)
            self.assertIn('test_connection_trace_preserves', row['fields']['caller'])
            self.assertIn('started_at', row)
        finally:
            blocker.rollback(); blocker.close()
            RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_session_lifetime_is_not_reported_as_transaction(self):
        RUNTIME_DEBUG.set_enabled(True, clear=True)
        try:
            def own_active():
                return [r for r in RUNTIME_DEBUG.export()['active']
                        if r['thread'] == threading.current_thread().name]
            with self.store.connection_session():
                self.assertEqual(own_active(), [])
                with self.store.conn() as c:
                    self.assertEqual(len(own_active()), 1)
                    c.execute('SELECT 1')
            self.assertEqual(len([r for r in RUNTIME_DEBUG.export()['entries']
                                  if r['kind']=='end' and r['operation']=='sqlite_transaction'
                                  and r['thread']==threading.current_thread().name]), 1)
        finally:
            RUNTIME_DEBUG.set_enabled(False, clear=True)

    def test_fixture_scope_is_compatible(self):
        with background_sqlite(object()):
            pass

    def test_connection_is_closed_if_setup_pragma_fails(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError('setup failed')
        with patch('storage.sqlite3.connect', return_value=connection):
            with self.assertRaises(sqlite3.OperationalError), self.store.conn():
                self.fail('failed setup yielded a connection')
        connection.close.assert_called_once()

    def test_reporting_busy_error_does_not_wait_thirty_seconds_again(self):
        blocker = sqlite3.connect(self.store.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with patch('builtins.print'):
                self.store.event(None, 'error', 'test_busy', 'writer busy')
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(self.store.list_events()[0]['kind'], 'test_busy')
            self.assertEqual(len(self.store._event_buffer), 1)
        finally:
            blocker.rollback(); blocker.close()
        self.assertEqual(self.store.flush_events(), 1)

    def test_explicit_flush_waits_for_writer_beyond_background_timeout(self):
        buffer = ContextRowBuffer(self.store, 'test_rows', 'INSERT INTO test_rows VALUES(?,?,?)')
        buffer.submit([('a', 'sensor', 1)])
        held = threading.Event()
        def writer():
            c = sqlite3.connect(self.store.path)
            try:
                c.execute('BEGIN IMMEDIATE'); held.set()
                time.sleep(.6)
            finally:
                c.rollback(); c.close()
        t = threading.Thread(target=writer); t.start()
        self.assertTrue(held.wait(3))
        try:
            self.assertEqual(buffer.flush(), 1)
        finally:
            t.join(3)
        self.assertEqual(buffer.snapshot()['pending'], 0)

    def test_shadow_worker_bounds_periodic_flush_but_not_final_barrier(self):
        # Exercise the actual worker method without starting other Engine producers.
        service = object.__new__(ContextTournament)
        service.store = self.store
        service.engine = SimpleNamespace(stop_event=threading.Event())
        service._shadow_flush_event = threading.Event()
        service._shadow_flush_event.set()
        timeouts = []
        def flush():
            with self.store.conn() as c:
                timeouts.append(c.execute('PRAGMA busy_timeout').fetchone()[0])
            service.engine.stop_event.set()
        service._flush_background_context = flush
        service._shadow_writer()
        self.assertEqual(timeouts, [250, 30000])


class CriticalTrace153Tests(unittest.TestCase):
    def test_error_survives_ordinary_ring_overwrite(self):
        trace = RuntimeDebugTrace(); trace.set_enabled(True)
        token = trace.begin('failed_writer', entity='sensor.test')
        trace.end(token, status='error')
        for n in range(trace.MAX_ENTRIES + 1):
            trace.instant('fast', n=n)
        payload = trace.export()
        self.assertFalse(any(r['operation']=='failed_writer' for r in payload['entries']))
        self.assertEqual(payload['critical_spans'][0]['operation'], 'failed_writer')
        self.assertGreater(payload['dropped_entries'], 0)

    def test_slow_span_retained_and_bounded(self):
        trace = RuntimeDebugTrace(); trace.set_enabled(True)
        for n in range(trace.MAX_CRITICAL_SPANS + 10):
            with patch('telemetry.time.monotonic', return_value=10):
                token = trace.begin('slow', n=n)
            with patch('telemetry.time.monotonic', return_value=13):
                trace.end(token)
        rows = trace.export()['critical_spans']
        self.assertEqual(len(rows), trace.MAX_CRITICAL_SPANS)
        self.assertEqual(rows[0]['fields']['n'], 10)
        self.assertEqual(rows[-1]['duration_ms'], 3000)
        trace.clear(); self.assertEqual(trace.export()['critical_spans'], [])

    def test_stop_preserves_unfinished_long_span_and_clear_resets(self):
        trace = RuntimeDebugTrace(); trace.set_enabled(True)
        with patch('telemetry.time.monotonic', return_value=10):
            trace.begin('still_blocked')
        with patch('telemetry.time.monotonic', return_value=45):
            trace.set_enabled(False)
        payload = trace.export()
        self.assertEqual(payload['active'], [])
        self.assertEqual(payload['critical_spans'][0]['status'], 'tracing_stopped')
        self.assertEqual(payload['critical_spans'][0]['duration_ms'], 35000)
        trace.set_enabled(True, clear=True)
        self.assertEqual(trace.export()['critical_spans'], [])

    def test_fast_success_and_disabled_tracing_do_not_fill_protected_ring(self):
        trace = RuntimeDebugTrace()
        self.assertIsNone(trace.begin('disabled'))
        trace.set_enabled(True)
        token = trace.begin('fast'); trace.end(token)
        self.assertEqual(trace.export()['critical_spans'], [])
