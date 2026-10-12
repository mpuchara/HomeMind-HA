"""Disk pressure, capped real SQLite, retention and bounded worker output."""
import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
import settings
import storage_files
from storage import Store
from storage_budget import MIB, StoragePressure
from storage_retention import StorageRetention


class StorageBudget175Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.dict('storage.OPTIONS', storage_budget_mode='manual'):
            self.store = Store(self.root / 'data.db')
        self.addCleanup(self.store.stop_wal_keeper)
        self.addCleanup(self.store.stop_wal_checkpoint)
        self.addCleanup(self.store.stop_storage_retention)
        self.budget = self.store.storage_budget

    def free(self, count):
        return patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=count))

    def write(self, key, value='kept'):
        with self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)', (key, value))

    def test_new_database_enables_incremental_vacuum_and_page_cap(self):
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
            self.assertEqual(c.execute('PRAGMA max_page_count').fetchone()[0] * c.execute('PRAGMA page_size').fetchone()[0], 2048 * MIB)

    def test_database_hard_page_limit_rolls_back_and_preserves_preferences(self):
        self.write('preference')
        self.budget.db_limit = 512 * 1024
        with self.assertRaises(sqlite3.DatabaseError), self.store.conn() as c:
            c.execute('INSERT INTO app_meta VALUES(?,?)', ('too-big', 'x' * MIB))
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='preference'").fetchone()[0], 'kept')
            self.assertIsNone(c.execute("SELECT value FROM app_meta WHERE key='too-big'").fetchone())
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertLessEqual(c.execute('PRAGMA page_count').fetchone()[0] * 4096, self.budget.db_limit)

    def test_reserved_space_pauses_bulk_but_preserves_critical_settings(self):
        with self.free(self.budget.reserve - MIB):
            with self.assertRaises(sqlite3.DatabaseError), self.store.conn() as c:
                c.execute("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.radar',1,'on','live')")
            self.write('control-ownership')
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='control-ownership'").fetchone()[0], 'kept')
            self.assertEqual(c.execute('SELECT count(*) FROM entity_history').fetchone()[0], 0)

    def test_prepared_insert_cannot_bypass_new_pressure_and_recovers(self):
        sql = "INSERT INTO entity_history(entity_id,ts,state,source) VALUES(?,?,?,'live')"
        with self.store.connection_session():
            with self.store.conn() as c:
                c.execute(sql, ('sensor.radar', 1, 'on'))
            with self.free(self.budget.reserve - MIB):
                with self.assertRaises(sqlite3.DatabaseError), self.store.conn() as c:
                    c.execute(sql, ('sensor.radar', 2, 'off'))
            with self.store.conn() as c:
                c.execute(sql, ('sensor.radar', 3, 'off'))
        with self.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT ts FROM entity_history ORDER BY ts')], [1, 3])

    def test_hard_reserve_blocks_writes_but_allows_reads(self):
        self.write('model')
        with self.free(self.budget.hard_reserve - 1):
            with self.assertRaises(StoragePressure):
                self.write('blocked')
            with self.store.conn() as c:
                self.assertEqual(c.execute("SELECT value FROM app_meta WHERE key='model'").fetchone()[0], 'kept')
        self.write('recovered')

    def test_cursor_path_has_same_storage_admission(self):
        with self.free(self.budget.hard_reserve - 1):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.cursor().execute("INSERT INTO app_meta VALUES('blocked','no')")

    def test_executemany_pressure_change_rolls_back_complete_batch(self):
        def rows():
            yield ('a', 'one')
            with self.free(self.budget.hard_reserve - 1):
                # The next row arrives after the bounded in-batch sampling window.
                self.budget._last_sample_at -= .051
                yield ('b', 'two')
        with self.assertRaises(StoragePressure), self.store.conn() as c:
            c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM app_meta WHERE key IN ('a','b')").fetchone()[0], 0)

    def test_script_cannot_bypass_hard_reserve(self):
        with self.free(self.budget.hard_reserve - 1):
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.executescript("CREATE TABLE bypass(id); INSERT INTO bypass VALUES(1);")

    def test_soft_pressure_change_during_batch_cannot_use_cached_permissions(self):
        def rows():
            yield ('a', 'one')
            with self.free(self.budget.reserve - MIB):
                self.budget._last_sample_at -= .051
                yield ('b', 'two')
        with self.assertRaises(StoragePressure), self.store.conn() as c:
            c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM app_meta WHERE key IN ('a','b')").fetchone()[0], 0)

    def test_unknown_free_space_fails_closed_for_writes(self):
        with patch('storage_budget.shutil.disk_usage', side_effect=OSError('unknown')):
            with self.assertRaises(StoragePressure):
                self.write('no')
            with self.store.conn() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM agents').fetchone()[0], 0)

    def test_wal_growth_with_pinned_reader_stops_and_recovers(self):
        self.store.start_wal_keeper()
        self.write('seed')
        self.budget.wal_limit = 96 * 1024
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM app_meta').fetchall()
            blocked = False
            for i in range(30):
                try:
                    self.write(str(i), 'x' * 8192)
                except StoragePressure:
                    blocked = True
                    break
            self.assertTrue(blocked)
            self.assertLess(Path(self.store.path + '-wal').stat().st_size, 256 * 1024)
            self.assertEqual(reader.execute("SELECT value FROM app_meta WHERE key='seed'").fetchone()[0], 'kept')
            reader.execute('COMMIT')
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as raw:
            from wal_checkpoint import WALCheckpointWorker
            worker = WALCheckpointWorker(self.store)
            self.assertEqual(worker.RECLAIM_THRESHOLD_BYTES, self.budget.wal_limit // 4)
            self.assertTrue(worker.reclaim(raw, worker.checkpoint(raw)))
            self.assertEqual(Path(self.store.path + '-wal').stat().st_size, 0)
        self.write('after')

    def test_training_writer_stops_at_soft_pressure(self):
        self.budget.training_process = True
        with self.free(self.budget.reserve - MIB):
            with self.assertRaises(StoragePressure):
                self.write('worker-publication')

    def test_payload_limit_rejects_large_write_before_growth(self):
        with patch('storage_budget.MAX_WRITE_BYTES', 512):
            with self.assertRaises(StoragePressure):
                self.write('large', 'x' * 1024)
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT value FROM app_meta WHERE key='large'").fetchone())

    def test_retention_keeps_ten_days_and_one_causal_seed_per_entity(self):
        now = time.time()
        with self.store.conn() as c:
            c.executemany("INSERT INTO entity_history(entity_id,ts,state,source) VALUES(?,?,?,'live')", [
                ('sensor.radar', now - d * 86400, str(d)) for d in (12, 11, 9, 1)] + [
                ('sensor.unchanged', now - 100 * 86400, 'on')])
        worker = StorageRetention(self.store, 10)
        worker.run_once(now)
        with self.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT state FROM entity_history WHERE entity_id='sensor.radar' ORDER BY ts")], ['11', '9', '1'])
            self.assertEqual(c.execute("SELECT state FROM entity_history WHERE entity_id='sensor.unchanged'").fetchone()[0], 'on')

    def test_retention_prunes_provenance_and_preserves_manual_preferences(self):
        now = time.time()
        with self.store.conn() as c:
            c.executescript('CREATE TABLE provenance_events(event_time REAL, payload TEXT); CREATE TABLE manual_feedback_journal(created_time REAL, payload TEXT);')
            c.executemany('INSERT INTO provenance_events VALUES(?,?)', [(now - 11 * 86400, 'old'), (now, 'new')])
            c.execute('INSERT INTO manual_feedback_journal VALUES(?,?)', (now - 100 * 86400, 'keep'))
        StorageRetention(self.store, 10).run_once(now)
        with self.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT payload FROM provenance_events')], ['new'])
            self.assertEqual(c.execute('SELECT payload FROM manual_feedback_journal').fetchone()[0], 'keep')

    def test_retention_reuses_free_pages_and_shrinks_new_database(self):
        now = time.time()
        with self.store.conn() as c:
            c.execute('CREATE TABLE provenance_events(event_time REAL, payload TEXT)')
            c.executemany('INSERT INTO provenance_events VALUES(?,?)', [(now - 11 * 86400, 'x' * 4096)] * 100)
        before = Path(self.store.path).stat().st_size
        worker = StorageRetention(self.store, 10)
        worker.run_once(now)
        self.assertGreater(worker.snapshot()['reclaimed_pages'], 0)
        self.assertLess(Path(self.store.path).stat().st_size, before)

    def test_retention_allows_deletes_when_bulk_recording_is_paused(self):
        now = time.time()
        with self.store.conn() as c:
            c.execute('CREATE TABLE provenance_events(event_time REAL, payload TEXT)')
            c.execute('INSERT INTO provenance_events VALUES(?,?)', (now - 11 * 86400, 'old'))
        with self.free(self.budget.reserve - MIB):
            StorageRetention(self.store, 10).run_once(now)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM provenance_events').fetchone()[0], 0)

    def test_text_timestamp_retention_respects_timezone_boundary(self):
        from datetime import datetime, timezone, timedelta
        now = time.time()
        old = datetime.fromtimestamp(now - 10 * 86400 - 60, timezone(timedelta(hours=12))).isoformat()
        new = datetime.fromtimestamp(now - 10 * 86400 + 60, timezone(timedelta(hours=-12))).isoformat()
        with self.store.conn() as c:
            c.executemany('INSERT INTO events(created_at,level,kind,message) VALUES(?,?,?,?)', [(old,'info','old','old'), (new,'info','new','new')])
        StorageRetention(self.store, 10).run_once(now)
        with self.store.conn() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT kind FROM events')], ['new'])

    def test_window_children_are_deleted_before_parent_with_foreign_keys(self):
        with self.store.conn() as c:
            c.execute('PRAGMA foreign_keys=ON')
            c.executescript('CREATE TABLE feature_windows(window_id TEXT PRIMARY KEY, created_time REAL); CREATE TABLE feature_window_entities(window_id TEXT REFERENCES feature_windows(window_id), entity_id TEXT, PRIMARY KEY(window_id,entity_id));')
            c.executemany('INSERT INTO feature_windows VALUES(?,?)', [('old', 1), ('new', 100)])
            c.executemany('INSERT INTO feature_window_entities VALUES(?,?)', [('old','sensor.a'), ('new','sensor.a')])
            removed = StorageRetention(self.store)._delete_batch(c, 'feature_windows', 'created_time<?', (10,), 'created_time', {'feature_windows','feature_window_entities'})
            self.assertEqual(removed, 1)
            self.assertEqual([r[0] for r in c.execute('SELECT window_id FROM feature_window_entities')], ['new'])

    def test_legacy_options_are_migrated_without_old_long_memory(self):
        path = self.root / 'options.json'
        path.write_text(json.dumps({'agent_training_history_days': 7, 'agent_training_long_memory_days': 35, 'archive_retention_days': 365}))
        with patch.object(settings, 'OPTIONS_PATH', path):
            options = settings.load_options()
        self.assertEqual(options['agent_training_history_days'], 14)
        self.assertEqual(options['agent_training_long_memory_days'], 14)
        self.assertEqual(options['archive_retention_days'], 14)

    def test_worker_json_quota_preserves_previous_result(self):
        path = self.root / 'result.json'
        storage_files.atomic_text(path, 'original')
        with patch.object(storage_files, 'DIRECTORY_LIMIT_BYTES', 12):
            with self.assertRaises(StoragePressure):
                storage_files.atomic_text(path, 'x' * 20)
        self.assertEqual(path.read_text(), 'original')
        self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_worker_log_cap_drains_excess_without_growing_disk(self):
        stream = io.BytesIO(b'a' * 20000)
        path = self.root / 'log.log'
        with patch.object(storage_files, 'LOG_LIMIT_BYTES', 1000):
            storage_files.drain_log(stream, path)
        self.assertTrue(stream.closed)
        self.assertEqual(path.stat().st_size, 1000)

    def test_completed_transport_files_expire_without_erasing_unknown_files(self):
        key = 'a' * 16
        for suffix in ('job.json', 'result.json', 'log'):
            (self.root / (key + '.' + suffix)).write_text('data')
        (self.root / 'user-notes.txt').write_text('keep')
        storage_files.prune_completed(self.root, now=time.time() + 11 * 86400)
        self.assertFalse((self.root / (key + '.job.json')).exists())
        self.assertEqual((self.root / 'user-notes.txt').read_text(), 'keep')

    def test_runtime_storage_snapshot_does_not_touch_disk(self):
        with patch('storage_budget.shutil.disk_usage', side_effect=AssertionError('not RAM only')):
            snapshot = self.store.sqlite_snapshot()
        self.assertEqual(snapshot['storage_budget']['database_max_bytes'], 2048 * MIB)

    def test_expired_calibration_invalidates_cached_reports(self):
        from confidence_contract import ProbabilityCalibrationJournal
        journal = ProbabilityCalibrationJournal(self.store)
        now = time.time()
        journal.record(metric_id='action', model_key='model', scope_id='room',
                       episode_id='old', ts=now - 11 * 86400, prediction=.8,
                       observed=1, source_kind='human')
        with self.store.conn() as c:
            before = c.execute('SELECT revision FROM confidence_probability_revisions').fetchone()[0]
        StorageRetention(self.store, 10).run_once(now)
        self.assertEqual(journal.rows('action', 'model', 'room'), [])
        with self.store.conn() as c:
            after = c.execute('SELECT revision FROM confidence_probability_revisions').fetchone()[0]
        self.assertGreater(after, before)

    def test_repeated_storage_failures_do_not_flood_supervisor_log(self):
        with patch.object(self.store, 'flush_events', side_effect=StoragePressure('disk reserve')), patch('builtins.print') as output:
            for _ in range(100):
                self.store.event(None, 'error', 'storage', 'Recording paused')
        self.assertEqual(output.call_count, 1)
        self.assertEqual(len(self.store._event_buffer), 100)

    def test_training_descriptor_refuses_pressure_before_starting_worker(self):
        import training_process
        with self.free(1024 * MIB), patch('storage.STORE', self.store):
            with self.assertRaises(StoragePressure):
                training_process._build_job(SimpleNamespace(), 0, 1, {})
