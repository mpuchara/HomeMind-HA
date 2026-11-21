"""Real SQLite coverage for the replay interval and its single causal seed."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import support
from history_recovery import prepare_generation
from storage import Store
from training_snapshot import capture, install, validate


TARGET = 'light.kitchen'


class TrainingHistoryFenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / 'source.db')
        self.addCleanup(self.store.stop_storage_retention)
        self.owner = self.store.create_agent(support.agent(target_entity=TARGET))['id']
        self.store.archive_batch([(TARGET, stamp, 'off', {}, None, 'ha_history_full')
                                  for stamp in (10., 15., 19., 20., 25., 30., 40.)])

    def freeze(self):
        with self.store.conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            install(conn)
            return capture(conn, 30., [TARGET], start_ts=20., owner=self.owner)

    def check(self, boundary):
        with self.store.conn() as conn:
            validate(conn, boundary)

    def prepare(self, **kwargs):
        return prepare_generation(self.store.path, self.root / 'generation.db', [TARGET],
                                  20., 30., agent_id=self.owner, **kwargs)

    def stop_during_copy(self):
        stopped = []

        def progress(**fields):
            if fields.get('preparation', {}).get('table_name') == 'entity_history':
                stopped.append(True)

        with self.assertRaises(InterruptedError):
            self.prepare(progress=progress, cancelled=lambda: bool(stopped))

    def test_irrelevant_old_delete_and_recorder_enrichment_allow_resume(self):
        self.stop_during_copy()
        with self.store.conn() as conn:
            conn.execute('DELETE FROM entity_history WHERE ts=10')
        self.store.archive_batch([(TARGET, 15., 'on', {}, None, 'ha_history_full')])
        report = self.prepare()
        self.assertTrue(report['resumed'])
        with closing(sqlite3.connect(self.root / 'generation.db')) as conn:
            self.assertEqual([row[0] for row in conn.execute('SELECT ts FROM entity_history ORDER BY ts')],
                             [19., 20., 25., 30.])

    def test_old_physical_row_that_becomes_seed_invalidates(self):
        boundary = self.freeze()
        with self.store.conn() as conn:
            conn.execute('UPDATE entity_history SET ts=19.5 WHERE ts=15')
        with self.assertRaisesRegex(RuntimeError, 'prefix changed: entity_history'):
            self.check(boundary)

    def test_delete_of_the_causal_seed_invalidates(self):
        boundary = self.freeze()
        with self.store.conn() as conn:
            conn.execute('DELETE FROM entity_history WHERE ts=19')
        with self.assertRaisesRegex(RuntimeError, 'prefix changed: entity_history'):
            self.check(boundary)

    def test_update_from_after_end_into_replay_invalidates(self):
        boundary = self.freeze()
        with self.store.conn() as conn:
            conn.execute('UPDATE entity_history SET ts=29 WHERE ts=40')
        with self.assertRaisesRegex(RuntimeError, 'prefix changed: entity_history'):
            self.check(boundary)

    def test_worker_can_replace_a_stale_copy_once_from_corrected_source(self):
        self.stop_during_copy()
        self.store.save_model(self.owner, {'marker': 'prior model'})
        previous = self.store.get_model(self.owner)
        self.store.archive_upsert(TARGET, 25., 'on', source='ha_history_full')
        report = self.prepare(restart_stale_history=True)
        self.assertTrue(report['stale_history_checkpoint_restarted'])
        self.assertFalse(report['resumed'])
        self.assertEqual(self.store.get_model(self.owner), previous)
        with closing(sqlite3.connect(self.root / 'generation.db')) as conn:
            self.assertEqual(conn.execute('SELECT state FROM entity_history WHERE ts=25').fetchone()[0], 'on')
