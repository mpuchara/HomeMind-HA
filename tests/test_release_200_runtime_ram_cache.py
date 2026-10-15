"""Real WAL freshness, storage guards, lease isolation and bounded RAM targets."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import support
from runtime_sqlite_cache import RuntimeSQLiteCache, memory_headroom
from storage import Store
from storage_budget import StoragePressure, MIB
from wal_checkpoint import WALCheckpointWorker


class RuntimeRAMCache200Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ram-cache-200-')
        self.addCleanup(self.directory.cleanup)
        with patch.dict('storage.OPTIONS', storage_budget_mode='manual'):
            self.store = Store(Path(self.directory.name) / 'test.db')
        self.store.start_wal_keeper()
        self.addCleanup(self.store.stop_wal_keeper)
        self.pool = RuntimeSQLiteCache(memory=lambda: (8192, 6144))
        self.store._runtime_sqlite_cache = self.pool
        self.addCleanup(self.pool.stop)
        with self.store.conn() as c:
            c.execute('CREATE TABLE cache_test(id INTEGER PRIMARY KEY, value TEXT)')
            c.executemany('INSERT INTO cache_test VALUES(?,?)', [(i, str(i)) for i in range(20)])

    def session(self):
        return self.store.inference_connection_session()

    def test_warm_inference_reuses_pages_and_prepared_statements_with_fresh_sql(self):
        with self.session() as first:
            self.assertEqual(first.execute('PRAGMA cache_size').fetchone()[0], -65536)
            self.assertEqual(first.execute('PRAGMA mmap_size').fetchone()[0], 0)
            with self.store.conn() as c:
                self.assertIs(c, first)
                self.assertEqual(c.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], '1')
        with closing(sqlite3.connect(self.store.path)) as external, external:
            external.execute("UPDATE cache_test SET value='fresh' WHERE id=1")
        with self.session() as second:
            self.assertIs(first, second)
            self.assertEqual(second.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], 'fresh')
        self.assertEqual(self.pool.snapshot()['hits'], 1)
        self.assertFalse(hasattr(self.store._connection_session, 'connection'))

    def test_unconsumed_cursor_is_closed_and_does_not_pin_wal_between_decisions(self):
        with self.session() as c:
            cursor = c.execute('SELECT * FROM cache_test')
            self.assertEqual(cursor.fetchone()[0], 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as external:
            external.execute("UPDATE cache_test SET value='external'")
            self.assertEqual(external.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
        with self.session() as fresh:
            self.assertEqual(fresh.execute('SELECT count(*) FROM cache_test WHERE value=?', ('external',)).fetchone()[0], 20)

    def test_each_write_commits_and_failure_rolls_back_only_current_transaction(self):
        with self.assertRaises(ValueError), self.session() as c:
            with self.store.conn() as same:
                same.execute("UPDATE cache_test SET value='committed' WHERE id=1")
            with closing(sqlite3.connect(self.store.path)) as external:
                self.assertEqual(external.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], 'committed')
            with self.store.conn() as same:
                same.execute("UPDATE cache_test SET value='rollback' WHERE id=2")
                raise ValueError('later observation failed')
        self.assertEqual(self.pool.snapshot()['idle'], 0)
        with self.session() as next_connection:
            self.assertIsNot(c, next_connection)
            self.assertEqual(next_connection.execute('SELECT value FROM cache_test WHERE id=2').fetchone()[0], '2')

    def test_background_nested_timeout_restored_and_active_writer_not_borrowed(self):
        with self.session() as anchor:
            with self.store.background_sqlite(), self.store.connection_session(), self.store.conn() as c:
                self.assertIs(c, anchor)
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 250)
            self.assertEqual(anchor.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            with self.assertRaises(ValueError), self.store.conn() as owner:
                owner.execute("UPDATE cache_test SET value='private' WHERE id=1")
                with self.store.conn() as independent:
                    self.assertIsNot(owner, independent)
                    self.assertEqual(independent.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], '1')
                raise ValueError('rollback')

    def test_real_storage_pressure_keeps_bulk_write_admission_and_rollback(self):
        budget = self.store.storage_budget
        with self.session():
            with patch('storage_budget.shutil.disk_usage', return_value=type('Disk', (), {'free': budget.reserve - MIB})()):
                with self.assertRaises(StoragePressure), self.store.conn() as c:
                    c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.a',1,'on','live')")
                self.assertFalse(c.in_transaction)
            with self.store.conn() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM entity_history').fetchone()[0], 0)

    def test_commit_pressure_rolls_back_a_write_already_admitted(self):
        with self.session(), self.assertRaises(StoragePressure):
            with self.store.conn() as c:
                c.execute("UPDATE cache_test SET value='uncommitted' WHERE id=1")
                with patch.object(self.store.storage_budget, 'check', side_effect=StoragePressure('commit pressure')):
                    c.commit()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], '1')

    def test_settings_and_session_local_extensions_discard_the_connection(self):
        operations = [lambda c: c.execute('PRAGMA query_only=ON'),
                      lambda c: c.execute('CREATE TEMP TABLE private(n)'),
                      lambda c: c.executescript('CREATE TEMP TABLE private(n);'),
                      lambda c: c.create_function('private_function', 0, lambda: 1),
                      lambda c: c.set_authorizer(lambda *_: sqlite3.SQLITE_OK),
                      lambda c: c.set_progress_handler(lambda: 0, 1000)]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.session() as c:
                    operation(c)
                self.assertEqual(self.pool.snapshot()['idle'], 0)
                with self.assertRaises(sqlite3.ProgrammingError):
                    c.execute('SELECT 1')

    def test_concurrent_sessions_have_exclusive_connections_and_no_queue_wait(self):
        barrier = threading.Barrier(5)
        connections, errors = [], []
        def visit():
            try:
                with self.session() as c:
                    connections.append(c)
                    self.assertEqual(c.execute('SELECT count(*) FROM cache_test').fetchone()[0], 20)
                    barrier.wait(5)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=visit) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len({id(c) for c in connections}), 5)
        stats = self.pool.snapshot()
        self.assertEqual(stats['idle'], 4)
        self.assertEqual(stats['bypasses'], 1)
        self.assertLessEqual(stats['cache_target_mb'], 256)

    def test_headroom_shrink_releases_idle_and_retires_active_lease(self):
        headroom, clock = [8192, 6144], [0]
        self.pool.memory, self.pool.clock = lambda: tuple(headroom), lambda: clock[0]
        self.pool.last_sample = -100
        with self.session():
            pass
        with self.session():
            headroom[1] = 500
            clock[0] = 6
        self.assertEqual(self.pool.snapshot()['cache_target_mb'], 0)
        self.assertEqual(self.pool.snapshot()['effective_mb'], 0)
        with self.session():
            self.assertEqual(self.pool.snapshot()['bypasses'], 1)

    def test_stop_closes_idle_and_inflight_on_return_without_reopening_pool(self):
        with self.session() as active:
            self.pool.stop()
            self.assertEqual(active.execute('SELECT 1').fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            active.execute('SELECT 1')
        with self.session() as bypass:
            self.assertNotIn(bypass, self.pool.leased)

    def test_unknown_memory_and_zero_option_disable_extra_cache(self):
        for configured, memory in [(256, (None, None)), (0, (8192, 6144)), (256, (1024, 400))]:
            pool = RuntimeSQLiteCache(configured, memory=lambda: memory)
            with self.subTest(configured=configured, memory=memory):
                self.assertIsNone(pool.acquire(lambda _: self.fail('disabled pool opened a connection')))
                self.assertEqual(pool.snapshot()['cache_target_mb'], 0)
            pool.stop()

    def test_container_limits_and_ancestors_cap_host_memory(self):
        files = {'/proc/meminfo': 'MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n',
                 '/proc/self/cgroup': '0::/addon\n',
                 '/sys/fs/cgroup/addon/memory.max': 'max',
                 '/sys/fs/cgroup/memory.max': str(1024 ** 3),
                 '/sys/fs/cgroup/memory.current': str(768 * 1024 ** 2)}
        def read(path, *args, **kwargs):
            if path.as_posix() in files:
                return files[path.as_posix()]
            raise FileNotFoundError(path)
        with patch.object(Path, 'read_text', read), patch.object(Path, 'is_dir', return_value=True):
            self.assertEqual(memory_headroom(), (1024, 256))

    def test_training_process_never_leases_realtime_cache(self):
        self.store.training_worker_process = True
        with self.session() as c:
            self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0], 60000)
            self.assertNotIn(c, self.pool.leased)
        self.assertEqual(self.pool.snapshot()['misses'], 0)

    def test_real_dispatch_benchmark_preserves_every_result(self):
        from tools.benchmark_runtime_ram_cache import run_mode
        previous, current = run_mode('previous', 3), run_mode('current', 3)
        self.assertEqual(previous['result_sha256'], current['result_sha256'])
        self.assertEqual(current['runtime_cache']['hits'], 3)
        self.assertEqual(current['runtime_cache']['misses'], 1)

    def test_background_memory_trim_releases_idle_cache_without_new_inference(self):
        headroom, clock = [8192, 6144], [0]
        self.pool.memory, self.pool.clock = lambda: tuple(headroom), lambda: clock[0]
        self.pool.last_sample = -100
        with self.session() as c:
            pass
        headroom[1], clock[0] = 500, 6
        WALCheckpointWorker(self.store).sample_storage()
        self.assertEqual(self.pool.snapshot()['idle'], 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            c.execute('SELECT 1')

    def test_cold_setup_does_not_lock_out_a_parallel_inference(self):
        opening, finish, visited, errors = threading.Event(), threading.Event(), threading.Event(), []
        def slow_factory(size):
            opening.set()
            self.assertTrue(finish.wait(5))
            return self.store._cached_connection(size)
        def slow():
            try:
                c = self.pool.acquire(slow_factory)
                self.pool.release(c, healthy=True)
            except BaseException as exc:
                errors.append(exc)
        def peer():
            try:
                with self.session():
                    visited.set()
            except BaseException as exc:
                errors.append(exc)
        first, second = threading.Thread(target=slow), threading.Thread(target=peer)
        first.start()
        self.assertTrue(opening.wait(3))
        second.start()
        try:
            self.assertTrue(visited.wait(3))
        finally:
            finish.set()
            first.join(5)
            second.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.pool.snapshot()['idle'], 2)

    def test_writer_outlasting_quiet_window_is_busy_and_keeps_committed_rows(self):
        worker = WALCheckpointWorker(self.store)
        worker.RECLAIM_THRESHOLD_BYTES, worker.quiesce_seconds = 1, 0
        with closing(sqlite3.connect(self.store.path)) as blocker:
            blocker.execute("UPDATE cache_test SET value='kept' WHERE id=1")
            with closing(sqlite3.connect(self.store.path, timeout=0, isolation_level=None)) as checkpointer:
                self.assertFalse(worker.reclaim(checkpointer, dict(busy=0, remaining_frames=0), now=100))
            self.assertFalse(self.store._wal_reclaiming.is_set())
            self.assertEqual(worker.snapshot()['reclaim_busy'], 1)
            self.assertEqual(worker.snapshot()['reclaim_errors'], 0)
            blocker.commit()
        with self.session() as c:
            self.assertEqual(c.execute('SELECT value FROM cache_test WHERE id=1').fetchone()[0], 'kept')
