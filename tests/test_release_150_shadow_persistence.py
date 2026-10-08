"""Detached shadow snapshots, durable ordering and persistence failure recovery."""
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import ROOT
from context_tournament import ContextTournament
from storage import Store


class ShadowPersistence150Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'shadow.db')
        self.engine = SimpleNamespace()
        self.service = ContextTournament(self.store, self.engine)
        self.key = ('agent', 'sensor.radar')

    def model(self, samples=1):
        return dict(version=1, action_count=2, samples=samples,
                    counts={'0:4': [0, samples]}, candidate_policy={'rows': [[1., 2.]]})

    def disk_model(self):
        with self.store.conn() as c:
            row = c.execute('SELECT model_json FROM context_tournament_shadow WHERE '
                            'agent_id=? AND challenger_entity=?', self.key).fetchone()
        return json.loads(row[0]) if row else None

    def test_save_defers_json_and_freezes_nested_model_at_observation(self):
        model = self.model()
        expected = json.loads(json.dumps(model))
        with patch('context_tournament.json.dumps', side_effect=AssertionError('hot path JSON')):
            self.service._save_shadow_model(*self.key, model)
        model['counts']['0:4'][1] = 99
        model['candidate_policy']['rows'][0][0] = 99.
        self.assertEqual(self.service._flush_shadow_models(), 1)
        self.assertEqual(self.disk_model(), expected)
        restarted = ContextTournament(self.store, self.engine)
        self.assertEqual(restarted._load_shadow_model(*self.key, 2), expected)
        with self.store.conn() as c:
            raw = c.execute('SELECT model_json FROM context_tournament_shadow').fetchone()[0]
        self.assertIsInstance(raw, str)
        self.assertEqual(raw, json.dumps(expected, separators=(',', ':'), sort_keys=True))

    def test_coalescing_keeps_latest_complete_cumulative_votes(self):
        model = self.model()
        for count in range(1, 21):
            model['samples'] = count
            model['counts']['0:4'][1] = count
            self.service._save_shadow_model(*self.key, model)
        self.assertEqual(self.service.shadow_persistence_snapshot()['pending'], 1)
        self.assertEqual(self.service._flush_shadow_models(), 1)
        self.assertEqual(self.disk_model()['samples'], 20)
        self.assertEqual(self.disk_model()['counts']['0:4'], [0, 20])

    def test_json_scalar_subclasses_survive_snapshot_and_restart(self):
        import numpy as np
        model = self.model()
        model['candidate_policy']['rows'][0][0] = np.float64(.25)
        self.service._save_shadow_model(*self.key, model)
        self.service._flush_shadow_models()
        self.assertEqual(self.disk_model(), json.loads(json.dumps(model)))

    def test_database_failure_restores_batch_and_retries(self):
        self.service._save_shadow_model(*self.key, self.model())
        with patch.object(self.store, 'conn', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.service._flush_shadow_models()
        self.assertEqual(self.service.shadow_persistence_snapshot()['pending'], 1)
        self.assertEqual(self.service.shadow_persistence_snapshot()['errors'], 1)
        self.assertEqual(self.service._flush_shadow_models(), 1)
        self.assertEqual(self.disk_model()['samples'], 1)

    def test_snapshot_failure_keeps_previous_pending_observation(self):
        self.service._save_shadow_model(*self.key, self.model())
        previous = self.service._shadow_dirty[self.key]
        with patch('context_tournament.pickle.dumps', side_effect=ValueError('snapshot failed')):
            with self.assertRaises(ValueError):
                self.service._save_shadow_model(*self.key, self.model(2))
        self.assertIs(self.service._shadow_dirty[self.key], previous)
        self.service._flush_shadow_models()
        self.assertEqual(self.disk_model()['samples'], 1)

    def test_failed_batch_does_not_replace_newer_pending_snapshot(self):
        self.service._save_shadow_model(*self.key, self.model())

        def failed_conn():
            self.service._save_shadow_model(*self.key, self.model(2))
            raise OSError('disk unavailable')

        with patch.object(self.store, 'conn', side_effect=failed_conn):
            with self.assertRaises(OSError):
                self.service._flush_shadow_models()
        self.service._flush_shadow_models()
        self.assertEqual(self.disk_model()['samples'], 2)

    def test_encoding_failure_restores_all_snapshots_before_any_db_write(self):
        self.service._save_shadow_model(*self.key, self.model())
        self.service._save_shadow_model('agent', 'sensor.other', self.model(2))
        with patch('context_tournament.json.dumps', side_effect=ValueError('encoder failed')):
            with self.assertRaises(ValueError):
                self.service._flush_shadow_models()
        self.assertIsNone(self.disk_model())
        self.assertEqual(self.service.shadow_persistence_snapshot()['pending'], 2)
        self.assertEqual(self.service._flush_shadow_models(), 2)

    def test_overlapping_flushes_cannot_persist_old_batch_after_new_batch(self):
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        errors = []
        original_conn = self.store.conn
        calls = []

        @contextmanager
        def paused_conn():
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test flush was not released')
            with original_conn() as connection:
                yield connection

        def flush(second=False):
            try:
                if second:
                    second_started.set()
                self.service._flush_shadow_models()
            except Exception as exc:
                errors.append(exc)

        self.service._save_shadow_model(*self.key, self.model())
        with patch.object(self.store, 'conn', paused_conn):
            first = threading.Thread(target=flush, name='old-batch')
            second = threading.Thread(target=flush, args=(True,), name='new-batch')
            try:
                first.start()
                self.assertTrue(entered.wait(5))
                self.service._save_shadow_model(*self.key, self.model(2))
                second.start()
                self.assertTrue(second_started.wait(5))
                self.assertFalse(self.service._shadow_flush_lock.acquire(blocking=False))
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, ['old-batch', 'new-batch'])
        self.assertEqual(self.disk_model()['samples'], 2)

    def test_stopped_writer_flushes_pending_snapshot_as_json(self):
        self.service._save_shadow_model(*self.key, self.model())
        self.engine.stop_event = threading.Event()
        self.engine.stop_event.set()
        self.service._shadow_writer()
        self.assertEqual(self.disk_model()['samples'], 1)
        self.assertEqual(self.service.shadow_persistence_snapshot()['pending'], 0)


if __name__ == '__main__':
    unittest.main()
