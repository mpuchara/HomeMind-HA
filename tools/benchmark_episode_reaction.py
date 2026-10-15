"""Independent-source comparison parity and actual Engine event scheduling under UI load.

Synthetic episodes and injected sensor events. The real scheduler, policy and Shadow
Executor run; no HA service is sent. This is a host contention benchmark, not device ACK.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def seed(store, count):
    # Structured audit JSON incurs the same allocation/decoding class as episode
    # metric evidence; it does not affect the aggregate result being compared.
    evidence=json.dumps({str(i):dict(value=.25,evidence='counterfactual_proxy',complete=True,
                        known_seconds=60,unknown_seconds=0) for i in range(48)})
    metrics=json.dumps(dict(meaningful=True,harmful=False,meaningful_proxy=True,proxy_harmful=False,
                           needed_on_delay_seconds=1.,off_while_needed_seconds=0.,unnecessary_on_seconds=0.,
                           false_arrival_prediction=0,retrigger_count=0,chatter_count=0,
                           manual_correction_count=0,automation_disagreement_seconds=1.))
    for offset in range(0,count,32):
        with store.conn() as c:
            for i in range(offset,min(offset+32,count)):
                eid=f'episode-{i:06}'
                c.execute("INSERT INTO episode_evaluator_episodes VALUES(?,1,'light_power','a',?,?,?, ?,?,NULL,NULL,NULL,?,?)",
                          (eid,float(i),float(i+60),'{}',json.dumps({'light_need':'true' if i%2 else 'false'}),'{}',eid,float(i)))
                for key in (('p','c') if i%5 else ('p',)):
                    c.execute('INSERT INTO episode_evaluator_policy_results VALUES(?,?,?,0,1,?,?)',
                              (eid,key,'candidate',metrics,evidence))
    return len(evidence.encode())


def one(source, episodes, events):
    scratch=tempfile.TemporaryDirectory(prefix='episode-reaction-global-')
    os.environ['ADAPTIVE_AI_DATA']=scratch.name
    sys.path[:0]=[str(source/'adaptive_ai/src'),str(source/'tests'),str(source)]
    from storage import Store
    from episode_evaluator import EpisodeEvaluator
    from runtime_threading import configure_runtime_threads
    import support
    import engine as engine_module
    import executor as executor_module
    configure_runtime_threads({})
    with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
        store=Store(Path(tmp)/'runtime.db');evaluator=EpisodeEvaluator(store)
        evidence_bytes=seed(store,episodes)
        cold_started=time.perf_counter()
        baseline_result=evaluator.compare_policies('a','p','c')
        comparison_cold_ms=(time.perf_counter()-cold_started)*1000
        digest=hashlib.sha256(json.dumps(baseline_result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        durations=[]
        for _ in range(8):
            started=time.perf_counter();result=evaluator.compare_policies('a','p','c')
            durations.append((time.perf_counter()-started)*1000)
            assert result==baseline_result
        stack.enter_context(patch.object(engine_module,'STORE',store))
        stack.enter_context(patch.object(executor_module,'STORE',store))
        runtime=engine_module.Engine()
        stack.enter_context(patch.dict(engine_module.OPTIONS,{'realtime_inference_debounce_ms':25}))
        for name in ('control_workers','poll_worker','registry_worker','housekeeping_worker'):
            stack.callback(getattr(runtime,name).shutdown,wait=True,cancel_futures=True)
        agent=store.create_agent(support.agent(id='reaction',target_entity='light.kitchen',
                                  input_entities=['binary_sensor.motion'],mode='shadow'))
        detail={'balanced':True,'counts':{'samples':80,'correct':80,'per_action':{
            '0':{'samples':40,'correct':40},'1':{'samples':40,'correct':40}}}}
        store.set_training_state(agent['id'],'qualified',score=1.,samples=80,detail=detail)
        store.update_agent(agent['id'],{'mode':'shadow'})
        agent=store.get_agent_config(agent['id'])
        runtime.state_map={agent['target_entity']:support.state(agent['target_entity'],'off'),
                           'binary_sensor.motion':support.state('binary_sensor.motion','off')}
        runtime.context.configure(runtime.state_map);runtime.policy(agent)
        runtime._refresh_agent_index(force=True)
        runtime.refresh_states=lambda:None
        runtime._startup_warmup_step=lambda _states:0
        runtime._due_inference_targets=lambda:set()
        runtime._schedule_housekeeping=lambda:None
        runtime._maybe_schedule_state_resync=lambda:None
        runtime.startup_inference_not_before=0.;runtime.initial_inference_pending=False
        runtime.inference_enabled.set()
        completed=threading.Event();results=[];injected=[0.]
        original_submit=runtime.executor.submit
        def submit(*args,**kw):
            result=original_submit(*args,**kw)
            results.append(((time.perf_counter()-injected[0])*1000,result['status']))
            completed.set()
            return result
        runtime.executor.submit=submit
        service=stack.enter_context(patch.object(executor_module.HA,'service',side_effect=AssertionError('Shadow cannot dispatch')))
        reader_stop=threading.Event();reader_ready=threading.Event();reads=[0];errors=[]
        def ui_reader():
            try:
                while not reader_stop.is_set():
                    assert evaluator.compare_policies('a','p','c')==baseline_result
                    reads[0]+=1;reader_ready.set()
                    reader_stop.wait(.05)
            except BaseException as exc:
                errors.append(repr(exc));reader_ready.set()
        worker=threading.Thread(target=ui_reader,name='synthetic-ui-reader',daemon=True)
        loop=threading.Thread(target=runtime.run,name='real-engine-loop',daemon=True)
        worker.start();assert reader_ready.wait(10);loop.start()
        started=time.perf_counter();cpu=time.process_time()
        try:
            for i in range(events):
                completed.clear();injected[0]=time.perf_counter()
                stamp=datetime.now(timezone.utc).isoformat()
                state=dict(entity_id='binary_sensor.motion',state='on' if i%2==0 else 'off',
                           attributes={'device_class':'motion'},last_changed=stamp,last_updated=stamp,context={})
                runtime.on_state_changed({'entity_id':'binary_sensor.motion','new_state':state})
                assert completed.wait(10),f'event {i} lost'
                assert not errors,errors
        finally:
            elapsed=time.perf_counter()-started;cpu=time.process_time()-cpu
            reader_stop.set();runtime.stop_event.set();runtime.wake_event.set()
            worker.join(10);loop.join(10)
            assert not worker.is_alive() and not loop.is_alive()
        assert len(results)==events and all(status=='SHADOW' for _,status in results),results
        service.assert_not_called()
        timings=sorted(value for value,_ in results)
        snap=getattr(evaluator,'comparison_cache_snapshot',lambda:{})()
        return dict(episodes=episodes,matched_episodes=baseline_result['matched_episodes'],evidence_bytes_per_policy=evidence_bytes,
                    comparison_sha256=digest,comparison_cold_ms=comparison_cold_ms,comparison_median_ms=statistics.median(durations),
                    events=events,event_to_shadow_executor_median_ms=statistics.median(timings),
                    event_to_shadow_executor_p95_ms=timings[int((len(timings)-1)*.95)],
                    event_max_ms=max(timings),all_events_completed=True,ha_service_calls=0,
                    ui_read_requests=reads[0],process_cpu_seconds_per_second=cpu/elapsed,
                    comparison_cache=snap)


def paired(baseline, episodes, events):
    pairs=[]
    for n in range(3):
        pair={}
        for label in (('previous','current') if n%2==0 else ('current','previous')):
            source=baseline if label=='previous' else ROOT
            raw=subprocess.check_output([sys.executable,str(Path(__file__).resolve()),'--one',
                '--source-root',str(source),'--episodes',str(episodes),'--events',str(events)],text=True)
            pair[label]=json.loads(raw.strip().splitlines()[-1])
        assert pair['previous']['comparison_sha256']==pair['current']['comparison_sha256']
        pair['comparison_speedup']=pair['previous']['comparison_median_ms']/pair['current']['comparison_median_ms']
        pair['reaction_p95_speedup']=pair['previous']['event_to_shadow_executor_p95_ms']/pair['current']['event_to_shadow_executor_p95_ms']
        pairs.append(pair)
    return dict(synthetic=True,independent_sources=True,series=pairs,
                comparison_speedup_median=statistics.median(x['comparison_speedup'] for x in pairs),
                reaction_p95_speedup_median=statistics.median(x['reaction_p95_speedup'] for x in pairs),
                limit='Host Engine/scheduler and real Shadow Executor under a 50 ms UI comparison workload. No Tournament inference, HA network or device ACK. Episode result parity and delivery checked; not a Raspberry Pi latency guarantee.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--one',action='store_true');p.add_argument('--source-root',type=Path,default=ROOT);p.add_argument('--baseline-root',type=Path);p.add_argument('--episodes',type=int,default=1200);p.add_argument('--events',type=int,default=64);a=p.parse_args()
    if a.episodes<1 or a.events<1 or (not a.one and a.baseline_root is None):p.error('positive counts and baseline-root required (except --one)')
    print(json.dumps(one(a.source_root.resolve(),a.episodes,a.events) if a.one else paired(a.baseline_root.resolve(),a.episodes,a.events),separators=(',',':')))
