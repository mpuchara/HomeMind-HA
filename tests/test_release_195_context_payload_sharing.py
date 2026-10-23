"""Bounded same-object payload sharing across pending and in-flight versions."""
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from storage import Store
from context_row_buffer import ContextRowBuffer
from context_tournament import ContextTournament
from storage_budget import MIB


class ContextPayload195Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'rows.db')
        with self.store.conn() as c:
            c.execute('CREATE TABLE rows(a TEXT,e TEXT,n INTEGER,p TEXT,PRIMARY KEY(a,e))')
        self.sql = 'INSERT INTO rows VALUES(?,?,?,?) ON CONFLICT(a,e) DO UPDATE SET n=excluded.n,p=excluded.p'

    def buffer(self, **kwargs):
        return ContextRowBuffer(self.store, 'rows', self.sql, shared_columns=(3,), **kwargs)

    def paused_versions(self, buf, first, second, expected_ready):
        entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.store.conn
        @contextmanager
        def paused():
            entered.set()
            if not release.wait(3): raise TimeoutError('writer pause')
            with original() as c: yield c
        def flush():
            try: buf.flush()
            except Exception as exc: errors.append(exc)
        def submit():
            try: buf.submit([second]); submitted.set()
            except Exception as exc: errors.append(exc)
        buf.submit([first])
        with patch.object(self.store, 'conn', paused):
            writer = threading.Thread(target=flush)
            foreground = threading.Thread(target=submit)
            writer.start()
            try:
                self.assertTrue(entered.wait(3))
                foreground.start()
                ready = submitted.wait(.2)
                during = buf.snapshot()
            finally:
                release.set()
                writer.join(3)
                if foreground.ident is not None: foreground.join(3)
        self.assertFalse(writer.is_alive())
        self.assertFalse(foreground.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(ready, expected_ready)
        self.assertLessEqual(during['bytes'], during['max_bytes'])
        buf.flush()
        self.assertEqual(buf.snapshot()['bytes'], 0)
        self.assertEqual(buf.snapshot()['shared_payloads'], 0)
        with self.store.conn() as c:
            self.assertEqual(tuple(c.execute('SELECT n,p FROM rows').fetchone()), (second[2], second[3]))
        return during

    def test_ten_mib_shared_payload_admits_new_metadata_while_writer_is_paused(self):
        payload = 'x' * (10 * MIB)
        buf = self.buffer(max_bytes=16 * MIB)
        stats = self.paused_versions(buf, ('a', 'sensor.x', 1, payload), ('a', 'sensor.x', 2, payload), True)
        self.assertEqual(stats['overflow_flushes'], 0)
        self.assertEqual(stats['inflight'], 1)
        self.assertEqual(stats['pending'], 1)
        self.assertGreaterEqual(stats['duplicate_payload_charge_avoided_bytes'], 10 * MIB)

    def test_generic_buffer_keeps_old_pressure_backpressure(self):
        payload = 'x' * MIB
        buf = ContextRowBuffer(self.store, 'rows', self.sql, max_bytes=3 * MIB // 2)
        stats = self.paused_versions(buf, ('a', 'sensor.x', 1, payload), ('a', 'sensor.x', 2, payload), False)
        self.assertGreater(stats['overflow_flushes'], 0)

    def test_equal_text_in_distinct_objects_is_not_shared(self):
        old = 'x' * MIB
        new = old.encode().decode()
        self.assertIsNot(old, new)
        buf = self.buffer(max_bytes=3 * MIB // 2)
        stats = self.paused_versions(buf, ('a', 'sensor.x', 1, old), ('a', 'sensor.x', 2, new), False)
        self.assertGreater(stats['overflow_flushes'], 0)

    def test_other_sensor_rows_are_conservatively_charged_separately(self):
        payload = 'x' * MIB
        buf = self.buffer(max_bytes=3 * MIB)
        buf.submit([('a', 'sensor.one', 1, payload), ('a', 'sensor.two', 1, payload)])
        self.assertEqual(buf.snapshot()['shared_payloads'], 2)
        self.assertGreater(buf.snapshot()['shared_payload_bytes'], 2 * MIB)
        self.assertEqual(buf.flush(), 2)
        self.assertEqual(buf.snapshot()['bytes'], 0)

    def failed_commit(self, new_payload):
        old = 'x' * 4000
        buf = self.buffer(max_bytes=65536)
        buf.submit([('a', 'sensor.x', 10, old)])
        @contextmanager
        def failed():
            buf.submit([('a', 'sensor.x', 20, new_payload(old))])
            raise RuntimeError('disk failed')
            yield
        with patch.object(self.store, 'conn', failed):
            with self.assertRaises(RuntimeError): buf.flush()
        self.assertEqual(buf.snapshot()['inflight'], 0)
        self.assertEqual(buf.snapshot()['shared_payload_references'], 1)
        self.assertEqual(buf.snapshot()['shared_payloads'], 1)
        self.assertEqual(buf.pending[('a', 'sensor.x')][2], 20)
        self.assertEqual(buf.flush(), 1)
        self.assertEqual(buf.snapshot()['bytes'], 0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT n FROM rows').fetchone()[0], 20)

    def test_failed_commit_releases_old_version_but_keeps_shared_latest_votes(self):
        self.failed_commit(lambda old: old)

    def test_failed_commit_keeps_distinct_newer_payload_and_releases_old_charge(self):
        self.failed_commit(lambda old: 'new' * 2000)

    def test_oversized_input_is_rejected_before_any_partial_admission(self):
        buf = self.buffer(max_bytes=8192)
        buf.submit([('a', 'sensor.old', 1, 'small')])
        before = buf.snapshot()
        with self.assertRaises(ValueError):
            buf.submit([('a', 'sensor.valid', 2, 'small'), ('a', 'sensor.large', 2, 'x' * 9000)])
        self.assertEqual(buf.snapshot(), before)

    def test_only_observed_pool_registers_shared_json_columns(self):
        service = ContextTournament(self.store, SimpleNamespace())
        with patch('runtime_sqlite_cache.memory_headroom', return_value=(8192, 6000)):
            pool = service.register_context_rows('observed_pool', self.sql)
            quality = service.register_context_rows('sensor_quality', self.sql)
        self.assertEqual(pool.shared_columns, (13, 14, 15))
        self.assertEqual(quality.shared_columns, ())
        self.assertEqual(pool.max_bytes, 64 * MIB)


if __name__ == '__main__': unittest.main()
