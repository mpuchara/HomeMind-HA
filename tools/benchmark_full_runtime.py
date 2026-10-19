"""Boot the shipped entrypoint composition with real policies and a local HA transport.

No Root prediction/feature/scheduler/observer is replaced. Only external HA transport,
the listening address and benchmark configuration are controlled. Never contacts HA.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import http.server
import hashlib
import importlib
import json
import os
from pathlib import Path
import queue
import platform
import sys
import tempfile
import threading
import time
from urllib.request import urlopen
from urllib.parse import urlencode
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]



def join_runtime_producers(core, timeout=10.):
    """Own fixture threads through directory cleanup, after shipped shutdown.

    main() signals Engine/History but need not join their Thread objects before
    returning. A final loop turn may still open the test database. Never race
    TemporaryDirectory cleanup with those producers, or suppress cleanup errors.
    """
    for name in ('ENGINE', 'HISTORY', 'EVENT_STREAM'):
        worker = getattr(core, name, None)
        if isinstance(worker, threading.Thread) and worker.is_alive():
            worker.join(timeout)
            assert not worker.is_alive(), f'Fixture producer {name} did not stop after shipped shutdown'

def one(source, seconds=30, active=3, configured=20, mode='shadow', ui=False, trace=False, candidates=False, warmup=5., training_fixture='production'):
    with tempfile.TemporaryDirectory(prefix='full-runtime-') as directory, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        os.environ.pop('SUPERVISOR_TOKEN', None)
        os.environ.pop('HA_TOKEN', None)
        # Match Dockerfile before importing NumPy through the shipped entrypoint.
        # Host BLAS defaults can otherwise dominate the fixture and expire Control
        # intents. Keep the real TTL/qualification/freshness guards authoritative.
        for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
            os.environ[name]='1'
        sys.path.insert(0, str(source/'adaptive_ai/src'))
        import trial_queue_main as entry
        core = entry.core
        from settings import OPTIONS
        OPTIONS.update(automation_scan_enabled=False, process_nice=0)
        import storage, ha, engine, policy
        from observation_contract import install_training_contract
        install_training_contract()
        store = storage.STORE
        states, registry = {}, []
        def state(eid, value, **attrs):
            stamp = datetime.now(timezone.utc).isoformat()
            return dict(entity_id=eid,state=str(value),attributes=attrs,
                        last_changed=stamp,last_updated=stamp,context={})
        for i in range(configured):
            for eid,value,attrs in ((f'light.bench_{i}','off',{}),
                    (f'binary_sensor.bench_{i}','off',dict(device_class='occupancy')),
                    (f'sensor.bench_{i}_move_energy','0',dict(unit_of_measurement='%'))):
                states[eid] = state(eid,value,**attrs)
                registry.append(dict(entity_id=eid,area_id=f'area_{i%9}',device_id=eid,disabled_by=None))
        for i in range(778-len(states)):
            eid=f'sensor.extra_{i}';states[eid]=state(eid,i%100,unit_of_measurement='%')
            registry.append(dict(entity_id=eid,area_id=f'area_{i%9}',device_id=eid,disabled_by=None))
        roots=[]
        for i in range(configured):
            agent=store.create_agent(dict(name=f'Bench {i}',target_entity=f'light.bench_{i}',target_property='power',
                min_value=0,max_value=1,deadband=.5,input_entities=[f'binary_sensor.bench_{i}',f'sensor.bench_{i}_move_energy'],
                mode='paused',confidence_threshold=.5,action_interval=0,micro_exploration=False))
            if i < active:
                model=policy.MultiHorizonPolicy(agent,states,{r['entity_id']:r for r in registry},set())
                for j in range(80):
                    value=j%2
                    if training_fixture=='legacy':
                        # Preserve the original one-second functional Control
                        # smoke's prepared model, independently of the richer
                        # feature-training fixture used for performance runs.
                        features={0:1.,5:1. if value else -1.}
                    else:
                        training_states=dict(states)
                        eid=f'binary_sensor.bench_{i}'
                        training_states[eid]=state(eid,'on' if value else 'off',device_class='occupancy')
                        eid=f'sensor.bench_{i}_move_energy'
                        training_states[eid]=state(eid,(j*34)%100,unit_of_measurement='%')
                        features,_,_=model.features(training_states,None,at_ts=time.time())
                    for head in model.heads.values():
                        head.update(value,features,1.)
                        head.validate(value,features,1.)
                store.save_model(agent['id'],model.serialize())
                detail=dict(balanced=True,counts=dict(samples=80,correct=80,per_action={
                    '0':dict(samples=40,correct=40),'1':dict(samples=40,correct=40)}))
                store.set_training_state(agent['id'],'qualified',score=1.,samples=80,detail=detail)
                store.update_agent(agent['id'],dict(mode=mode))
                roots.append(store.get_agent_config(agent['id']))
                if mode=='control':
                    from control import set_review_approval
                    set_review_approval(store,roots[-1],states[agent['target_entity']],True)
        candidate_ids=[]
        if candidates:
            from agent_candidates import ensure_tables
            from agent_candidate_conservative_correct import _ensure_gate_column
            from agent_candidate_lineage import ensure_lineage_tables, _ensure_root, _register_generation
            from agent_candidate_shadow_runtime import ensure_shadow_tables
            ensure_tables(store);_ensure_gate_column(store)
            ensure_lineage_tables(store);ensure_shadow_tables(store)
            for root in roots:
                child=store.create_agent({**root,'id':None,'name':root['name']+' Candidate','mode':'paused'})
                candidate_ids.append(child['id'])
                store.save_model(child['id'],store.get_model(root['id']))
                store.set_training_state(child['id'],'qualified',score=1.,samples=80,detail=detail)
                parent=_ensure_root(store,root['id'])
                _register_generation(store,root['id'],parent,child['id'],1,'candidate_correct',state='offline_blocked')
                now=time.time()
                with store.lock,store.conn() as c:
                    c.execute('''INSERT INTO agent_candidates
                        (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                         dirty,queued_ts,comparison_json,offline_gate_json,updated_ts)
                        VALUES(?,?,1,'offline_blocked','candidate_correct',0,0,0,?,'{}',?,?)''',
                        (root['id'],child['id'],now,json.dumps(dict(passed=False,status='failed',
                         reasons=['synthetic benchmark passive blocked generation'])),now))
        messages=queue.Queue();services=[]
        class Socket:
            def __enter__(self): messages.put(dict(type='auth_ok'));return self
            def __exit__(self,*args): return False
            def send(self,raw):
                msg=json.loads(raw);kind=msg['type'];result=[]
                if kind=='config/entity_registry/list':result=registry
                elif kind=='config/area_registry/list':result=[dict(area_id=f'area_{i}',name=f'Area {i}') for i in range(9)]
                messages.put(dict(type='result',id=msg['id'],success=True,result=result))
            def recv(self,timeout=5):
                try:return json.dumps(messages.get(timeout=min(.1,timeout)))
                except queue.Empty:raise TimeoutError()
        def emit(eid,new):
            states[eid]=new
            messages.put(dict(type='event',id=2,event=dict(data=dict(entity_id=eid,new_state=new))))
        def request(method,path,payload=None,timeout=10):
            if path=='states':return copy.deepcopy(list(states.values()))
            if path.startswith('services/'):
                eid=payload['entity_id'];new=state(eid,'off' if path.endswith('turn_off') else 'on')
                new['context']={'id':f'bench-command-{len(services)}'}
                services.append(dict(path=path,entity_id=eid));emit(eid,new)
                return [new]
            return [] if path.startswith(('history/','config/')) else {}
        stack.enter_context(patch.object(engine,'ws_connect',lambda *a,**kw:Socket()))
        stack.enter_context(patch.object(ha.HA,'request',request))
        request_lock=threading.Lock();request_threads=set()
        server_diagnostics=dict(errors=0,last_errors=[],request_drain_seconds=None)
        class BenchmarkHTTPServer(http.server.ThreadingHTTPServer):
            # Keep the shipped request behavior. Own the fixture's request
            # lifetime so timed-out clients cannot print from daemon handlers
            # during interpreter finalization after the JSON report is written.
            def process_request_thread(self,*args):
                thread=threading.current_thread()
                with request_lock:request_threads.add(thread)
                try:return super().process_request_thread(*args)
                finally:
                    with request_lock:request_threads.discard(thread)
            def handle_error(self,*args):
                with request_lock:
                    server_diagnostics['errors']+=1
                    if len(server_diagnostics['last_errors'])<8:
                        server_diagnostics['last_errors'].append(repr(sys.exception())[:1000])
        server_type=BenchmarkHTTPServer
        stack.enter_context(patch.object(core,'ThreadingHTTPServer',lambda address,handler:server_type(('127.0.0.1',0),handler)))
        from telemetry import RUNTIME_DEBUG, TELEMETRY
        RUNTIME_DEBUG.set_enabled(trace,clear=True)
        started=time.perf_counter()
        server_thread=threading.Thread(target=core.main,name='benchmark-shipped-main',daemon=True)
        server_thread.start()
        ui_stop=threading.Event();ui_stats=dict(requests=0,errors=0,routes={});ui_lock=threading.Lock();ui_threads=[]
        try:
            deadline=time.monotonic()+45
            while not core.startup_snapshot()['ready']:
                assert time.monotonic()<deadline,core.startup_snapshot()
                assert not core.startup_snapshot().get('error'),core.startup_snapshot()
                time.sleep(.01)
            ready_ms=(time.perf_counter()-started)*1000
            runtime=core.ENGINE
            deadline=time.monotonic()+60
            while any(not runtime.runtime.get(a['id'],{}).get('_live_decision') for a in roots):
                assert time.monotonic()<deadline,(runtime.error,runtime.runtime)
                time.sleep(.01)
            time.sleep(max(0.,warmup))
            warmup_pipelines_drained=all(future.done() for future in tuple(runtime.in_flight.values()))
            base_url=f'http://127.0.0.1:{core.HTTP_SERVER.server_port}/'
            def read_ui(path,timeout=12):
                key=path.split('?')[0]
                try:
                    with urlopen(base_url+path,timeout=timeout) as response:data=json.load(response)
                    with ui_lock:
                        ui_stats['requests']+=1
                        stats=ui_stats['routes'].setdefault(key,dict(requests=0,errors=0))
                        stats['requests']+=1
                    return data
                except Exception:
                    with ui_lock:
                        ui_stats['errors']+=1
                        stats=ui_stats['routes'].setdefault(key,dict(requests=0,errors=0))
                        stats['errors']+=1
                    return None
            def poll_full():
                # app.js and candidate_ui.js: bounded concurrent full reads at 4s.
                with ThreadPoolExecutor(max_workers=4) as clients:
                    while not ui_stop.is_set():
                        futures=[clients.submit(read_ui,path,3.5 if path=='api/status' else 12)
                            for path in ('api/status','api/agents','api/events?limit=60','api/candidates')]
                        for future in futures:future.result()
                        ui_stop.wait(4.)
            def poll_live():
                revision=None
                while not ui_stop.is_set():
                    query='?'+urlencode(dict(after=revision,wait_ms=1500)) if revision else ''
                    data=read_ui('api/live'+query,2.5)
                    if isinstance(data,dict):revision=data.get('revision')
                    ui_stop.wait(.1)
            def poll_candidate_live():
                while not ui_stop.is_set():
                    read_ui('api/candidate-live',2.5);ui_stop.wait(.1)
            if ui:
                for fn in (poll_full,poll_live,poll_candidate_live):
                    thread=threading.Thread(target=fn,daemon=True);thread.start();ui_threads.append(thread)
            samples={};tail_samples={};phase=['steady'];sample_lock=threading.Lock()
            reads={}
            for module_name,name in (
                    ('agent_candidate_user_promotion','_active_observation_row'),
                    ('agent_explore','_table_exists'),('agent_explore','_active_free_for_live'),
                    ('agent_candidate_blocked_shadow_evidence','_active_comparison_edge'),
                    ('episode_evaluator_runtime','_candidate_edge')):
                module=importlib.import_module(module_name);original=getattr(module,name)
                def measured_read(*args,_fn=original,_name=module_name+'.'+name,**kwargs):
                    kind='target_worker' if threading.current_thread().name.startswith('device-control') else 'other'
                    with sample_lock:
                        if phase[0]=='steady':
                            key=_name+':'+kind;reads[key]=reads.get(key,0)+1
                    return _fn(*args,**kwargs)
                stack.enter_context(patch.object(module,name,measured_read))
            original_observe=TELEMETRY.observe
            def observe(name,value,*args,**kwargs):
                result=original_observe(name,value,*args,**kwargs)
                with sample_lock:
                    if phase[0]!='closed':
                        bucket=samples if phase[0]=='steady' else tail_samples
                        values=bucket.setdefault(name,[])
                        if len(values)<100000:values.append(float(value))
                return result
            # Passive timing collection only. No model, feature or scheduler override.
            stack.enter_context(patch.object(TELEMETRY,'observe',observe))
            start=time.monotonic();cpu=time.process_time();sent=0;digest=hashlib.sha256()
            lag_sum=lag_max=missed=0.;ticks=max(1,round(seconds*20));queue_series=[]
            for tick in range(ticks):
                due=start+tick/20
                time.sleep(max(0.,due-time.monotonic()))
                lag=max(0.,time.monotonic()-due)*1000
                lag_sum+=lag;lag_max=max(lag_max,lag);missed+=int(lag>50.)
                if tick%20==0 and len(queue_series)<1000:
                    journal=runtime.feature_observation_deferred_snapshot()
                    candidate=runtime.agent_candidates.candidate_shadow_async_diagnostics()
                    evidence=runtime.agent_candidates.candidate_shadow_persistence_snapshot()
                    sqlite_status=store.sqlite_snapshot()
                    checkpoint=sqlite_status.get('wal_checkpoint') or {}
                    queue_series.append(dict(seconds=time.monotonic()-start,
                        raw_pending=journal['observations']['pending'],windows_pending=journal['windows']['pending'],
                        retention_gaps=journal['windows'].get('overflow_gaps',0),
                        candidate_pending=candidate['queue_depth'],candidate_oldest_ms=candidate.get('oldest_root_wait_ms'),
                        candidate_processed=candidate['processed'],candidate_gaps=candidate['observation_gaps'],
                        evidence_pending=evidence['pending'],evidence_bytes=evidence['bytes'],
                        wal_allocated_bytes=checkpoint.get('wal_bytes',0),
                        wal_log_frames=checkpoint.get('log_frames',0),
                        wal_remaining_frames=checkpoint.get('remaining_frames',0),
                        wal_reclaim_successes=checkpoint.get('reclaim_successes',0)))
                i=tick%active;binary='on' if (tick//active)%2 else 'off';move=(tick*34)%100
                digest.update(json.dumps((tick,i,binary,move)).encode())
                eid=f'binary_sensor.bench_{i}'
                emit(eid,state(eid,binary,device_class='occupancy'))
                eid=f'sensor.bench_{i}_move_energy';emit(eid,state(eid,move,unit_of_measurement='%'))
                sent+=2
            time.sleep(max(0.,start+seconds-time.monotonic()))
            elapsed=time.monotonic()-start;used_cpu=time.process_time()-cpu
            with sample_lock:phase[0]='tail'
            tail_started=time.monotonic()
            with runtime.lock:
                backlog_at_input_stop=dict(inflight=sum(not f.done() for f in runtime.in_flight.values()),
                    pending_targets=len(runtime.pending_target_changes),ready_targets=len(runtime.ready_target_changes),
                    dirty_entities=len(runtime.dirty_entities))
            journal_at_input_stop=runtime.feature_observation_deferred_snapshot()
            candidate_at_input_stop=runtime.agent_candidates.candidate_shadow_async_diagnostics()
            candidate_evidence_at_input_stop=runtime.agent_candidates.candidate_shadow_persistence_snapshot()
            sqlite_at_input_stop=store.sqlite_snapshot()
            ui_stop.set()
            time.sleep(.5)
            for thread in ui_threads:thread.join(15)
            tail_seconds=time.monotonic()-tail_started
            snapshot=TELEMETRY.snapshot()
            pipeline=runtime.process_agent_pipeline_snapshot()
            assert pipeline['registered_layers']==7,pipeline
            assert runtime.state_event_pipeline_snapshot()['registered_layers']==4
            if mode=='shadow':assert not services,services
            manager=runtime.agent_candidates
            candidate_diagnostics=manager.candidate_shadow_async_diagnostics()
            if candidates:
                assert candidate_diagnostics['processed']>0,candidate_diagnostics
            def distribution(values):
                values=sorted(values)
                def percentile(p):
                    index=(len(values)-1)*p;low=int(index);high=min(low+1,len(values)-1)
                    return values[low]+(values[high]-values[low])*(index-low)
                return dict(count=len(values),p50=percentile(.5),p95=percentile(.95),
                            p99=percentile(.99),max=max(values))
            with sample_lock:
                phase[0]='closed'
                measured={key:distribution(values) for key,values in samples.items() if values}
                tail_measured={key:distribution(values) for key,values in tail_samples.items() if values}
                latency_samples={key:list(values) for key,values in samples.items()
                    if key in ('event_to_decision','ws_receive_to_decision','event_to_target_start',
                               'inference','wrapped_inference','event_to_worker_release',
                               'candidate_enqueue_to_decision','candidate_deferred_event_to_decision',
                               'ws_receive_to_candidate_decision')}
                tail_latencies={key:list(values) for key,values in tail_samples.items()
                    if key in ('event_to_decision','ws_receive_to_decision','event_to_target_start')}
            source_files={str(p.relative_to(source)):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
                for p in sorted((source/'adaptive_ai/src').rglob('*.py'))}
            return dict(version=core.APP_VERSION,seconds=elapsed,warmup_seconds=warmup,
                active=active,configured=configured,entities=len(states),
                mode=mode,ui=ui,trace=trace,ui_stats=ui_stats,server_diagnostics=server_diagnostics,sent_state_events=sent,ready_ms=ready_ms,
                ui_profile='full status/agents/events/candidates 4s; reactive live 100ms + revision wait; candidate-live 100ms',
                control_dispatch_exercised=bool(services),warmup_pipelines_drained=warmup_pipelines_drained,
                control_coverage=('guarded_no_dispatch' if mode=='control' and not services else 'dispatch' if services else 'shadow_only'),
                root_executor_results={a['target_entity']:dict(runtime.runtime[a['id']].get('intent') or {}) for a in roots},
                input_value_sha256=digest.hexdigest(),agent_confidence_threshold=.5,
                injection_lag_ms=dict(mean=lag_sum/ticks,max=lag_max,late_over_period=int(missed)),
                startup=core.startup_snapshot(),cpu_seconds=used_cpu,telemetry=snapshot,
                hot_path=runtime.inference_hot_path_metrics.snapshot(),pipeline=pipeline,service_calls=len(services),
                candidate_diagnostics=candidate_diagnostics,candidate_ids=candidate_ids,
                lifecycle_read_function_calls=dict(reads),
                measured_window=measured,latency_samples=latency_samples,
                tail_window=tail_measured,tail_latency_samples=tail_latencies,tail_seconds=tail_seconds,
                backlog_at_input_stop=backlog_at_input_stop,
                sqlite_at_input_stop=sqlite_at_input_stop,sqlite_after_tail=store.sqlite_snapshot(),
                journal_at_input_stop=journal_at_input_stop,
                candidate_at_input_stop=candidate_at_input_stop,
                candidate_evidence_at_input_stop=candidate_evidence_at_input_stop,queue_series=queue_series,
                journal_after_tail=runtime.feature_observation_deferred_snapshot(),
                source_sha256=hashlib.sha256(json.dumps(source_files,sort_keys=True).encode()).hexdigest(),
                benchmark_tool_sha256=hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
                platform=platform.platform(),python=platform.python_version(),
                native_thread_environment={name:os.environ[name] for name in (
                    'OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')},
                root_decisions={a['target_entity']:dict(desired=runtime.runtime[a['id']].get('last_prediction'),
                    source=runtime.runtime[a['id']].get('decision_source')) for a in roots},
                production_root=True,prediction_override=False,ha_transport='local stub only',
                training_fixture=training_fixture,
                training_contract=('80 updates/validations with production feature extraction and alternating presence states'
                    if training_fixture=='production' else '80 sparse fixture updates/validations; functional Control smoke only'),
                limits='Synthetic trained models and local transport, no deployed Pi/Ingress/physical ACK claim; startup grace and workers retained.')
        finally:
            ui_stop.set()
            for thread in ui_threads:thread.join(15)
            drain_started=time.monotonic()
            deadline=drain_started+60.
            while True:
                with request_lock:active_requests=tuple(request_threads)
                if not active_requests:break
                remaining=deadline-time.monotonic()
                assert remaining>0,'Fixture HTTP handlers did not drain before runtime shutdown'
                for thread in active_requests:thread.join(min(.2,remaining))
            server_diagnostics['request_drain_seconds']=time.monotonic()-drain_started
            if core.HTTP_SERVER is not None:core.HTTP_SERVER.shutdown()
            server_thread.join(30)
            assert not server_thread.is_alive(),'shipped shutdown did not finish'
            join_runtime_producers(core)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--seconds',type=float,default=30)
    p.add_argument('--warmup-seconds',type=float,default=5.)
    p.add_argument('--active',type=int,default=3)
    p.add_argument('--configured',type=int,default=20)
    p.add_argument('--mode',choices=('shadow','control'),default='shadow')
    p.add_argument('--ui',action='store_true');p.add_argument('--trace',action='store_true')
    p.add_argument('--candidates',action='store_true')
    p.add_argument('--training-fixture',choices=('production','legacy'),default='production')
    a=p.parse_args()
    assert 1<=a.active<=a.configured<=100 and a.seconds>0
    print(json.dumps(one(a.source_root,a.seconds,a.active,a.configured,a.mode,a.ui,a.trace,a.candidates,a.warmup_seconds,a.training_fixture)))
