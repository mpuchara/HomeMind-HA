"""Bounded import admission, unchanged retries and causal mutation parity."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from storage import Store
from storage_budget import MIB, StoragePressure
from tools.fixtures.archive_batch_014184 import archive_batch as reference_batch


class ArchiveWrites185Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'archive.db')
        self.reference = Store(Path(temp.name) / 'reference.db')
        self.budget = self.store.storage_budget

    def row(self, **changes):
        values = dict(entity='sensor.test', ts=100., state='on', attrs={'a': 1},
                      user='human', source='ha_history_full', received=None)
        values.update(changes)
        return tuple(values.values())

    def snapshot(self, store=None):
        return list((store or self.store).archive_iter())

    def free(self, count):
        return patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=count))

    def test_short_batch_uses_three_fresh_samples_and_scopes_to_connection(self):
        with self.store.connection_session() as c, \
             patch('storage_budget.time.monotonic', return_value=100.):
            before = self.budget.snapshot()['disk_samples']
            self.store.archive_batch([self.row(ts=i + 1) for i in range(128)])
            self.assertEqual(self.budget.snapshot()['disk_samples'] - before, 3)
            self.assertEqual(c.storage_batch_depth, 0)
            before = self.budget.snapshot()['disk_samples']
            with self.store.conn() as writer:
                writer.execute("INSERT INTO app_meta VALUES('ordinary','guarded')")
            self.assertEqual(self.budget.snapshot()['disk_samples'] - before, 3)
        self.assertEqual(self.store.archive_count(), 128)

    def test_unknown_space_before_batch_preserves_existing_row(self):
        self.store.archive_batch([self.row()])
        before = self.snapshot()
        with patch('storage_budget.shutil.disk_usage', side_effect=OSError('unavailable')):
            with self.assertRaises(StoragePressure):
                self.store.archive_batch([self.row(state='off')])
        self.assertEqual(self.snapshot(), before)

    def test_final_commit_still_refreshes_after_batch_scope(self):
        with patch('storage_budget.time.monotonic', return_value=100.), \
             self.assertRaises(StoragePressure), self.store.conn() as c:
            with c.storage_batch():
                c.execute("INSERT INTO app_meta VALUES('new','value')")
            with self.free(self.budget.hard_reserve - 1):
                c.commit()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_soft_pressure_at_scope_exit_rolls_back_rows_and_revision(self):
        self.store.archive_batch([self.row()])
        before = self.snapshot()
        with self.store.connection_session() as c:
            original = c.executemany
            def pressure_after_sql(sql, rows):
                result = original(sql, rows)
                self.budget.reserve = 10 ** 18
                return result
            with patch.object(c, 'executemany', side_effect=pressure_after_sql):
                with self.assertRaises(StoragePressure):
                    self.store.archive_batch([self.row(state='off')])
            self.assertEqual(c.storage_batch_depth, 0)
            self.assertFalse(c.in_transaction)
        self.assertEqual(self.snapshot(), before)

    def test_long_batch_samples_pressure_before_next_row_and_rolls_back(self):
        clock = SimpleNamespace(now=100.)
        with patch('storage_budget.time.monotonic', side_effect=lambda: clock.now), \
             self.assertRaises(StoragePressure), self.store.conn() as c, c.storage_batch():
            def rows():
                yield ('first', 'small')
                clock.now += .051
                with self.free(self.budget.hard_reserve - 1):
                    yield ('second', 'small')
            c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_payload_threshold_and_large_single_row_still_fail_closed(self):
        for large in (False, True):
            with patch('storage_budget.time.monotonic', return_value=100.), \
                 self.assertRaises(StoragePressure), self.store.conn() as c, c.storage_batch():
                def rows():
                    yield ('first', 'x' * (MIB // 2))
                    with self.free(self.budget.hard_reserve - 1):
                        yield ('second', 'x' * (17 * MIB if large else MIB // 2))
                c.executemany('INSERT INTO app_meta VALUES(?,?)', rows())
            with self.store.conn() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_unchanged_retry_preserves_row_revision_and_archive_fingerprint(self):
        self.store.archive_batch([self.row()])
        before = self.snapshot()
        fingerprint = self.store.archive_window_fingerprint(['sensor.test'], 0, 200)
        self.assertEqual(self.store.archive_batch([self.row()]), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.archive_window_fingerprint(['sensor.test'], 0, 200), fingerprint)

    def test_effectively_unchanged_merge_preserves_revision(self):
        self.store.archive_batch([self.row(source='live', received=101.)])
        before = self.snapshot()
        self.store.archive_batch([self.row(source='ha_history_fast_context', attrs={}, user=None, received=102.)])
        self.assertEqual(self.snapshot(), before)

    def test_each_material_field_change_invalidates_fingerprint(self):
        for index, (before_row, after_row) in enumerate((
            (self.row(), self.row(state=None)),
            (self.row(state=None), self.row(state='on')),
            (self.row(), self.row(attrs={'a': 2})),
            (self.row(), self.row(user='other')),
            (self.row(source='ha_history_minimal'), self.row()),
            (self.row(received=None), self.row(received=101.)),
            (self.row(source='ha_history_minimal', received=101.), self.row(source='live', received=102.)),
        )):
            before_row = (before_row[0], 100. + index, *before_row[2:])
            after_row = (after_row[0], 100. + index, *after_row[2:])
            with self.subTest(after_row=after_row):
                self.store.archive_batch([before_row])
                before = self.snapshot()[-1]['mutation_revision']
                self.store.archive_batch([after_row])
                self.assertGreater(self.snapshot()[-1]['mutation_revision'], before)

    def test_merges_match_frozen_writer_for_null_attributes_sources_and_receive_time(self):
        sequence = [self.row(state=None, attrs={}, user=None, source='ha_history_minimal'),
                    self.row(received=101.), self.row(source='live', received=102.),
                    self.row(attrs={}, user=None, source='ha_history_minimal', received=103.),
                    self.row(state='off'), self.row(), self.row()]
        for row in sequence:
            self.store.archive_batch([row])
            reference_batch(self.reference, [row])
            actual = [{k: v for k, v in r.items() if k != 'mutation_revision'} for r in self.snapshot()]
            expected = [{k: v for k, v in r.items() if k != 'mutation_revision'} for r in self.snapshot(self.reference)]
            self.assertEqual(actual, expected)

    def test_legacy_six_field_rows_keep_existing_receive_timestamp(self):
        self.store.archive_batch([self.row(source='live', received=101.)])
        self.store.archive_batch([self.row(state='off')[:-1]])
        self.assertEqual(self.snapshot()[0]['received_ts'], 101.)
        self.assertEqual(self.snapshot()[0]['state'], 'off')

    def test_later_failure_rolls_back_only_current_batch(self):
        self.store.archive_batch([self.row()])
        before = self.snapshot()
        with self.assertRaises(sqlite3.OperationalError), self.store.conn() as c, c.storage_batch():
            c.execute("UPDATE entity_history SET state='bad'")
            c.execute('INSERT INTO missing_table VALUES(1)')
        self.assertEqual(self.snapshot(), before)
        with self.store.conn() as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
