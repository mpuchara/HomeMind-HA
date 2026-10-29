"""Pure syntax reuse cannot cache data or bypass storage admission."""
from contextlib import closing
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from runtime_sqlite_cache import (RuntimeSQLiteCache, session_sql_taints,
                                  _cached_session_sql_taints)
from storage import Store
from storage_budget import StoragePressure
from tools.benchmark_sql_session_classification import reference, STATEMENTS


class SessionSyntaxTests(unittest.TestCase):
    def setUp(self):
        _cached_session_sql_taints.cache_clear()

    def test_reference_parity_for_comments_pragmas_and_generated_syntax(self):
        rng = random.Random(232)
        prefixes = ['', ' ', '-- header\n', '/* a\nb */ ', '-- a\n/*b*/\n']
        suffixes = ['', ';', ' ; ', '(agents)', '=0', ' = ON', ' -- trailing']
        for _ in range(3000):
            sql = rng.choice(prefixes)+rng.choice(STATEMENTS)+rng.choice(suffixes)
            self.assertEqual(session_sql_taints(sql), reference(sql), sql)

    def test_both_entry_count_and_retained_key_length_are_bounded(self):
        for i in range(700):
            session_sql_taints('SELECT '+str(i))
        self.assertEqual(_cached_session_sql_taints.cache_info().currsize, 512)
        info = _cached_session_sql_taints.cache_info()
        for sql in ('SELECT '+('x'*4000), 'SELECT '+('ą'*4000)):
            self.assertEqual(session_sql_taints(sql), reference(sql))
        self.assertEqual(_cached_session_sql_taints.cache_info(), info)

    def test_mutating_stringification_never_reuses_a_previous_classification(self):
        class SQL:
            value = 'SELECT 1'
            def __str__(self):
                return self.value
        item = SQL()
        self.assertFalse(session_sql_taints(item))
        item.value = 'ATTACH DATABASE ? AS other'
        self.assertTrue(session_sql_taints(item))
        self.assertEqual(_cached_session_sql_taints.cache_info().currsize, 0)

    def test_warm_statement_reads_fresh_data_and_rechecks_write_pressure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'test.db')
            store.start_wal_keeper()
            store._runtime_sqlite_cache = RuntimeSQLiteCache(memory=lambda: (8192, 6144))
            try:
                with store.conn() as c:
                    c.execute('CREATE TABLE syntax_test(value TEXT)')
                    c.execute("INSERT INTO syntax_test VALUES('before')")
                for expected in ('before', 'after'):
                    with store.inference_connection_session(), store.conn() as c:
                        self.assertEqual(c.execute('SELECT value FROM syntax_test').fetchone()[0], expected)
                    with closing(sqlite3.connect(store.path)) as other, other:
                        other.execute("UPDATE syntax_test SET value='after'")
                sql = "INSERT INTO entity_history(entity_id,ts,state,source) VALUES(?,?,?,?)"
                with store.inference_connection_session(), store.conn() as c:
                    c.execute(sql, ('sensor.a', 1., 'on', 'live'))
                with self.assertRaises(StoragePressure), store.inference_connection_session(), store.conn() as c:
                    with patch.object(store.storage_budget, 'check', side_effect=StoragePressure('new pressure')):
                        c.execute(sql, ('sensor.a', 2., 'off', 'live'))
                with store.conn() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM entity_history').fetchone()[0], 1)
            finally:
                store.stop_runtime_cache()
                store.stop_wal_keeper()

    def test_taint_is_applied_to_every_connection_even_on_cache_hits(self):
        from runtime_sqlite_cache import CachedBudgetConnection
        for _ in range(2):
            with closing(sqlite3.connect(':memory:', factory=CachedBudgetConnection)) as c:
                c.prepare_cache_tracking()
                c.execute('CREATE TEMP TABLE example(value)')
                self.assertTrue(c.cache_tainted)
                self.assertFalse(c.finish_lease())
        self.assertGreater(_cached_session_sql_taints.cache_info().hits, 0)

    def test_probe_is_bounded_and_preserves_reentrant_nonblocking_lock_contract(self):
        import threading
        from tools.runtime_scaling_probe import Probe, TimedLock
        probe = Probe(capacity=3)
        lock = TimedLock(threading.RLock(), probe, 'test')
        with lock:
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
            result = []
            worker = threading.Thread(target=lambda: result.append(lock.acquire(blocking=False)))
            worker.start(); worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [False])
        for _ in range(10):
            with probe.span('bounded'):
                pass
        row = probe.snapshot()['operations']['bounded']
        self.assertEqual(row['count'], 10)
        self.assertEqual(len(row['samples']), 3)
        self.assertEqual(row['dropped'], 7)


if __name__ == '__main__':
    unittest.main()
