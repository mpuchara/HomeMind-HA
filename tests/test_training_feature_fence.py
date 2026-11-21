"""Isolation tests with real FeatureJournal writes and independent SQLite connections."""
import tempfile
import unittest
from pathlib import Path

import support
from observation_contract import FeatureJournal
from storage import Store
from training_snapshot import capture, install, protect_training_inputs, release_training_windows, validate


RADAR = 'binary_sensor.kitchen_presence_moving_target'


class TrainingFeatureFenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(str(Path(directory.name) / 'live.db'))
        self.owner = self.create_agent('switch.target')
        self.clock = 100000.0
        self.journal = FeatureJournal(self.store, clock=lambda: self.clock)

    def record(self, key, timestamp=20.0, *, received=25.0, quality=0.5, entity=RADAR):
        self.journal.record(entity, support.state(entity, 'on'), event_key=key,
                            event_time=timestamp, received_time=received,
                            source='ha_event', quality=quality)

    def create_agent(self, target):
        return self.store.create_agent(support.agent(name=target, target_entity=target,
            input_entities=[RADAR]))['id']

    def freeze(self, *, start=10.0, end=30.0, owner=None):
        with self.store.conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            install(conn)
            return capture(conn, end, [RADAR], start_ts=start, owner=owner or self.owner)

    def check(self, boundary):
        with self.store.conn() as conn:
            validate(conn, boundary)

    def test_live_evidence_window_and_identical_dedup_do_not_invalidate_radar(self):
        self.record('frozen')
        boundary = self.freeze()
        self.journal.open_window('window', 'agent', [RADAR], 25.0, 'decision', before=10.0, after=10.0)
        self.check(boundary)
        self.record('frozen')
        self.record('late_append', 21.0)
        self.record('next', 31.0, received=31.0)
        with self.store.conn() as conn:
            self.assertGreater(conn.execute(
                'SELECT protected_until FROM feature_observation_events WHERE event_key=?',
                ('frozen',)).fetchone()[0], self.clock)
        self.check(boundary)

    def test_dedup_detects_actual_causal_receipt_or_quality_change(self):
        self.record('frozen')
        boundary = self.freeze()
        self.record('frozen', received=22.0)
        with self.assertRaisesRegex(RuntimeError, 'UPDATE inside frozen feature window'):
            self.check(boundary)
        boundary = self.freeze()
        self.record('frozen', quality=1.0)
        with self.assertRaisesRegex(RuntimeError, 'UPDATE inside frozen feature window'):
            self.check(boundary)

    def test_routine_journal_age_prune_waits_while_hard_limit_still_applies(self):
        self.record('frozen')
        boundary = self.freeze()
        with protect_training_inputs(self.store, self.owner):
            self.journal.prune(entity_id=RADAR)
            self.check(boundary)
            self.journal.global_event_limit = 0
            self.journal.prune(entity_id=RADAR)
            with self.assertRaisesRegex(RuntimeError, 'DELETE inside frozen feature window'):
                self.check(boundary)
        with self.store.conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0], 0)

    def test_global_cap_prefers_rows_outside_active_training_windows(self):
        self.record('first')
        first = self.freeze()
        self.record('second', 40.0, received=41.0)
        second_owner = self.create_agent('switch.second')
        second = self.freeze(start=35.0, end=50.0, owner=second_owner)
        self.record('stale')
        stale = self.freeze(owner=self.create_agent('switch.stale'))
        self.record('unrelated', entity='sensor.other', received=50.0)
        self.record('late_inside_second', 43.0, received=51.0)
        self.journal.global_event_limit = 2
        with protect_training_inputs(self.store, self.owner), protect_training_inputs(self.store, second_owner):
            self.journal.prune()
            self.check(first)
            self.check(second)
            with self.assertRaisesRegex(RuntimeError, 'DELETE inside frozen feature window'):
                self.check(stale)
        with self.store.conn() as conn:
            self.assertEqual({row[0] for row in conn.execute(
                'SELECT event_key FROM feature_observation_events')}, {'first', 'second'})

    def test_independent_agent_windows_and_reused_rowids(self):
        self.record('before', 8.0)
        self.record('inside')
        self.record('later', 40.0, received=41.0)
        old = self.freeze()
        later = self.freeze(start=35.0, end=50.0, owner=self.create_agent('switch.second'))
        with self.store.conn() as conn:
            conn.execute("DELETE FROM feature_observation_events WHERE event_key='before'")
            conn.execute("UPDATE feature_observation_events SET source='other' WHERE event_key='later'")
        self.check(old)
        with self.assertRaisesRegex(RuntimeError, 'UPDATE inside frozen feature window'):
            self.check(later)
        with self.store.conn() as conn:
            # Reuse a vacant key below the captured high-water with an in-window event.
            conn.execute("""INSERT INTO feature_observation_events
                (rowid,event_key,contract_version,entity_id,event_time,received_time,source,quality)
                VALUES(1,'reused',12,?,11,12,'fixture',1)""", (RADAR,))
        with self.assertRaisesRegex(RuntimeError, 'INSERT inside frozen feature window'):
            self.check(old)

    def test_move_into_window_and_retire_only_matching_generation(self):
        self.record('before', 8.0)
        old = self.freeze()
        new = self.freeze()
        with self.assertRaisesRegex(RuntimeError, 'window superseded'):
            self.check(old)
        with self.store.conn() as conn:
            release_training_windows(conn, old)
        self.check(new)
        with self.store.conn() as conn:
            conn.execute("UPDATE feature_observation_events SET event_time=10 WHERE event_key='before'")
        with self.assertRaisesRegex(RuntimeError, 'UPDATE inside frozen feature window'):
            self.check(new)
