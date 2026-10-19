"""Recorder RAM staging, durable row parity and storage-retry hysteresis."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import history
from settings import DEFAULT_OPTIONS
from storage import Store
from storage_budget import StoragePressure, _payload_bytes, run_storage_write
from training_preparation import preparation_profile, preparation_memory
from training_process import training_options_fingerprint
from training_queue import TrainingQueue
from test_training_queue import FakeStore, FakeEngine, FakeHistory
from tools.benchmark_history_import import payload, legacy_rows, canonical


class Preparation220Tests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        with patch.dict('storage.OPTIONS', storage_budget_mode='manual'):
            self.store = Store(Path(scratch.name)/'archive.db')
        self.manager = object.__new__(history.HistoryManager)
        self.manager.stop_event = threading.Event()
        self.manager.job_cancel_event = threading.Event()
        self.manager.training_job_agent_id = 'a'
        self.manager.training_preparation_profile = preparation_profile(DEFAULT_OPTIONS,
            {'total_mb': 7806, 'available_mb': 5600}, 320)
        self.manager.set_status = Mock()
        p = patch.object(history, 'STORE', self.store);p.start();self.addCleanup(p.stop)
        p = patch.object(history.TRAINING_BUDGET, 'checkpoint', return_value=0);p.start();self.addCleanup(p.stop)

    def test_headroom_and_custom_limits(self):
        large = self.manager.training_preparation_profile
        self.assertEqual(large['memory_limit_mb'], 1536)
        self.assertEqual(large['batch_rows'], 1024)
        low = preparation_profile(DEFAULT_OPTIONS, {'total_mb': 1024, 'available_mb': 500}, 200)
        self.assertEqual(low['batch_rows'], 128)
        self.assertLessEqual(low['memory_limit_mb'], 500)
        unknown = preparation_profile(DEFAULT_OPTIONS, {}, 200)
        self.assertEqual((unknown['memory_limit_mb'], unknown['batch_rows']), (500, 128))
        custom = preparation_profile(dict(DEFAULT_OPTIONS, training_preparation_memory_limit_mb=600,
            training_recorder_batch_rows=256), {'total_mb': 7806, 'available_mb': 5600}, 320)
        self.assertEqual((custom['memory_limit_mb'], custom['batch_rows']), (600, 128))
        self.assertEqual(training_options_fingerprint(DEFAULT_OPTIONS), training_options_fingerprint(
            dict(DEFAULT_OPTIONS, training_preparation_memory_limit_mb=800, training_recorder_batch_rows=256,
                 training_recorder_capacity_wait_seconds=0)))

    def test_cgroup_cap_is_applied_to_headroom(self):
        def read(p, *a, **kw):
            if str(p)=='/sys/fs/cgroup/memory.max':return str(1024*1024*1024)
            if str(p)=='/sys/fs/cgroup/memory.current':return str(600*1024*1024)
            raise FileNotFoundError
        with patch('training_process._host_memory_mb', return_value={'total_mb':7806.,'available_mb':5600.}), \
                patch('training_preparation.Path.read_text', autospec=True, side_effect=read):
            self.assertEqual(preparation_memory(), {'total_mb':1024., 'available_mb':424.})

    def test_expanded_buffers_preserve_full_and_numeric_history(self):
        for source in ('ha_history_full', 'ha_history_minimal'):
            with self.subTest(source=source):
                data = payload(700)
                with self.store.conn() as c:c.execute('DELETE FROM entity_history')
                expected = legacy_rows(data, source)
                sizes=[];original=self.store.archive_packed_batch
                def write(rows):
                    sizes.append((len(rows),sum(_payload_bytes(r) for r in rows)))
                    return original(rows)
                with patch.object(self.store, 'archive_packed_batch', side_effect=write):
                    self.assertEqual(self.manager._archive_history_payload(data,source),len(expected))
                actual=canonical(self.store)
                with self.store.conn() as c:c.execute('DELETE FROM entity_history')
                self.store.archive_batch(expected)
                self.assertEqual([{k:v for k,v in r.items() if k!='id'} for r in actual],
                                 [{k:v for k,v in r.items() if k!='id'} for r in canonical(self.store)])
                self.assertTrue(all(n<=1024 and b<=1024*1024 for n,b in sizes))
                if source=='ha_history_full':self.assertEqual(len(sizes),3)

    def test_byte_cap_and_single_oversized_row_keep_admission(self):
        data=payload(6)
        for group in data:
            for row in group:row['attributes']={'text':'x'*200000}
        sizes=[];original=self.store.archive_packed_batch
        def write(rows):
            sizes.append(sum(_payload_bytes(r) for r in rows));return original(rows)
        with patch.object(self.store,'archive_packed_batch',side_effect=write):
            self.manager._archive_history_payload(data,'ha_history_full')
        self.assertGreater(len(sizes),1)
        self.assertLessEqual(max(sizes),1024*1024)
        huge=payload(1)[:1];huge[0][0]['attributes']={'text':'x'*(17*1024*1024)}
        with self.assertRaises(StoragePressure):self.manager._archive_history_payload(huge,'ha_history_full')

    def test_cancel_after_commit_preserves_only_committed_batch(self):
        original=self.store.archive_packed_batch
        def write(rows):
            n=original(rows);self.manager.job_cancel_event.set();return n
        with patch.object(self.store,'archive_packed_batch',side_effect=write):
            with self.assertRaises(InterruptedError):self.manager._archive_history_payload(payload(700),'ha_history_full')
        self.assertEqual(self.store.archive_count(),1024)

    def test_capacity_wait_retains_same_batch_and_resumes_only_below_low_watermark(self):
        b=self.store.storage_budget;b.db_limit=10000;b.wal_limit=10000;b.reserve=10
        stop=Mock();stop.is_set.return_value=False
        levels=iter([{'level':'paused','reason':'database_budget','database_used_bytes':9000,'wal_bytes':0,'free_bytes':100},
                     {'level':'normal','reason':None,'database_used_bytes':8990,'wal_bytes':0,'free_bytes':100},
                     {'level':'normal','reason':None,'database_used_bytes':8400,'wal_bytes':0,'free_bytes':100}])
        error=StoragePressure('database_budget');error.reason='database_budget'
        cb=Mock(side_effect=[error,7])
        def outside_lock(data):
            self.assertTrue(self.store.lock.acquire(blocking=False));self.store.lock.release()
        waiting=Mock(side_effect=outside_lock)
        with patch.object(b,'sample',side_effect=lambda:next(levels)):
            self.assertEqual(run_storage_write(self.store,cb,stop,capacity_timeout=30,on_wait=waiting),7)
        self.assertEqual(cb.call_count,2)
        self.assertEqual(waiting.call_count,2)
        self.assertEqual(stop.wait.call_count,2)

    def test_rollback_below_pause_threshold_still_waits_for_real_headroom(self):
        b=self.store.storage_budget;b.db_limit=10000;b.wal_limit=10000;b.reserve=10
        stop=Mock();stop.is_set.return_value=False
        data=dict(level='normal',reason=None,database_used_bytes=8990,wal_bytes=0,free_bytes=100)
        error=StoragePressure('database_budget');error.reason='database_budget'
        cb=Mock(side_effect=[error,1])
        stop.wait.side_effect=lambda seconds:data.update(database_used_bytes=8400)
        with patch.object(b,'sample',side_effect=lambda:dict(data)):
            self.assertEqual(run_storage_write(self.store,cb,stop,capacity_timeout=30),1)
        stop.wait.assert_called_once_with(1.)
        self.assertEqual(cb.call_count,2)

    def test_packed_batch_rolls_back_revision_and_rows_after_denial(self):
        b=self.store.storage_budget
        # Deny after INSERT execution through the ordinary admission guard.
        original=b.check;seen=[]
        def deny(*args,**kw):
            if any(sql.startswith('INSERT INTO entity_history') for sql in seen):
                raise b.pressure('database_budget')
            return original(*args,**kw)
        packed=[self.store.pack_archive_row(('sensor.a',1,'on',{},None,'ha_history_full'))]
        with self.store.connection_session() as connection:
            connection.set_trace_callback(seen.append)
            with patch.object(b,'check',side_effect=deny):
                with self.assertRaises(StoragePressure):self.store.archive_packed_batch(packed)
            self.assertFalse(connection.in_transaction)
            connection.set_trace_callback(None)
        self.assertEqual(self.store.archive_count(),0)
        with self.store.conn() as c:self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_capacity_wait_cancel_timeout_and_disk_failure_never_retry_write(self):
        b=self.store.storage_budget;b.db_limit=10000;b.wal_limit=10000;b.reserve=10
        data=dict(level='paused',reason='database_budget',database_used_bytes=9000,wal_bytes=0,free_bytes=100)
        error=StoragePressure('database_budget');error.reason='database_budget'
        for mode in ('cancel','timeout','disk'):
            with self.subTest(mode=mode):
                stop=Mock();stop.is_set.return_value=False;cancel=threading.Event();cb=Mock(side_effect=error)
                stop.wait.side_effect=lambda seconds:cancel.set()
                row=dict(data)
                if mode=='disk':row.update(free_bytes=0,reason='disk_reserve')
                ticks=iter([0.,0.,40.,40.,40.]) if mode=='timeout' else None
                with patch.object(b,'sample',return_value=row), \
                        patch('storage_budget.time.monotonic',side_effect=(lambda:next(ticks)) if ticks else (lambda:100.)):
                    with self.assertRaises(InterruptedError if mode=='cancel' else StoragePressure):
                        run_storage_write(self.store,cb,stop,cancel,capacity_timeout=30)
                self.assertEqual(cb.call_count,1)

    def test_storage_failure_does_not_publish_recorder_coverage(self):
        m=self.manager;m.training_recorder_coverage={};m.training_recorder_coverage_hits=0;m.training_recorder_coverage_misses=0
        m.recorder_skipped_slices=0;m.progress=0
        m._persist_training_recorder_coverage=Mock()
        error=StoragePressure('database_budget');error.reason='database_budget'
        with patch.object(m,'_import_section',side_effect=error):
            with self.assertRaises(StoragePressure):m._training_recorder_import(['sensor.a'],1,100,
                batch_size=1,minimal=False,no_attributes=False,source='ha_history_full',label='test',max_hours=6)
        self.assertEqual(m.training_recorder_coverage,{})
        m._persist_training_recorder_coverage.assert_not_called()

    def test_queue_hysteresis_does_not_restart_at_89_percent(self):
        store=FakeStore();trace=[];h=FakeHistory(store,trace);q=TrainingQueue(h,store,FakeEngine(trace))
        q.enqueue('a',rebuild=True);q.storage_retry_reason='database_budget';q.storage_retry_after=5.
        state=dict(level='normal',database_used_bytes=8990,database_max_bytes=10000,wal_bytes=0,wal_max_bytes=10000)
        with patch('training_queue.storage_status',return_value=state),patch('training_queue.time.monotonic',return_value=10.):
            self.assertFalse(q._try_start_head());self.assertFalse(q._try_start_head())
            self.assertEqual(h.started,[])
            state['database_used_bytes']=8400
            self.assertTrue(q._try_start_head())
        h.complete('a');q._finish_active_if_done()
        self.assertEqual(h.started,[('a',True)])


if __name__=='__main__':unittest.main()
