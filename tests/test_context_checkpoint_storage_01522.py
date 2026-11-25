"""Real SQLite regression tests for adaptive presence checkpoint durability."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
import context_engine
from context_engine import ContextEngine
from storage import Store
from settings import DEFAULT_OPTIONS


class ContextCheckpointStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.store = Store(self.path)
        self.context = ContextEngine(dict(DEFAULT_OPTIONS), self.store)
        self.now = time.time()
        self.save(self.now)

    def save(self, stamp):
        with patch.object(context_engine, "time", SimpleNamespace(time=lambda: stamp)):
            return self.context.save(force=True)

    def label(self, stamp):
        self.context.adaptive_presence.record_independent_label(
            "sensor.raw", .4, True, "binary_sensor.reference",
            ts=stamp, label_event_ts=stamp-1)

    def rows(self):
        with self.store.conn() as c:
            meta = c.execute("SELECT value FROM app_meta WHERE key=?",
                             (self.context.ADAPTIVE_MODEL_KEY,)).fetchone()[0]
            history = [tuple(row) for row in c.execute(
                "SELECT ts,model_json FROM adaptive_presence_checkpoints ORDER BY ts")]
        return meta, history

    def test_changed_adaptive_model_uses_one_connection_and_exact_replay_payload(self):
        before, history = self.rows()
        self.label(self.now+60)
        with patch.object(self.store, "conn", wraps=self.store.conn) as conn:
            self.assertTrue(self.save(self.now+61))
            self.assertEqual(conn.call_count, 1)
        after, checkpoints = self.rows()
        self.assertNotEqual(before, after)
        self.assertEqual(checkpoints, history+[(self.now+61, after)])
        self.assertEqual(self.store.meta_get(self.context.ADAPTIVE_MODEL_KEY), after)
        self.assertEqual(json.loads(after), self.context.adaptive_presence.export())
        # The strict causal replay lookup must still see the earlier checkpoint.
        with self.store.conn() as c:
            earlier = c.execute("SELECT model_json FROM adaptive_presence_checkpoints "
                                "WHERE ts<? ORDER BY ts DESC LIMIT 1", (self.now+61,)).fetchone()[0]
        self.assertEqual(earlier, before)
        restarted = ContextEngine(dict(DEFAULT_OPTIONS), Store(self.path))
        self.assertEqual(restarted.adaptive_presence.export(), json.loads(after))
        with patch.object(self.store, "conn", side_effect=AssertionError("unchanged save wrote")):
            self.assertFalse(self.save(self.now+62))

    def test_failed_checkpoint_rolls_back_metadata_and_retries(self):
        before = self.rows()
        last_save = self.context.last_save
        self.label(self.now+60)
        with self.store.conn() as c:
            c.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON adaptive_presence_checkpoints "
                      "BEGIN SELECT RAISE(ABORT,'test checkpoint failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test checkpoint failure"):
            self.save(self.now+61)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.meta_get(self.context.ADAPTIVE_MODEL_KEY), before[0])
        self.assertEqual(self.context.last_save, last_save)
        with self.store.conn() as c:
            c.execute("DROP TRIGGER reject_checkpoint")
        self.assertTrue(self.save(self.now+62))
        after, rows = self.rows()
        self.assertNotEqual(after, before[0])
        self.assertEqual(rows[-1], (self.now+62, after))

    def test_same_timestamp_retry_avoids_updates_and_keeps_retention_boundary(self):
        raw, _ = self.rows()
        cutoff = self.now - self.context.ADAPTIVE_CHECKPOINT_RETENTION_SECONDS
        with self.store.conn() as c:
            c.executemany("INSERT INTO adaptive_presence_checkpoints VALUES(?,?)",
                          [(cutoff-1, raw), (cutoff, raw)])
            c.execute("CREATE TABLE checkpoint_probe(kind TEXT)")
            for table in ("app_meta", "adaptive_presence_checkpoints"):
                c.execute(f"CREATE TRIGGER probe_{table} AFTER UPDATE ON {table} "
                          f"BEGIN INSERT INTO checkpoint_probe VALUES('{table}'); END")
        self.store.save_adaptive_presence_checkpoint(self.context.ADAPTIVE_MODEL_KEY,
                                                    raw, self.now, cutoff)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM checkpoint_probe").fetchone()[0], 0)
            self.assertEqual([r[0] for r in c.execute("SELECT ts FROM adaptive_presence_checkpoints ORDER BY ts")],
                             [cutoff, self.now])
        # Genuine change at the same timestamp preserves the old replace semantics.
        self.label(self.now+60)
        changed = json.dumps(self.context.adaptive_presence.export(), separators=(',', ':'), sort_keys=True)
        self.store.save_adaptive_presence_checkpoint(self.context.ADAPTIVE_MODEL_KEY,
                                                    changed, self.now, cutoff)
        self.assertEqual(self.rows()[1][-1], (self.now, changed))
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM checkpoint_probe").fetchone()[0], 2)
