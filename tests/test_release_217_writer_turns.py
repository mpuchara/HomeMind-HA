"""Yield optional writes without losing committed rows, retries or budget guards."""
from contextlib import closing
from pathlib import Path
import sqlite3
import random
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from context_row_buffer import ContextRowBuffer
from observation_contract import FeatureJournal
from storage import Store
from storage_budget import StoragePressure


class WriterTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'turns.db')
        with self.store.conn() as c:
            c.execute('CREATE TABLE micro(owner TEXT,key TEXT,value INTEGER,PRIMARY KEY(owner,key))')
        self.buffer=ContextRowBuffer(self.store,'micro','INSERT OR REPLACE INTO micro VALUES(?,?,?)',batch_rows=2)

    def rows(self):
        with self.store.conn() as c:return [tuple(r) for r in c.execute('SELECT * FROM micro ORDER BY key')]

    def test_store_is_released_between_committed_microbatches(self):
        self.buffer.submit([('a',str(i),i) for i in range(7)])
        visible=[]
        def other_owner(_seconds):
            def read():
                acquired=self.store.lock.acquire(blocking=False)
                if acquired:
                    try:visible.append(len(self.rows()))
                    finally:self.store.lock.release()
            thread=threading.Thread(target=read);thread.start();thread.join(3)
            self.assertFalse(thread.is_alive())
        with patch('context_row_buffer.time.sleep',side_effect=other_owner):
            self.assertEqual(self.buffer.flush(),7)
        self.assertEqual(visible,[2,4,6])
        self.assertEqual(self.buffer.snapshot()['commits'],4)
        self.assertEqual(self.buffer.snapshot()['bytes'],0)

    def test_failed_chunk_rolls_back_only_uncommitted_rows_and_retry_keeps_newest(self):
        self.buffer.submit([('a',str(i),i) for i in range(7)])
        with self.store.conn() as c:
            c.execute("""CREATE TRIGGER reject_micro BEFORE INSERT ON micro
                WHEN NEW.key='3' BEGIN SELECT RAISE(ABORT,'test chunk failure'); END""")
        submitted=[]
        def replace_pending(_seconds):
            if not submitted:
                self.buffer.submit([('a','4',100)]);submitted.append(True)
        with patch('context_row_buffer.time.sleep',side_effect=replace_pending):
            with self.assertRaisesRegex(sqlite3.IntegrityError,'chunk failure'):
                self.buffer.flush()
        self.assertEqual(self.rows(),[('a','0',0),('a','1',1)])
        state=self.buffer.snapshot()
        self.assertEqual(state['flushed'],2);self.assertEqual(state['inflight'],0)
        self.assertEqual(state['pending'],5);self.assertGreater(state['bytes'],0)
        with self.store.conn() as c:c.execute('DROP TRIGGER reject_micro')
        self.assertEqual(self.buffer.flush(),5)
        self.assertEqual(self.rows(),[('a',str(i),100 if i==4 else i) for i in range(7)])
        self.assertEqual(self.buffer.snapshot()['bytes'],0)
        self.assertEqual(self.buffer.snapshot()['flushed'],7)

    def test_each_chunk_keeps_fresh_entry_exit_commit_and_cached_row_checks(self):
        self.buffer.submit([('a',str(i),i) for i in range(3)])
        with patch.object(self.store.storage_budget,'check',wraps=self.store.storage_budget.check) as check:
            self.buffer.flush()
        cached=[c for c in check.call_args_list if c.kwargs.get('refresh') is False]
        fresh=[c for c in check.call_args_list if c.kwargs.get('refresh',True)]
        self.assertGreater(len(cached),0)
        self.assertGreaterEqual(len(fresh),6)


class JournalAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'journal.db')
        self.journal=FeatureJournal(self.store,clock=lambda:100.)

    def test_background_turn_is_bounded_and_explicit_flush_drains_remainder(self):
        import test_release_196_deferred_contention as contention
        helper = contention.DeferredContention196Tests()
        helper.store = self.store
        self.addCleanup(helper.doCleanups)
        engine = helper.journal_fixture()
        for i in range(270):
            engine.executor.submit(SimpleNamespace(agent_id='a', intent_id='bounded'+str(i), created_at=time.time()))
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'], 271)
        self.assertEqual(engine.flush_feature_journal(max_batches=1), {'observations': 1, 'windows': 128})
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'], 143)
        self.assertEqual(engine.flush_feature_journal(), {'observations': 0, 'windows': 143})
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'], 0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_windows').fetchone()[0], 271)

    def test_saturated_optional_window_queue_never_delays_executor_on_store(self):
        import test_release_196_deferred_contention as contention
        helper = contention.DeferredContention196Tests()
        helper.store = self.store
        self.addCleanup(helper.doCleanups)
        engine = helper.journal_fixture()
        for i in range(4095):
            engine.executor.submit(SimpleNamespace(agent_id='a', intent_id='full'+str(i), created_at=time.time()))
        locked, release, completed = threading.Event(), threading.Event(), threading.Event()
        results=[]
        def hold_store():
            with self.store.lock:
                locked.set()
                release.wait(5)
        def submit():
            try:results.append(engine.executor.submit(SimpleNamespace(agent_id='a',intent_id='gap',created_at=time.time())))
            finally:completed.set()
        holder=threading.Thread(target=hold_store);holder.start()
        self.assertTrue(locked.wait(1))
        worker=threading.Thread(target=submit);worker.start()
        try:self.assertTrue(completed.wait(1),'Optional journal overflow blocked Executor')
        finally:
            release.set();holder.join(2);worker.join(2)
        self.assertEqual(results,['accepted'])
        snapshot=engine.feature_window_deferred_snapshot()
        self.assertEqual(snapshot['windows']['queued'],4096)
        self.assertEqual(snapshot['windows']['pending'],4096)
        self.assertEqual(snapshot['windows']['overflow_sync'],0)
        self.assertEqual(snapshot['windows']['overflow_gaps'],1)
        self.assertEqual(snapshot['observations']['pending'],1)

    def test_manual_correction_window_keeps_durable_fallback_at_capacity(self):
        import test_release_196_deferred_contention as contention
        helper=contention.DeferredContention196Tests();helper.store=self.store
        self.addCleanup(helper.doCleanups)
        engine=helper.journal_fixture()
        for i in range(4095):
            engine.executor.submit(SimpleNamespace(agent_id='a',intent_id='full'+str(i),created_at=time.time()))
        cfg=engine.agent_configs['a']
        engine._provenance_latest_events={cfg['target_entity']:(time.time(),'manual-correction','user')}
        engine.process_agent(cfg,{}, {cfg['target_entity']})
        snapshot=engine.feature_window_deferred_snapshot()['windows']
        self.assertEqual(snapshot['overflow_gaps'],0)
        self.assertEqual(snapshot['overflow_sync'],1)
        self.assertEqual(snapshot['pending'],4096)
        with self.store.conn() as c:
            row=c.execute("SELECT kind FROM feature_windows WHERE window_id='correction:manual-correction:a'").fetchone()
        self.assertEqual(row[0],'correction')

    def test_grouped_window_protection_matches_legacy_pointwise_max(self):
        entities = ['sensor.a', 'sensor.b', 'sensor.c']
        events = [(entity, float(t)) for entity in entities for t in range(61)]
        self.journal.record_batch([dict(entity_id=entity,
            state=support.state(entity, t), event_time=t, received_time=t, source='test')
            for entity, t in events])
        rng = random.Random(217)
        windows = []
        expected = {key: 0. for key in events}
        # Include disjoint ranges, inclusive boundaries, different entity sets,
        # duplicate IDs with different intervals, and expiry order reversals.
        stamps = [100. + rng.randrange(100) for _ in range(600)]
        for i in range(600):
            selected = entities[:2] if i < 300 else entities[:1+rng.randrange(3)]
            anchor = float(rng.randrange(61))
            before, after = float(rng.randrange(5)), float(rng.randrange(5))
            windows.append(dict(window_id='w'+str(i%400), agent_id='a', entities=selected,
                anchor_time=anchor, before=before, after=after))
            for entity, t in events:
                if entity in selected and anchor-before <= t <= anchor+after:
                    expected[(entity,t)] = max(expected[(entity,t)], stamps[i]+7*86400.)
        clock = iter(stamps)
        self.journal.clock = lambda: next(clock)
        self.journal.max_windows_per_agent = 1000
        with self.store.connection_session() as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            self.assertEqual(self.journal.open_windows_batch(windows), 600)
        with self.store.conn() as c:
            actual = {(r[0],r[1]):r[2] for r in c.execute(
                'SELECT entity_id,event_time,protected_until FROM feature_observation_events')}
        self.assertEqual(actual, expected)

    def test_record_batch_exit_pressure_rolls_back_and_resets_admission_scope(self):
        original=self.store.storage_budget.check;seen=[]
        def pressure(parameters=(),*,refresh=True):
            if refresh and len(seen)>=2:raise StoragePressure('test batch exit pressure')
            result=original(parameters,refresh=refresh)
            if not refresh:seen.append(True)
            return result
        row=dict(entity_id='sensor.a',state=support.state('sensor.a',1),event_time=10.,received_time=10.,source='test')
        with patch.object(self.store.storage_budget,'check',side_effect=pressure):
            with self.assertRaises(StoragePressure):self.journal.record_batch([row])
        with self.store.conn() as c:
            self.assertEqual(c.storage_batch_depth,0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0],0)

    def test_bulk_records_after_windows_keep_per_sample_protection_and_missing_entities(self):
        rng = random.Random(218)
        windows = []
        for i in range(400):
            windows.append(dict(window_id='w'+str(i), agent_id='a',
                entities=['sensor.a'] if i % 2 else ['sensor.a', 'sensor.b'],
                anchor_time=float(rng.randrange(60)), before=float(rng.randrange(5)),
                after=float(rng.randrange(5))))
        stamps = [100.+rng.randrange(100) for _ in windows]
        clock = iter(stamps)
        self.journal.clock = lambda: next(clock)
        self.journal.open_windows_batch(windows)
        self.journal.clock = lambda: 100.
        records = [dict(entity_id=entity, state=support.state(entity, t),
                        event_time=float(t), received_time=float(t), source='test')
                   for entity in ['sensor.a', 'sensor.b', 'sensor.missing'] for t in range(61)]
        with self.store.connection_session() as c:
            c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            self.journal.record_batch(records)
        with self.store.conn() as c:
            actual = {(r[0], r[1]): r[2] for r in c.execute(
                'SELECT entity_id,event_time,protected_until FROM feature_observation_events')}
        for row in records:
            entity, stamp = row['entity_id'], row['event_time']
            expected = max([expiry+7*86400. for window, expiry in zip(windows, stamps)
                            if entity in window['entities'] and
                            window['anchor_time']-window['before'] <= stamp <=
                            window['anchor_time']+window['after']] or [0.])
            self.assertEqual(actual[(entity, stamp)], expected)

    def test_common_maximum_coverage_and_higher_partial_expiry_are_exact(self):
        self.journal.clock = lambda: 1000.
        self.journal.open_window('wide', 'a', ['sensor.a', 'sensor.b'], 3., 'test', before=2., after=2.)
        self.journal.clock = lambda: 100.
        self.journal.open_window('lower', 'a', ['sensor.b'], 4., 'test', before=0., after=0.)
        self.journal.clock = lambda: 2000.
        self.journal.open_window('higher', 'a', ['sensor.a'], 4., 'test', before=0., after=0.)
        self.journal.record_batch([dict(entity_id=entity, state=support.state(entity, stamp),
            event_time=stamp, received_time=stamp, source='test')
            for entity in ['sensor.a', 'sensor.b', 'sensor.none'] for stamp in [2., 3., 4.]])
        with self.store.conn() as c:
            actual = {(r[0], r[1]): r[2] for r in c.execute(
                'SELECT entity_id,event_time,protected_until FROM feature_observation_events')}
        for entity, stamp in actual:
            expected = 0. if entity == 'sensor.none' else (
                2000. if entity == 'sensor.a' and stamp == 4. else 1000.) + 7*86400.
            self.assertEqual(actual[(entity, stamp)], expected)

    def test_bulk_record_preserves_wal_admission_before_writer_reservation(self):
        self.store._wal_reclaiming.set()
        self.addCleanup(self.store._wal_reclaiming.clear)
        row=dict(entity_id='sensor.a',state=support.state('sensor.a',1),event_time=10.,received_time=10.,source='test')
        with self.store.background_sqlite(), self.assertRaises(sqlite3.OperationalError) as caught:
            self.journal.record_batch([row])
        self.assertEqual(caught.exception.sqlite_errorcode & 0xff, sqlite3.SQLITE_BUSY)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0],0)

    def test_protection_snapshot_is_reserved_before_bulk_insert(self):
        import observation_contract as observation
        self.journal.open_window('existing','a',['sensor.a'],10.,'test')
        original=observation._insert_journal_rows
        attempted=[]
        def insert(connection,prefix,rows,width,suffix=''):
            if 'feature_observation_events' in prefix:
                with closing(sqlite3.connect(self.store.path,timeout=0)) as external:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        external.execute("DELETE FROM feature_windows WHERE window_id='existing'")
                    self.assertEqual(caught.exception.sqlite_errorcode & 0xff,sqlite3.SQLITE_BUSY)
                attempted.append(True)
            return original(connection,prefix,rows,width,suffix)
        row=dict(entity_id='sensor.a',state=support.state('sensor.a',1),event_time=10.,received_time=10.,source='test')
        with patch.object(observation,'_insert_journal_rows',side_effect=insert):
            self.journal.record_batch([row])
        self.assertEqual(attempted,[True])
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT protected_until FROM feature_observation_events').fetchone()[0],100.+7*86400.)

    def test_window_batch_exit_pressure_is_atomic_including_entity_links(self):
        original=self.store.storage_budget.check;seen=[]
        def pressure(parameters=(),*,refresh=True):
            if refresh and len(seen)>=2:raise StoragePressure('test window exit pressure')
            result=original(parameters,refresh=refresh)
            if not refresh:seen.append(True)
            return result
        with patch.object(self.store.storage_budget,'check',side_effect=pressure):
            with self.assertRaises(StoragePressure):
                self.journal.open_windows_batch([dict(window_id='w',agent_id='a',entities=['sensor.a'],anchor_time=10.)])
        with self.store.conn() as c:
            self.assertEqual(c.storage_batch_depth,0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_windows').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_window_entities').fetchone()[0],0)


if __name__=='__main__':unittest.main()
