"""Real HTTP regression: two interrupted agents, blocked Store and restart.

Imports the shipped entrypoint and uses real SQLite lifecycle state/HistoryManager.
Recorder/replay work is held at the indexing boundary; no HA service is called.
"""
import json,sys,tempfile,threading,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import support
import trial_queue_main,history,queue_main
from storage import Store
from training_queue import TrainingQueue
from runtime_http import ExplicitRouteRegistry,install_dispatch
from candidate_http_routes import register_routes as candidate_routes
from test_training_queue import FakeEngine


def until(predicate, seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.01)
    raise AssertionError('Timed out waiting for test state')


def main():
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'interrupted.db';first=Store(path);saved={};aids=[]
        for i in range(2):
            aid=first.create_agent(support.agent(mode='paused',target_entity=f'light.interrupted_{i}',input_entities=['binary_sensor.room']))['id'];aids.append(aid)
            first.save_model(aid,{'version':11,'dims':128,'schema':{'version':12,'feature_contract_version':4,'entities':['binary_sensor.room']},'saved_weights':[i,2,3]})
            first.set_training_state(aid,'training',score=.91,samples=1259)
            first.set_training_progress(aid,100,160,200)
            saved[aid]=(first.get_agent_config(aid),first.get_model(aid))
        store=Store(path);engine=FakeEngine([])
        engine.lock=threading.RLock();engine.state_map={};engine.entity_registry={};engine.models={}
        engine.executor.target_lock=lambda target:threading.RLock()
        release=threading.Event();started=set();blocked_candidates={}
        def index(session,aid,**kwargs):
            started.add(aid)
            assert release.wait(10),'test did not release indexing worker'
        with patch.object(history,'STORE',store),patch.dict(history.OPTIONS,manual_agent_training=True),patch.object(history.HistoryManager,'_run_agent_indexing',index):
            manager=history.HistoryManager(engine);manager.effective_training_slots=lambda:2
            for aid in aids:
                current=store.get_agent_config(aid)
                assert current['training_state']=='paused' and current['mode']=='paused',current
                for key in ('training_cursor_ts','training_progress','benchmark_score','benchmark_samples'):
                    assert current[key]==saved[aid][0][key],key
                assert store.get_model(aid)==saved[aid][1]
            queue=TrainingQueue(manager,store,engine,poll_seconds=.02)
            core=queue_main.core;core.STORE=store;core.ENGINE=engine;core.HISTORY=manager;core.AUTOMATION_KNOWLEDGE=object()
            core.set_startup('ready',7,'ready',ready=True)
            queue_main.TRAINING_QUEUE=queue;core.TRAINING_QUEUE=queue
            registry=ExplicitRouteRegistry();queue_main.register_mutation_routes(registry)
            candidate_routes(registry,core,SimpleNamespace(_candidate_row=lambda aid:blocked_candidates.get(aid),status=lambda aid:{'root_agent_id':aid}))
            server=ThreadingHTTPServer(('127.0.0.1',0),core.Handler)
            core.HTTP_SERVER=server
            install_dispatch(core,registry)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def call(path,body=None):
                request=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                try:
                    with urlopen(request,timeout=2) as response:return response.status,json.load(response)
                except HTTPError as error:return error.code,json.load(error)
            try:
                receipts=[];maximum_ms=0.
                with store.lock:
                    for i,aid in enumerate(aids):
                        stamp=time.perf_counter();status,ticket=call('/api/agents/'+aid+'/training-requests',dict(request_id=f'interrupted-{i}-request',action='resume'))
                        elapsed=(time.perf_counter()-stamp)*1000;maximum_ms=max(maximum_ms,elapsed)
                        assert status==202 and ticket['state']=='pending',(status,ticket)
                        assert elapsed<500,elapsed
                        receipts.append(ticket)
                    first_id=receipts[0]['request_id']
                    until(lambda:queue.training_admission.get(first_id)['phase']=='waiting_state')
                    for ticket in receipts:
                        status,view=call('/api/training-requests/'+ticket['request_id'])
                        assert status==200 and view['state']=='pending',(status,view)
                    assert not queue.jobs and not manager.agent_jobs
                    status,duplicate=call('/api/agents/'+aids[0]+'/training-requests',dict(request_id=first_id,action='resume'))
                    assert status==202 and duplicate['request_id']==first_id
                    status,conflict=call('/api/agents/'+aids[0]+'/training-requests',dict(request_id='different-rebuild-request',action='rebuild'))
                    assert status==409,(status,conflict)
                for ticket in receipts:
                    view=until(lambda:(row if (row:=queue.training_admission.get(ticket['request_id']))['state']!='pending' else None))
                    assert view['state']=='accepted' and view['response_status']==202,view
                assert len(queue.jobs)==2,len(queue.jobs)
                for aid in aids:
                    assert store.get_model(aid)==saved[aid][1]
                    assert store.get_agent_config(aid)['training_cursor_ts']==160
                queue.start();until(lambda:len(started)==2)
                assert len(manager.agent_jobs)==2
                queue.stop();release.set();queue.join(2);until(lambda:not manager.agent_jobs)
                # A new queue/runtime must not mistake an old browser ticket for a
                # running job or automatically restart a destructive operation.
                store2=Store(path);history.STORE=store2
                manager2=history.HistoryManager(engine)
                queue2=TrainingQueue(manager2,store2,engine)
                core.STORE=store2;core.HISTORY=manager2;core.TRAINING_QUEUE=queue2;queue_main.TRAINING_QUEUE=queue2
                status,missing=call('/api/training-requests/'+first_id)
                assert status==404 and missing['boot_id']!=receipts[0]['boot_id'],(status,missing)
                assert not queue2.jobs
                status,new=call('/api/agents/'+aids[0]+'/training-requests',dict(request_id=first_id,action='resume'))
                assert status==202
                result=until(lambda:(row if (row:=queue2.training_admission.get(first_id))['state']!='pending' else None))
                assert result['state']=='accepted',result
                assert store2.get_model(aids[0])==saved[aids[0]][1]
                # The worker must dispatch the existing Candidate learning guard,
                # not bypass it through a newly added async transport.
                blocked_candidates[aids[1]]={'id':'candidate-fixture'}
                status,guard=call('/api/agents/'+aids[1]+'/training-requests',dict(request_id='guarded-rebuild-request',action='rebuild'))
                assert status==202
                guarded=until(lambda:(row if (row:=queue2.training_admission.get(guard['request_id']))['state']!='pending' else None))
                assert guarded['state']=='failed' and guarded['response_status']==409,guarded
                blocked_candidates.clear()
                status,rebuild=call('/api/agents/'+aids[1]+'/training-requests',dict(request_id='allowed-rebuild-request',action='rebuild'))
                rebuilt=until(lambda:(row if (row:=queue2.training_admission.get(rebuild['request_id']))['state']!='pending' else None))
                assert rebuilt['state']=='accepted' and rebuilt['response']['rebuild'] is True,rebuilt
                assert store2.get_model(aids[1])==saved[aids[1]][1]
                assert store2.get_agent_config(aids[1])['training_cursor_ts']==160
                queue2.stop()
                print(json.dumps(dict(real_http=True,shipped_entrypoint=True,two_interrupted_models_preserved=True,
                      receipt_under_held_store_lock_ms=round(maximum_ms,3),status_reads_independent_of_store=True,
                      two_workers_started=True,restart_requires_new_confirmation=True,candidate_guard_preserved=True,
                      resume_and_rebuild_admitted=True,ram_receipt_distinct_from_queue_acceptance=True)))
            finally:
                release.set();queue.stop();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
