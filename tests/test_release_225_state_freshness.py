"""Closed-panel trends and read visibility during contended durable writes."""
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import engine as engine_module
from engine import Engine
from telemetry import Telemetry
from storage import Store
from teaching import Teaching
from teach_observed_history import _recorded_rows
from agent_correct_generation_history import _current_rows
from agent_live_card_refresh import live_agent_payload


def until(predicate):
    deadline=time.perf_counter()+2
    while time.perf_counter()<deadline:
        if predicate():return
        time.sleep(.001)
    raise AssertionError('Fixture did not reach expected state')


class Freshness225Tests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name)/'fresh.db')

    def make_engine(self):
        with patch.object(engine_module,'STORE',self.store):result=Engine()
        for name in ('control_workers','poll_worker','housekeeping_worker'):
            pool=getattr(result,name,None)
            if pool is not None:self.addCleanup(lambda pool=pool:pool.shutdown(wait=True,cancel_futures=True))
        return result

    def test_sampler_runs_without_any_status_read_and_stops_with_runtime(self):
        clock=[0.];commands=queue.Queue();waiting=threading.Event()
        class Stop:
            def wait(self,seconds):
                self_outer.assertEqual(seconds,5.0);waiting.set()
                return commands.get(timeout=3)
        self_outer=self
        with patch('telemetry.time.monotonic',side_effect=lambda:clock[0]):
            telemetry=Telemetry();telemetry.observe('event_to_decision',123)
            telemetry.start_trend_sampling(Stop());thread=telemetry.trend_thread
            try:
                until(waiting.is_set)
                telemetry.start_trend_sampling(Stop());self.assertIs(telemetry.trend_thread,thread)
                for tick in range(1,131):
                    waiting.clear();clock[0]=tick*5.;commands.put(False);until(waiting.is_set)
                # No snapshot or HTTP consumer ran during these 650 seconds.
                self.assertEqual(len(telemetry.trends['event_to_decision']),121)
                self.assertEqual(telemetry.trends['event_to_decision'][-1],(650.,None,0))
                self.assertEqual(telemetry.counts['event_to_decision'],1)
                series=telemetry.snapshot()['metrics']['event_to_decision']['trend']
                self.assertEqual(len(series),121);self.assertEqual(series[0]['age_seconds'],600.)
            finally:commands.put(True);thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_live_snapshot_never_calls_index_sql_or_context_refresh(self):
        runtime=self.make_engine();a=support.agent(id='a',target_entity='light.a')
        runtime.all_agent_configs={'a':a};runtime.state_map={'light.a':support.state('light.a','on')}
        runtime.state_map['light.a']['last_updated']='2026-10-22T10:00:00+00:00'
        runtime.entity_revisions['light.a']=42;runtime.entity_state_received_ts['light.a']=1792663201.
        runtime._refresh_agent_index=Mock(side_effect=AssertionError('HTTP must not refresh the index'))
        core=SimpleNamespace(ENGINE=runtime)
        with patch.object(self.store,'conn',side_effect=AssertionError('HTTP must not read SQLite')):
            row=live_agent_payload(core,include_configs=True)['agents'][0]
        self.assertEqual(row['current_value'],1.)
        self.assertEqual(row['current_state_ts'],1792663200.)
        self.assertEqual(row['current_received_ts'],1792663201.)
        self.assertEqual(row['current_revision'],42)
        runtime._refresh_agent_index.assert_not_called()

    def test_archive_current_stays_visible_until_commit_and_on_rollback(self):
        runtime=self.make_engine();a=support.agent(id='a',target_entity='light.a')
        row=('light.a',100.,'on',{},None,'live',110.)
        runtime.pending_archive=[row];entered=threading.Event();release=threading.Event();errors=[]
        real=self.store.archive_batch
        def slow(rows):
            entered.set();self.assertTrue(release.wait(3));return real(rows)
        def flush():
            try:runtime.flush_archive()
            except Exception as exc:errors.append(exc)
        manager=SimpleNamespace(store=self.store,engine=runtime)
        with patch.object(engine_module,'STORE',self.store),patch.object(self.store,'archive_batch',side_effect=slow):
            worker=threading.Thread(target=flush);worker.start()
            try:
                self.assertTrue(entered.wait(1));self.assertEqual(runtime.pending_archive,[])
                self.assertEqual(_current_rows(manager,a,99.,101.)[0],dict(ts=100.,current=1.))
                runtime.pending_archive.append(('light.a',100.5,'off',{},None,'live',111.))
                values=_current_rows(manager,a,99.,101.)
                self.assertEqual([(x['ts'],x['current']) for x in values],[(100.,1.),(100.5,0.),(101.,0.)])
            finally:release.set();worker.join(2)
        self.assertEqual(errors,[]);self.assertEqual(runtime.archive_inflight,[])
        with patch.object(engine_module,'STORE',self.store),patch.object(self.store,'archive_batch',side_effect=RuntimeError('rollback fixture')):
            with self.assertRaises(RuntimeError):runtime.flush_archive()
        self.assertEqual(len(runtime.pending_archive),1);self.assertEqual(runtime.archive_inflight,[])

    def test_desired_stays_visible_during_sqlite_commit_and_rollback(self):
        teaching=Teaching(self.store);runtime=SimpleNamespace(teaching=teaching)
        teaching.record('a',0.,1.,100.);entered=threading.Event();release=threading.Event();errors=[]
        import sqlite_bulk_rows
        real=sqlite_bulk_rows.insert_bounded_rows
        def slow(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(3));return real(*args,**kwargs)
        def flush():
            try:teaching.flush()
            except Exception as exc:errors.append(exc)
        with patch.object(sqlite_bulk_rows,'insert_bounded_rows',side_effect=slow):
            worker=threading.Thread(target=flush);worker.start()
            try:
                self.assertTrue(entered.wait(1));self.assertEqual(list(teaching.buffer),[])
                self.assertEqual(_recorded_rows(self.store,runtime,'a',99.,101.),[dict(ts=100.,current=0.,desired=1.)])
            finally:release.set();worker.join(2)
        self.assertEqual(errors,[]);self.assertEqual(teaching.inflight,[])
        teaching.record('a',1.,0.,102.)
        with patch.object(sqlite_bulk_rows,'insert_bounded_rows',side_effect=RuntimeError('rollback fixture')):
            with self.assertRaises(RuntimeError):teaching.flush()
        self.assertEqual(list(teaching.buffer),[('a',102.,1.,0.)]);self.assertEqual(teaching.inflight,[])

    def test_fallback_has_two_second_schedule_without_overlapping_polls(self):
        runtime=self.make_engine();runtime.ws_connected=False;runtime.last_full_poll=100.
        runtime.poll_worker=Mock();runtime.poll_future=None
        runtime.poll_worker.submit.return_value=SimpleNamespace(done=lambda:False)
        with patch.object(engine_module,'now_ts',return_value=102.1),patch.object(engine_module.time,'monotonic',return_value=1000.),patch.dict(engine_module.OPTIONS,realtime_fallback_poll_seconds=2):
            self.assertTrue(runtime._maybe_schedule_state_resync())
            self.assertFalse(runtime._maybe_schedule_state_resync())
        runtime.poll_worker.submit.assert_called_once()

    def test_rest_reconciliation_notifies_before_archive_io(self):
        runtime=self.make_engine();runtime.state_map={'light.a':support.state('light.a','off')}
        before=runtime.live_card_updates.token();entered=threading.Event();release=threading.Event();errors=[]
        def archive(*args,**kwargs):entered.set();self.assertTrue(release.wait(3))
        def refresh():
            try:runtime.refresh_states()
            except Exception as exc:errors.append(exc)
        with patch.object(engine_module.HA,'states',return_value=[support.state('light.a','on')]),patch.object(runtime,'_queue_archive_state',side_effect=archive),patch.object(runtime,'flush_archive',side_effect=AssertionError('REST must not persist archive')):
            worker=threading.Thread(target=refresh);worker.start()
            try:
                self.assertTrue(entered.wait(1));self.assertNotEqual(runtime.live_card_updates.token(),before)
                self.assertEqual(runtime.state_map['light.a']['state'],'on')
                self.assertIn('light.a',runtime.entity_state_received_ts)
            finally:release.set();worker.join(2)
        self.assertEqual(errors,[])

    def test_source_event_time_is_not_shifted_to_late_receipt_or_decision(self):
        runtime=self.make_engine();st=support.state('light.a','on')
        st['last_updated']='2026-10-22T10:00:00+00:00'
        with patch.object(engine_module,'STORE',self.store),patch.object(engine_module,'now_ts',return_value=1792663210.):
            runtime.on_state_changed(dict(entity_id='light.a',new_state=st,_hm_ws_received_ts=1792663209.))
        self.assertEqual(runtime.entity_state_received_ts['light.a'],1792663209.)
        self.assertEqual(runtime.pending_archive[0][1],1792663200.)
        self.assertEqual(runtime.pending_archive[0][-1],1792663210.)

    def test_final_http_live_and_heavy_cards_use_completed_decision(self):
        script=r'''
import json,threading,time,sys
sys.path.insert(0,'tests')
from types import SimpleNamespace
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import support,trial_queue_main,queue_main
from storage import STORE
from runtime_http import ExplicitRouteRegistry,install_dispatch
from candidate_http_routes import register_routes
from agent_live_card_refresh import live_agent_payload
core=trial_queue_main.core;core.STORE=STORE;a=support.agent(id='a',target_entity='light.a')
engine=SimpleNamespace(lock=threading.RLock(),all_agent_configs={'a':a},
 state_map={'light.a':support.state('light.a','on')},entity_revisions={'light.a':2},
 runtime={'a':dict(last_prediction=0.,last_inference_ts=200.,last_confidence=.1,
   _live_decision=dict(last_prediction=1.,last_inference_ts=100.,decision_ready_ts=101.,last_confidence=.9))},
 ws_connected=True,ws_error=None,_refresh_agent_index=Mock(side_effect=AssertionError('Live must not refresh index')))
core.ENGINE=engine;core.HISTORY=None;core.AUTOMATION_KNOWLEDGE=object();core.TRAINING_QUEUE=None;queue_main.TRAINING_QUEUE=None
core.set_startup('ready',7,'fixture',ready=True)
core.live_agent_payload=lambda include_configs=False:live_agent_payload(core,include_configs=include_configs)
registry=ExplicitRouteRegistry();register_routes(registry,core,SimpleNamespace())
server=ThreadingHTTPServer(('127.0.0.1',0),core.Handler);core.HTTP_SERVER=server;install_dispatch(core,registry)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
try:
 with patch.object(core.STORE,'conn',side_effect=AssertionError('Live must not query SQLite')):
  start=time.perf_counter()
  with urlopen(f'http://127.0.0.1:{server.server_port}/api/live',timeout=2) as response:row=json.load(response)['agents'][0]
  assert row['current_value']==1. and row['last_prediction']==1. and row['decision_ready_ts']==101.,row
  elapsed=(time.perf_counter()-start)*1000;assert elapsed<500,elapsed
 engine._refresh_agent_index.assert_not_called();engine._refresh_agent_index=lambda:None
 row=queue_main._agent_payloads(None)[0]['runtime']
 assert row['last_prediction']==1. and row['last_confidence']==.9 and row['decision_ready_ts']==101.,row
 print(json.dumps(dict(real_http=True,shipped_entrypoint=True,live_without_index_or_sql=True,completed_decision_in_both_reads=True,host_receipt_ms=round(elapsed,3))))
finally:server.shutdown();server.server_close();worker.join(2)
'''
        result=subprocess.run([sys.executable,'-c',script],cwd=support.ROOT,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_only_previous_fallback_default_migrates(self):
        import settings
        path=Path(self.store.path).with_name('options.json')
        with patch.object(settings,'OPTIONS_PATH',path):
            path.write_text('{"realtime_fallback_poll_seconds":10}')
            self.assertEqual(settings.load_options()['realtime_fallback_poll_seconds'],2)
            path.write_text('{"realtime_fallback_poll_seconds":6}')
            self.assertEqual(settings.load_options()['realtime_fallback_poll_seconds'],6)
            self.assertEqual(settings.load_options()['realtime_resync_seconds'],900)

    def test_delayed_ui_reply_does_not_rewind_current_or_desired(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('adaptive_ai/src/static/manual_feedback.js','utf8');
const helper=source.slice(source.indexOf('  const mergeLive='),source.indexOf('  window.applyLiveValues='));
const c={};vm.createContext(c);vm.runInContext(helper+'\nthis.merge=mergeLive;',c);
const fresh={current_value:1,current_observed_ts:120,current_state_ts:110,last_prediction:1,last_inference_ts:119,decision_ready_ts:119,last_confidence:.9};
const late={current_value:0,current_observed_ts:100,current_state_ts:90,last_prediction:0,last_inference_ts:99,decision_ready_ts:99,last_confidence:.1};
let next=c.merge(fresh,late);assert.equal(next.current_value,1);assert.equal(next.last_prediction,1);assert.equal(next.last_confidence,.9);
next=c.merge(late,fresh);assert.equal(next.current_value,1);assert.equal(next.last_prediction,1);
next=c.merge(fresh,{...late,decision_ready_ts:125,last_prediction:0});assert.equal(next.current_value,1);assert.equal(next.last_prediction,0);
next=c.merge(fresh,{...late,current_observed_ts:130,current_value:0});assert.equal(next.current_value,0);assert.equal(next.last_prediction,1);
const candidate=fs.readFileSync('adaptive_ai/src/static/candidate_preference_ui.js','utf8');
const constants=candidate.slice(candidate.indexOf('  const latestByRef='),candidate.indexOf('  const cardRef='));
const merge=candidate.slice(candidate.indexOf('  function mergeCandidate('),candidate.indexOf('  function decorate(c)'));
vm.runInContext(constants+'\n'+merge+'\nthis.mergeCandidate=mergeCandidate;',c);
c.mergeCandidate({generation_id:'g1',shadow_current:1,shadow_timestamp:119,candidate_desired:1,parent_shadow_timestamp:118,parent_desired:1,live_snapshot_ts:120},{live:true});
next=c.mergeCandidate({generation_id:'g1',shadow_current:0,shadow_timestamp:99,candidate_desired:0,parent_shadow_timestamp:98,parent_desired:0,live_snapshot_ts:100},{live:true});
assert.equal(next.shadow_current,1);assert.equal(next.candidate_desired,1);assert.equal(next.parent_desired,1);
next=c.mergeCandidate({generation_id:'g1',shadow_current:0,shadow_timestamp:125,candidate_desired:0,parent_shadow_timestamp:130,parent_desired:0});
assert.equal(next.shadow_current,1);assert.equal(next.candidate_desired,0);assert.equal(next.parent_desired,0);
console.log(JSON.stringify({late_current_rejected:true,late_desired_rejected:true,independent_streams:true,candidate_rich_current_is_not_physical_snapshot:true}));
'''
        result=subprocess.run(['node','-e',script],cwd=support.ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
