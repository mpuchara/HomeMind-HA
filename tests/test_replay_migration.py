import unittest
import time
import tracemalloc
import sqlite3
from unittest.mock import patch
from support import *
from storage import Store
from context_engine import ContextEngine
from replay import SQLiteTemporalTracker, DeferredUpdates
from policy import MultiHorizonPolicy
from context import archived_state
from settings import DEFAULT_OPTIONS
from telemetry import HeavyJobGate, HEAVY_JOBS
import history as history_module
from history import HistoryManager
from home_bootstrap import HomeBootstrap
from engine import Engine


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'archive.db')
        self.base=time.time()-100000

    def tearDown(self):self.temp.cleanup()

    def add(self,eid,ts,value):
        self.store.archive_batch([(eid,ts,str(value),{},None,'test')])

    def test_incompatible_migration_keeps_all_data_and_pauses(self):
        a=self.store.create_agent(agent())
        self.store.save_model(a['id'],{'version':9,'schema':{'version':10}})
        self.store.add_feedback(a['id'],1,1,1,'manual',{0:1},'human')
        self.add('light.kitchen',self.base,'on')
        self.store.set_training_state(a['id'],'qualified');self.store.update_agent(a['id'],{'mode':'control'})
        self.store.migrate_models();self.store.migrate_models()
        migrated=self.store.get_agent(a['id'])
        self.assertEqual(migrated['training_state'],'needs_retrain');self.assertEqual(migrated['mode'],'paused')
        self.assertEqual(migrated['feedback_count'],1);self.assertEqual(self.store.archive_count(),1)
        self.assertEqual(self.store.get_model(a['id'])['version'],9)
        with self.store.conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM model_backups').fetchone()[0],1)

    def test_compatible_model_is_kept(self):
        a=self.store.create_agent(agent());p=MultiHorizonPolicy(a,{}, {},set())
        self.store.save_model(a['id'],p.export());self.store.set_training_state(a['id'],'qualified')
        self.store.migrate_models()
        self.assertEqual(self.store.get_agent(a['id'])['training_state'],'qualified')

    def test_manual_rebuild_keeps_user_feedback(self):
        a=self.store.create_agent(agent());self.store.add_feedback(a['id'],1,1,1,'manual',{0:1},'u')
        self.store.clear_learning(a['id'])
        self.assertEqual(self.store.get_agent(a['id'])['feedback_count'],1)

    def test_new_agent_waiting(self):
        a=self.store.create_agent(agent())
        self.assertEqual(a['training_state'],'waiting');self.assertEqual(a['mode'],'paused')

    def test_context_edit_waits_for_manual_retrain(self):
        a=self.store.create_agent(agent())
        changed=self.store.update_agent(a['id'],{'input_entities':['binary_sensor.new_motion']})
        self.assertEqual(changed['training_state'],'needs_retrain')
        self.assertEqual(changed['mode'],'paused')

    def test_uncommitted_experiences_removed_after_interruption(self):
        a=self.store.create_agent(agent())
        self.store.add_historical_experience(a['id'],1,1,1,1,10,{0:1})
        self.store.save_model(a['id'],{'version':10,'schema':{'version':11}})
        self.store.add_historical_experience(a['id'],2,1,1,1,10,{0:1})
        self.store.discard_uncommitted_experiences(a['id'])
        rows=self.store.list_historical_experiences(a['id'])
        self.assertEqual([r['target_history_id'] for r in rows],[1])

    def test_import_windows_are_lazy(self):
        windows=HistoryManager._time_windows(0,3650*86400,max_hours=1)
        self.assertIs(iter(windows),windows)
        self.assertEqual(next(windows),(3650*86400-3600,3650*86400))

    def test_bootstrap_cancellation_keeps_live_model(self):
        import engine as em
        with patch.object(history_module,'STORE',self.store),patch.object(em,'STORE',self.store):
            e=Engine();eid='binary_sensor.motion'
            e.state_map={eid:state(eid,'off')};e.context.configure(e.state_map,entities={eid:{'area_id':'room'}})
            self.add(eid,self.base,'on')
            h=HistoryManager(e);b=HomeBootstrap(e.context,h,self.store)
            before=e.context.home.export()
            with patch.object(b,'_check',side_effect=InterruptedError('cancelled')):
                self.assertTrue(b.start(import_recorder=False));b.thread.join(timeout=5)
            self.assertEqual(b.status['state'],'CANCELLED',b.status)
            self.assertEqual(e.context.home.export(),before)
            self.assertIsNone(HEAVY_JOBS.owner)
            e.control_workers.shutdown();e.poll_worker.shutdown()

    def test_asof_rewind_no_future(self):
        eid='binary_sensor.motion'
        self.add(eid,self.base,'off');self.add(eid,self.base+10,'on');self.add(eid,self.base+20,'off')
        c=ContextEngine(DEFAULT_OPTIONS);c.configure({eid:state(eid,'off',device_class='motion')})
        t=SQLiteTemporalTracker(self.store,[eid],c,self.base,self.base+50)
        t.advance(self.base+15);self.assertEqual(t.state_map[eid]['state'],'on')
        t.advance(self.base+5);self.assertEqual(t.state_map[eid]['state'],'off')
        t.close()

    def test_temporal_queries_are_bounded(self):
        eid='sensor.room_temperature'
        self.store.archive_batch([(eid,self.base+i,str(i%10),{},None,'test') for i in range(10000)])
        c=ContextEngine(DEFAULT_OPTIONS);c.configure({eid:state(eid,20)})
        t=SQLiteTemporalTracker(self.store,[eid],c,self.base,self.base+10000)
        t.advance(self.base+9999)
        self.assertLessEqual(len(t.history.samples[eid]),64);t.close()

    def test_streaming_archive_memory(self):
        for batch in range(20):
            self.store.archive_batch([('sensor.x',self.base+batch*1000+i,str(i),{},None,'test') for i in range(1000)])
        tracemalloc.start();count=0
        for row in self.store.archive_iter(chunk_size=256):count+=1
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        self.assertEqual(count,20000);self.assertLess(peak,3*1024*1024)

    def test_deferred_validation_vectors_stay_on_disk(self):
        p=MultiHorizonPolicy(agent(),{}, {},set())
        spool=DeferredUpdates({'test':p})
        tracemalloc.start()
        for i in range(5000):spool.append((p,1,1,{j:.5 for j in range(128)},1,self.base+i))
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        self.assertLess(peak,2*1024*1024)
        self.assertEqual(sum(1 for _ in spool),5000);spool.close()

    def test_shared_heavy_job_gate(self):
        gate=HeavyJobGate();self.assertTrue(gate.acquire('agent:a'))
        self.assertFalse(gate.acquire('home'));gate.release('wrong');self.assertFalse(gate.acquire('home'))
        gate.release('agent:a');self.assertTrue(gate.acquire('home'))

    def test_startup_does_not_train(self):
        e=Engine()
        with patch.object(history_module,'STORE',self.store),patch.object(HistoryManager,'train_from_archive') as train:
            h=HistoryManager(e)
            self.assertFalse(h.agent_jobs);train.assert_not_called()
        e.control_workers.shutdown();e.poll_worker.shutdown()

    def test_end_to_end_streamed_agent_replay(self):
        import engine as em
        with patch.object(history_module,'STORE',self.store),patch.object(em,'STORE',self.store):
            e=Engine();eid='binary_sensor.motion'
            e.state_map={eid:state(eid,'off',device_class='motion'),'light.kitchen':state('light.kitchen')}
            e.context.configure(e.state_map)
            a=self.store.create_agent(agent(input_entities=[eid]))
            rows=[]
            for i in range(100):
                ts=self.base+i*120
                rows.extend([(eid,ts,'on',{'device_class':'motion'},None,'test'),
                             ('light.kitchen',ts+1,'on',{},'human','test'),
                             (eid,ts+30,'off',{'device_class':'motion'},None,'test'),
                             ('light.kitchen',ts+31,'off',{},'human','test')])
            self.store.archive_batch(rows)
            h=HistoryManager(e)
            with patch.object(self.store,'archive_rows_for_entities',side_effect=AssertionError('unbounded path')):
                n=h.train_from_archive(self.base,self.base+12000,agent_ids={a['id']},benchmark=True,qualify=True)
            self.assertGreater(n,100)
            self.assertEqual(self.store.get_model(a['id'])['version'],10)
            self.assertGreater(self.store.get_agent(a['id'])['benchmark_samples'],10)
            e.control_workers.shutdown();e.poll_worker.shutdown()

    def test_bootstrap_manual_and_streamed(self):
        import engine as em
        with patch.object(history_module,'STORE',self.store),patch.object(em,'STORE',self.store):
            e=Engine();states={f'binary_sensor.{area}':state(f'binary_sensor.{area}','off',device_class='occupancy') for area in ('terrace','living','kitchen')}
            reg={eid:{'area_id':eid.split('.')[1]} for eid in states}
            e.state_map=states;e.context.configure(states,entities=reg)
            e.context.store=self.store
            rows=[]
            for i in range(40):
                ts=self.base+i*100
                for eid in states:rows.append((eid,ts,'off',{},None,'test'))
                for j,eid in enumerate(states):rows.append((eid,ts+j*2+1,'on',{},None,'test'))
            self.store.archive_batch(rows)
            h=HistoryManager(e);b=HomeBootstrap(e.context,h,self.store)
            self.assertEqual(b.status['state'],'IDLE');self.assertTrue(b.start(import_recorder=False))
            b.thread.join(timeout=10)
            self.assertEqual(b.status['state'],'READY',b.status)
            self.assertTrue(e.context.home.graph);self.assertIsNone(HEAVY_JOBS.owner)
            e.control_workers.shutdown();e.poll_worker.shutdown()
