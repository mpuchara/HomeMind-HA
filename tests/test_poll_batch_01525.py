"""Coalesce only safe REST receipt writes; preserve the full native evidence."""
from copy import deepcopy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import observation_contract as observation
from context import TemporalHistory
from sqlite_background import DeferredBatchError
from storage import Store
from training_snapshot import protect_training_inputs


class PollBatchTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name)/"data.db")
        self.addCleanup(observation.install_training_contract()['restore'])
        agent = support.agent(id='a')
        states = {f'sensor.poll_{i:03d}': dict(support.state(f'sensor.poll_{i:03d}', str(i)),
                  last_updated='2026-11-01T00:00:00+00:00') for i in range(128)}
        engine = SimpleNamespace(stop_event=threading.Event(), lock=threading.RLock(),
            models={}, agent_index_at=1., agent_configs={'a': agent},
            temporal_history=TemporalHistory(), on_state_changed=lambda *_: None,
            refresh_states=lambda: states, process_agent=lambda *_: None,
            executor=SimpleNamespace(submit=Mock(return_value='accepted')))
        with patch('observation_contract.threading.Thread'), patch('observation_contract._migrate_models', return_value=[]):
            observation.install(SimpleNamespace(STORE=store, ENGINE=engine))
        engine.models['a'] = SimpleNamespace(schema=SimpleNamespace(entities=list(states)))
        return store, engine, states

    def poll(self, engine, stamp):
        with patch.object(observation, 'now_ts', return_value=stamp):
            engine.refresh_states()

    def turn(self, engine):
        with patch('universal_history_collector.foreground_idle', return_value=True), \
             patch.dict(observation.OPTIONS, universal_training_collector_enabled=False):
            return engine.flush_feature_journal(max_batches=1)['observations']

    def rows(self, store):
        with store.conn() as c:
            return [tuple(r) for r in c.execute('SELECT * FROM feature_observation_events ORDER BY event_key')]

    def seed_window(self, store, stamp, event_time):
        with store.conn() as c:
            c.execute("INSERT INTO feature_windows VALUES('w',12,'a',?,?,?,'test',?,?)",
                      (event_time, event_time-1, event_time+1, stamp, stamp+3600))
            c.execute("INSERT INTO feature_window_entities VALUES('w','sensor.poll_000')")

    def test_exact_native_receipts_and_prune_target_against_two_old_turns(self):
        store, engine, states = self.fixture()
        stamp = time.time()
        event_time = observation.parse_ts(states['sensor.poll_000']['last_updated'])
        self.seed_window(store, stamp, event_time)
        self.poll(engine, stamp)
        journal = engine.feature_journal
        with patch.object(journal, 'record_batch', wraps=journal.record_batch) as writes, \
             patch.object(journal, 'prune', wraps=journal.prune) as prune:
            self.assertEqual(self.turn(engine), 128)
            self.assertEqual(writes.call_count, 1)
            self.assertEqual(prune.call_count, 1)
            target = prune.call_args.kwargs
            records = deepcopy(writes.call_args.args[0])
        actual = self.rows(store)
        self.assertEqual({row['entity_id'] for row in records}, set(states))
        self.assertTrue(all(row['received_time'] == stamp and row['event_time'] == event_time
                            for row in records))
        with store.conn() as c:
            self.assertEqual(c.execute("SELECT protected_until FROM feature_observation_events "
                                       "WHERE entity_id='sensor.poll_000'").fetchone()[0], stamp+3600)
        self.assertEqual(len(actual), 128)
        self.assertEqual(engine.feature_observation_deferred_snapshot()['observations']['expanded_poll_rows'], 128)
        # Compare with the same FIFO order and two 64-row writes (including
        # native protection and retention). Separate database; no schema change.
        with tempfile.TemporaryDirectory() as root:
            other = Store(Path(root)/'oracle.db')
            oracle = observation.FeatureJournal(other)
            self.seed_window(other, stamp, event_time)
            with patch.object(oracle, 'prune', wraps=oracle.prune) as prune:
                oracle.record_batch(records[:64])
                oracle.record_batch(records[64:])
                self.assertEqual(prune.call_count, 1)
                self.assertEqual(prune.call_args.kwargs, target)
            self.assertEqual(self.rows(other), actual)
        # A later receipt of the SAME event is independent, not deduplicated.
        self.poll(engine, stamp+5)
        self.assertEqual(self.turn(engine), 128)
        with store.conn() as c:
            counts = c.execute('SELECT received_time,count(*) FROM feature_observation_events GROUP BY received_time ORDER BY received_time').fetchall()
        self.assertEqual([tuple(r) for r in counts], [(stamp,128),(stamp+5,128)])

    def test_training_and_pending_window_keep_old_turn_size(self):
        store, engine, _ = self.fixture()
        self.poll(engine, time.time())
        with protect_training_inputs(store, 'a'):
            self.assertEqual(self.turn(engine), 64)
        engine.executor.submit(SimpleNamespace(agent_id='a', intent_id='i', created_at=time.time()))
        self.poll(engine, time.time()+1)
        self.assertEqual(self.turn(engine), 64)
        self.assertEqual(engine.feature_observation_deferred_snapshot()['observations'].get('expanded_poll_rows',0), 0)

    def test_recent_foreground_and_optional_collector_keep_old_turn_size(self):
        store, engine, _ = self.fixture()
        self.poll(engine, time.time())
        with patch('universal_history_collector.foreground_idle', return_value=False):
            self.assertEqual(engine.flush_feature_journal(max_batches=1)['observations'], 64)
        engine.universal_training_collector = object()
        self.poll(engine, time.time()+1)
        self.assertEqual(self.turn(engine), 64)

    def test_prune_boundary_and_mixed_poll_times_keep_old_turn_size(self):
        store, engine, states = self.fixture()
        journal = engine.feature_journal
        journal._writes = 64
        stamp = time.time()
        self.poll(engine, stamp)
        self.assertEqual(self.turn(engine), 64)
        self.poll(engine, stamp+1)
        self.assertEqual(self.turn(engine), 64)
        self.assertEqual(engine.feature_observation_deferred_snapshot()['observations'].get('expanded_poll_rows',0), 0)

    def test_native_rollback_restores_entire_fifo_then_retry(self):
        store, engine, _ = self.fixture()
        self.poll(engine, time.time())
        with store.conn() as c:
            c.execute("CREATE TRIGGER reject_poll BEFORE INSERT ON feature_observation_events BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        with self.assertRaises(DeferredBatchError):
            self.turn(engine)
        self.assertEqual(len(self.rows(store)), 0)
        self.assertEqual(engine.feature_observation_deferred_snapshot()['observations']['pending'],128)
        with store.conn() as c:
            c.execute('DROP TRIGGER reject_poll')
        self.assertEqual(self.turn(engine),128)
        self.assertEqual(len(self.rows(store)),128)

    def test_explicit_drain_and_large_payload_keep_existing_contract(self):
        store, engine, states = self.fixture()
        for state in states.values():
            state['attributes']['friendly_name'] = 'x'*2048
        self.poll(engine, time.time())
        self.assertEqual(self.turn(engine),64)
        self.assertEqual(engine.flush_feature_journal()['observations'],64)
        self.assertEqual(len(self.rows(store)),128)
        self.assertEqual(engine.feature_observation_deferred_snapshot()['observations'].get('expanded_poll_rows',0),0)
