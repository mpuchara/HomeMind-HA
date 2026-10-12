import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from history import HistoryManager
from replay import SQLiteTemporalTracker
from storage import Store
from tools.benchmark_causal_statistics import fixture


class ChunkStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.context, self.start, self.end = fixture(20)
        self.addCleanup(self.conn.close)
        self.manager = object.__new__(HistoryManager)
        self.manager.engine = SimpleNamespace(context=self.context)
        self.manager.training_phase_timings = {}

    def load(self, persistent=True):
        return self.manager._training_home_statistics(self.conn, self.start, self.end, persistent)

    def test_reuse_hashes_sources_without_retraining_trajectory(self):
        first = self.load()
        expected = copy.deepcopy(first.statistics_at(self.start + 410))
        with patch.object(first, '_observe', side_effect=AssertionError('Repeated training')):
            self.assertIs(first, self.load())
        self.assertTrue(self.manager.training_phase_timings['causal_home_statistics_reused'])
        self.assertEqual(first.status()['query_calls'], 0)
        self.assertEqual(first.statistics_at(self.start + 410), expected)

    def test_unrelated_writes_and_future_observations_do_not_rebuild(self):
        first = self.load()
        self.conn.execute('CREATE TABLE rewards (value REAL)')
        self.conn.execute('INSERT INTO rewards VALUES(1)')
        self.conn.execute("INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json) "
                          "VALUES('binary_sensor.room',?,?,'on','{}')", (self.end + 5, self.end + 5))
        self.assertIs(first, self.load())

    def test_raw_correction_timestamp_and_delete_invalidate(self):
        first = self.load()
        self.conn.execute("UPDATE entity_history SET state='unavailable' WHERE ts=?", (self.start + 10,))
        second = self.load()
        self.assertIsNot(first, second)
        self.conn.execute('UPDATE entity_history SET ts=ts+0.1 WHERE ts=?', (self.start + 12,))
        third = self.load()
        self.assertIsNot(second, third)
        self.conn.execute('DELETE FROM entity_history WHERE ts=?', (self.start + 25,))
        self.assertIsNot(third, self.load())

    def test_initial_seed_and_newly_visible_late_packet_invalidate(self):
        first = self.load()
        self.conn.execute("UPDATE entity_history SET state='on' WHERE ts=?", (self.start,))
        second = self.load()
        self.assertIsNot(first, second)
        self.conn.execute("INSERT INTO entity_history(entity_id,ts,received_ts,state,attributes_json) "
                          "VALUES('binary_sensor.room',?,?,'on','{}')", (self.start - 1, self.start + 50))
        self.assertIsNot(second, self.load())

    def test_scope_window_and_checkpoint_invalidate(self):
        first = self.load()
        self.context.registry_revision += 1
        second = self.load()
        self.assertIsNot(first, second)
        self.context.options['home_model_half_life_days'] = 10
        third = self.load()
        self.assertIsNot(second, third)
        self.end += 10
        fourth = self.load()
        self.assertIsNot(third, fourth)
        self.conn.execute('CREATE TABLE home_checkpoints(ts REAL,model TEXT)')
        self.conn.execute('INSERT INTO home_checkpoints VALUES(?,?)', (self.start - 1, json.dumps({})))
        self.assertIsNot(fourth, self.load())

    def test_nonpersistent_path_and_close_release_cache(self):
        first = self.load()
        self.assertIsNot(first, self.load(False))
        self.assertIs(first, self.load())
        self.manager.close_persistent_training_resources()
        self.assertIsNone(self.manager._persistent_home_statistics)

    def test_semantic_reliability_change_invalidates(self):
        first = self.load()
        self.context.semantic_reliability.revision += 1
        self.assertIsNot(first, self.load())

    def test_other_connection_source_correction_invalidates(self):
        with tempfile.TemporaryDirectory() as temp:
            reader = sqlite3.connect(Path(temp) / 'sources.db')
            reader.row_factory = sqlite3.Row
            self.conn.backup(reader)
            writer = sqlite3.connect(Path(temp) / 'sources.db')
            try:
                first = self.manager._training_home_statistics(reader, self.start, self.end, True)
                writer.execute("UPDATE entity_history SET state='off' WHERE ts=?", (self.start + 10,))
                writer.commit()
                second = self.manager._training_home_statistics(reader, self.start, self.end, True)
                self.assertIsNot(first, second)
            finally:
                reader.close()
                writer.close()


class IntervalQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'history.db')
        self.base = 1700000000.0
        ctx = SimpleNamespace(options={}, relevant_entities=lambda: [])
        self.tracker = SQLiteTemporalTracker(self.store, ['sensor.a', 'sensor.b'], ctx,
                                            self.base, self.base + 30000)
        self.addCleanup(self.tracker.close)

    def reference(self, lo, hi, limit):
        rows = []
        for eid in ('sensor.a', 'sensor.b'):
            sql = ('SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source '
                   'FROM entity_history WHERE entity_id=? AND ts<=? AND COALESCE(received_ts,ts)<=? '
                   'AND (ts>? OR COALESCE(received_ts,ts)>?) ORDER BY ts DESC,id DESC')
            params = [eid, hi, hi, lo, lo]
            if limit is not None:
                sql += ' LIMIT ?'
                params.append(limit)
            rows.extend(dict(r) for r in self.tracker.conn.execute(sql, params))
        return sorted(rows, key=self.tracker._row_order)

    def test_boundary_late_null_receipt_top_k_and_missing_index_parity(self):
        rows = [(eid, self.base + i, str(i), {}, None, 'live',
                 None if i % 7 == 0 else self.base + i + (25 if i % 3 == 0 else -2))
                for eid in ('sensor.a', 'sensor.b') for i in range(120)]
        self.store.archive_batch(rows)
        for indexed in (True, False):
            self.tracker._archive_received_index = indexed
            for lower, upper in ((0, 20), (20, 50), (50, 51), (100, 150)):
                for limit in (None, 1, 3, 64):
                    lo, hi = self.base + lower, self.base + upper
                    self.assertEqual(self.tracker._sqlite_base_interval_rows(
                        ['sensor.a', 'sensor.b'], lo, hi, limit), self.reference(lo, hi, limit))

    def test_short_interval_work_does_not_scan_old_archive_prefix(self):
        self.store.archive_batch([('sensor.a', self.base + i, str(i), {}, None, 'live', None)
                                  for i in range(12000)])
        self.store.archive_batch([('sensor.a', self.base + 50, 'late', {}, None, 'live', self.base + 11995.5)])
        lo, hi = self.base + 11994, self.base + 11997
        def work(callback):
            steps = [0]
            def tick():
                steps[0] += 100
                return 0
            self.tracker.conn.set_progress_handler(tick, 100)
            try:
                result = callback()
            finally:
                self.tracker.conn.set_progress_handler(None, 0)
            return result, steps[0]
        reference, old_steps = work(lambda: self.reference(lo, hi, None))
        actual, new_steps = work(lambda: self.tracker._sqlite_base_interval_rows(['sensor.a', 'sensor.b'], lo, hi))
        self.assertEqual(actual, reference)
        self.assertLess(new_steps, old_steps / 20)


if __name__ == '__main__':
    unittest.main()
