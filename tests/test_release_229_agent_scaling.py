"""Shared causal inputs, foreground discovery isolation and bounded busy recovery."""
import copy
from contextlib import ExitStack
import pickle
from pathlib import Path
import subprocess
import sys
import sqlite3
import threading
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace

import support
import test_context_tournament_shadow as fixture
import test_release_217_writer_turns as writer_fixture
from context import TemporalHistory
from sqlite_background import background_sqlite


class AgentScalingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ContextTournamentShadowTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.e,self.service=self.f.engine,self.f.service
        self.e.models[self.f.agent['id']]=self.f.policy
        self.e._inference_tls=threading.local();self.e._inference_tls.state_revision=12
        self.e.stop_event=threading.Event();self.q=self.service._deferred_observations
        self.q.thread=SimpleNamespace(is_alive=lambda:False,join=lambda *a:None)
        self.addCleanup(lambda:setattr(self.q,'thread',None))

    def seed(self):
        self.e.temporal_history=TemporalHistory()
        for i in range(48):
            eid='sensor.shared_'+str(i)
            st=support.state(eid,str(i),description='x'*512)
            self.e.state_map[eid]=st
            for age in range(32):self.e.temporal_history.add(eid,100.+age,dict(st,attributes=dict(st['attributes'])))
        self.e.runtime[self.f.agent['id']]={'last_inference_ts':200.,'last_prediction':0.}

    def test_five_devices_share_history_without_false_capacity_gap(self):
        self.seed();agents=[]
        for i in range(5):
            a=dict(self.f.agent,id='root'+str(i),target_entity='light.device'+str(i));agents.append(a)
            self.e.state_map[a['target_entity']]=support.state(a['target_entity'],'off')
            self.e.models[a['id']]=SimpleNamespace(schema=SimpleNamespace(entities=[self.f.active]))
            self.e.runtime[a['id']]={'last_inference_ts':200.,'last_prediction':0.}
        job=self.q._capture(agents[0],self.e.state_map,set(),None)
        self.q.max_bytes=int(job['charge']*2.5)
        with patch.object(self.q,'_sample_memory'):
            for a in agents:self.q.submit(a,self.e.state_map,{a['target_entity']},None)
        self.assertEqual(len(self.q.pending),5)
        self.assertEqual(self.q.stats['dropped'],0)
        self.assertGreater(self.q.snapshot()['shared_snapshot_references'],self.q.snapshot()['shared_snapshot_records'])
        self.assertLessEqual(self.q.used_bytes,self.q.max_bytes)
        self.assertLess(self.q.used_bytes,sum(j['charge'] for j in self.q.pending)*.6)
        for job in list(self.q.pending):self.q._release(job)
        self.q.pending.clear()
        self.assertEqual(self.q.used_bytes,0);self.assertEqual(self.q.shared_parts,{})

    def test_active_frame_remains_charged_when_pending_peer_is_dropped(self):
        self.seed();a=self.f.agent
        first=self.q._capture(a,self.e.state_map,set(),None);second=self.q._capture(a,self.e.state_map,set(),None)
        with self.q.lock:
            self.q._retain(first);one=self.q.used_bytes
            self.q._retain(second);self.q.pending.append(second)
            self.q._gap(a['id'])
            self.assertEqual(self.q.used_bytes,one)
            self.assertTrue(self.q.shared_parts)
            self.q._release(first)
        self.assertEqual(self.q.used_bytes,0);self.assertEqual(self.q.shared_parts,{})

    def test_nested_replacement_keeps_private_causal_snapshot_and_accounts_new_record(self):
        self.seed();first=self.q._capture(self.f.agent,self.e.state_map,set(),None)
        eid='sensor.shared_0';self.e.state_map[eid]['attributes']['description']='changed'
        second=self.q._capture(self.f.agent,self.e.state_map,set(),None)
        self.assertEqual(first['states'][eid]['attributes']['description'],'x'*512)
        self.assertEqual(second['states'][eid]['attributes']['description'],'changed')
        self.q._retain(first);before=self.q.used_bytes;extra=self.q._additional_charge(second)
        self.q._retain(second);self.assertEqual(self.q.used_bytes,before+extra)
        self.q._release(first);self.q._release(second);self.assertEqual(self.q.used_bytes,0)

    def test_zero_budget_still_rejects_and_invalidates_optional_observation(self):
        self.seed();self.q.max_bytes=0
        self.q.submit(self.f.agent,self.e.state_map,{self.f.target},None)
        self.assertFalse(self.q.pending);self.assertEqual(self.q.used_bytes,0)
        self.assertIn(self.f.agent['id'],self.q.gaps)

    def test_root_policy_lookup_does_not_synchronize_discovery_under_writer_lock(self):
        done=threading.Event();errors=[]
        self.service._runtime_fingerprints.clear()
        def policy():
            try:
                self.e._inference_tls.state_revision=2
                with patch.object(self.f.store,'conn',side_effect=AssertionError('SQL before Root decision')):
                    self.assertIs(self.e.policy(self.f.agent),self.f.policy)
            except Exception as exc:errors.append(exc)
            finally:done.set()
        with self.f.store.lock:
            t=threading.Thread(target=policy);t.start();self.assertTrue(done.wait(.5))
        t.join(1.);self.assertEqual(errors,[])

    def test_failed_discovery_sync_retries_and_source_identity_reuses_success(self):
        self.service._runtime_fingerprints.clear()
        clone=copy.deepcopy(self.f.policy);clone._tournament_source_identity=(id(self.f.policy),'epoch',())
        original=self.service.sync_agent
        with patch.object(self.service,'sync_agent',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):self.service.sync_if_needed(self.f.agent,clone)
        self.assertNotIn(self.f.agent['id'],self.service._runtime_fingerprints)
        self.service.sync_if_needed(self.f.agent,clone)
        with patch.object(self.service,'sync_agent',side_effect=AssertionError('Repeated sync')):
            self.service.sync_if_needed(self.f.agent,self.f.policy)

    def test_busy_cooldown_avoids_capture_storm_and_keeps_explicit_gaps(self):
        self.service._observation_storage_retry.defer(sqlite3.OperationalError('database is locked'))
        with patch.object(self.q,'_capture',side_effect=AssertionError('Capture while busy')):
            for _ in range(20):self.q.submit(self.f.agent,self.f.states,{self.f.target},None)
        self.assertEqual(self.q.stats['busy_skips'],20);self.assertFalse(self.q.pending)
        self.assertIn(self.f.agent['id'],self.q.gaps)
        self.service._observation_storage_retry.next_attempt=0.
        self.q.submit(self.f.agent,self.f.states,{self.f.target},None)
        self.assertEqual(len(self.q.pending),1)
        self.q._gap(self.f.agent['id']);self.assertEqual(self.q.used_bytes,0)

    def test_real_sqlite_writer_failure_sets_retry_without_error_warning(self):
        blocker=sqlite3.connect(self.f.store.path);self.addCleanup(blocker.close)
        blocker.execute('BEGIN IMMEDIATE')
        self.service._runtime_fingerprints.clear()
        original=self.service.observe_shadow
        def observe(*args,**kwargs):
            original(*args,**kwargs)
            return self.service.sync_agent(self.f.agent,policy=self.f.policy)
        self.service.observe_shadow=observe
        with patch.object(self.service,'report_runtime_error') as report:
            # The final worker's exception path is exercised with a real native
            # SQLITE_BUSY. Explicit direct observation still returns failure.
            result=self.service._execute_observation(self.f.agent,self.f.states,{self.f.target},None)
        self.assertFalse(result);self.assertGreater(self.service._observation_storage_retry.delay(),0)
        report.assert_not_called();blocker.rollback()


class TrainingWriterPriorityTests(unittest.TestCase):
    def test_admission_arriving_between_chunks_retains_remaining_rows_and_yields(self):
        f=writer_fixture.WriterTurnTests();f.setUp();self.addCleanup(f.doCleanups)
        f.buffer.submit([('a',str(i),i) for i in range(7)])
        priority=f.store.training_admission_priority();entered=[]
        def request(_seconds):
            if not entered:priority.__enter__();entered.append(True)
        try:
            with background_sqlite(f.store),patch('context_row_buffer.time.sleep',side_effect=request):
                with self.assertRaises(sqlite3.OperationalError):f.buffer.flush()
            self.assertEqual(f.buffer.snapshot()['flushed'],2)
            self.assertEqual(f.buffer.snapshot()['pending'],5)
        finally:
            if entered:priority.__exit__(None,None,None)
        self.assertEqual(f.buffer.flush(),5)
        self.assertEqual(len(f.rows()),7);self.assertEqual(f.buffer.snapshot()['bytes'],0)

    def test_shared_sensor_is_protected_once_while_all_five_windows_remain(self):
        from observation_contract import FeatureJournal
        f=writer_fixture.WriterTurnTests();f.setUp();self.addCleanup(f.doCleanups)
        j=FeatureJournal(f.store);entities=['sensor.shared']+['light.'+str(i) for i in range(5)]
        j.record_batch([dict(entity_id=eid,state=support.state(eid,'1'),event_time=float(t),
                            received_time=float(t),source='test') for eid in entities for t in range(4)])
        def run(legacy):
            with f.store.conn() as c:
                c.execute('DELETE FROM feature_window_entities');c.execute('DELETE FROM feature_windows')
                c.execute('UPDATE feature_observation_events SET protected_until=0')
            clock=iter(1000.+i for i in range(5));j.clock=lambda:next(clock)
            windows=[dict(window_id='w'+str(i),agent_id='a'+str(i),entities=[entities[0],entities[i+1]],
                          anchor_time=2.,before=2.,after=2.) for i in range(5)]
            with f.store.connection_session() as c:
                before=c.total_changes
                with patch('observation_contract.sqlite3.sqlite_version_info',(3,32,0) if legacy else (3,45,1)):
                    j.open_windows_batch(windows)
                changes=c.total_changes-before
                rows=[tuple(r) for r in c.execute('SELECT entity_id,event_time,protected_until FROM feature_observation_events ORDER BY entity_id,event_time')]
                self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_windows').fetchone()[0],5)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_window_entities').fetchone()[0],10)
            return rows,changes
        modern,changes=run(False);legacy,_=run(True)
        self.assertEqual(modern,legacy)
        self.assertEqual(changes,5+10+len(entities)*4)

    def test_multiple_g0_devices_complete_real_scheduler_and_never_call_ha(self):
        result=subprocess.run([sys.executable,'tools/benchmark_agent_scaling.py','--agents','5','--events','3','--entities','64'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30.)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
