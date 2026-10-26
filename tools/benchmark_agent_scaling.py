"""Real Engine scheduler + Sensor Tournament, independent G0 Shadow devices.

Host-only synthetic inputs and deterministic Root confidence; no HA network or
physical latency claim. Each count runs in its own process with the same sources.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def journal_one(source,count):
    scratch=tempfile.TemporaryDirectory();os.environ['ADAPTIVE_AI_DATA']=scratch.name
    sys.path[:0]=[str(source/'tests'),str(source/'adaptive_ai/src'),str(source)]
    import support
    from storage import Store
    from observation_contract import FeatureJournal
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(Path(tmp)/'journal.db');journal=FeatureJournal(store,clock=lambda:1000.)
        common=['sensor.shared'+str(i) for i in range(8)]
        targets=['light.device'+str(i) for i in range(count)]
        journal.record_batch([dict(entity_id=eid,state=support.state(eid,str(t%2)),event_time=float(t),
            received_time=float(t),source='fixture') for eid in common+targets for t in range(1600)])
        times=iter(1000.+i for i in range(count));journal.clock=lambda:next(times)
        windows=[dict(window_id='w'+str(i),agent_id='root'+str(i),entities=common+[targets[i]],
                      anchor_time=800.,before=800.,after=800.) for i in range(count)]
        with store.connection_session() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.execute('PRAGMA wal_autocheckpoint=0')
            before=c.total_changes;started=time.perf_counter();journal.open_windows_batch(windows)
            ms=(time.perf_counter()-started)*1000;changes=c.total_changes-before
            evidence=[tuple(row) for row in c.execute('SELECT entity_id,event_time,protected_until FROM feature_observation_events ORDER BY entity_id,event_time')]
            links=[tuple(row) for row in c.execute('SELECT * FROM feature_window_entities ORDER BY window_id,entity_id')]
        return dict(agents=count,shared_sensors=8,events_per_entity=1600,ms=ms,
            total_changes=changes,protection_updates=changes-count*(2+len(common)),
            evidence_sha256=hashlib.sha256(json.dumps((evidence,links)).encode()).hexdigest(),
            scope='Actual SQLite evidence protection only; not physical HA latency')


def one(source, count, events, entities):
    scratch=tempfile.TemporaryDirectory();os.environ['ADAPTIVE_AI_DATA']=scratch.name
    sys.path[:0]=[str(source/'tests'),str(source/'adaptive_ai/src'),str(source)]
    import support
    import engine as em
    import executor as ex
    from context import TemporalHistory
    from context_engine import ContextEngine
    from observation_contract import install_training_contract, FeatureSchemaV12
    from policy import MultiHorizonPolicy
    from storage import Store
    from context_tournament import install
    from context_tournament_metrics import install_metrics
    from context_tournament_promotion import install_promotion
    from context_tournament_quality import install_sensor_quality
    from context_tournament_policy_candidate import install_policy_candidates
    from context_schema_history import install_schema_history
    from context_tournament_requalification import install_promotion_shadow_requalification
    from context_schema_probation import install_schema_probation
    from context_tournament_events import install_context_events
    from runtime_background_compute import COMPUTE
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        store=Store(Path(tmp)/'scaling.db');stack.callback(store.stop_runtime_cache)
        for mod in (em,ex):stack.enter_context(patch.object(mod,'STORE',store))
        contract=install_training_contract();stack.callback(contract['restore'])
        stack.enter_context(patch.dict(em.OPTIONS,runtime_shadow_observation_interval_ms=0))
        e=em.Engine()
        for name in ('control_workers','poll_worker','registry_worker','housekeeping_worker'):
            stack.callback(getattr(e,name).shutdown,wait=True,cancel_futures=True)
        states={f'binary_sensor.shared_{i}':support.state(f'binary_sensor.shared_{i}','off',device_class='occupancy') for i in range(8)}
        for i in range(max(0,entities-8-count)):
            eid=f'sensor.other_{i}';states[eid]=support.state(eid,str(i%100))
        agents=[]
        for i in range(count):
            a=store.create_agent(support.agent(target_entity=f'light.device_{i}',input_entities=['*'],action_interval=0,mode='shadow'))
            store.set_training_state(a['id'],'qualified',score=1.,samples=80,detail={})
            a=store.get_agent_config(a['id']);agents.append(a);states[a['target_entity']]=support.state(a['target_entity'],'off')
        e.state_map=states;e.context=ContextEngine(em.OPTIONS);e.context.configure(states)
        e.temporal_history=TemporalHistory(maxlen=96)
        seed_ts=time.time()-1.
        for eid,st in states.items():
            for age in reversed(range(24)):e.temporal_history.add(eid,seed_ts-age,st)
            e.context.observe(eid,st,seed_ts,learn=False)
        for a in agents:
            p=MultiHorizonPolicy(a,states,{},set(),context_engine=e.context)
            p.schema=FeatureSchemaV12(p.dims,[f'binary_sensor.shared_{i}' for i in range(8)])
            e.models[a['id']]=p
        service=install(store,e)
        for installer in (install_metrics,install_promotion,install_sensor_quality,install_policy_candidates,
                          install_schema_history,install_promotion_shadow_requalification,install_schema_probation,install_context_events):
            installer(service)
        stack.callback(COMPUTE.close)
        stack.enter_context(patch.object(ex.AUTOMATION_KNOWLEDGE,'hints_for_target',return_value=(set(),[])))
        ha=stack.enter_context(patch.object(ex.HA,'service',side_effect=AssertionError('Shadow called HA')))
        predict=MultiHorizonPolicy.predict
        def fixed(policy,features):
            chosen,confidence,arms,horizon,support_value,novelty=predict(policy,features)
            return chosen,.99,arms,horizon,1.,0.
        stack.enter_context(patch.object(MultiHorizonPolicy,'predict',fixed))
        e._refresh_agent_index(force=True);e.refresh_states=lambda:None
        e._startup_warmup_step=lambda _:0;e._due_inference_targets=lambda:set()
        e._schedule_housekeeping=lambda:None;e._maybe_schedule_state_resync=lambda:None
        e.startup_inference_not_before=0.;e.initial_inference_pending=False;e.inference_enabled.set()
        done=threading.Event();seen=set();rows=[];latencies=[];received=[0.]
        submit=e.executor.submit
        def observe(intent,*args,**kwargs):
            result=submit(intent,*args,**kwargs)
            if received[0] and intent.agent_id not in seen:
                seen.add(intent.agent_id);latencies.append((time.perf_counter()-received[0])*1000.)
                rows.append((agents.index(next(a for a in agents if a['id']==intent.agent_id)),float(intent.desired_value),result['status']))
                if len(seen)==count:done.set()
            return result
        e.executor.submit=observe
        loop=threading.Thread(target=e.run,daemon=True);loop.start()
        try:
            for step in range(events):
                # Drain prior Root workers; optional Tournament work can overlap.
                for future in list(e.in_flight.values()):future.result(15.)
                seen.clear();done.clear();received[0]=time.perf_counter()
                st=support.state('binary_sensor.shared_1','on' if step%2 else 'off')
                stamp=datetime.now(timezone.utc).isoformat();st.update(last_updated=stamp,last_changed=stamp)
                e.on_state_changed({'entity_id':'binary_sensor.shared_1','new_state':st})
                if not done.wait(20.):raise AssertionError((len(seen),count,e.error))
        finally:
            for future in list(e.in_flight.values()):future.result(15.)
            deadline=time.monotonic()+20.
            q=service._deferred_observations
            while (q.snapshot()['active'] or q.snapshot()['pending']) and time.monotonic()<deadline:
                time.sleep(.01)
            assert not q.snapshot()['active'] and not q.snapshot()['pending'],q.snapshot()
            e.stop_event.set();e.wake_event.set();loop.join(10.)
            for future in list(e.in_flight.values()):future.result(10.)
            service._deferred_observations.close(15.)
            service._shadow_flush_event.set();service._shadow_writer_thread.join(15.)
            assert not loop.is_alive() and not service._shadow_writer_thread.is_alive()
        ha.assert_not_called();assert len(rows)==count*events
        assert all(row[2]=='SHADOW' for row in rows),rows
        # Round order may vary across workers; decision values/status must not.
        canonical=[sorted(rows[i:i+count]) for i in range(0,len(rows),count)]
        times=sorted(latencies)
        return dict(agents=count,events=events,entities=len(states),samples=len(times),
            event_to_executor_p95_ms=times[int((len(times)-1)*.95)],median_ms=times[len(times)//2],
            decisions_sha256=hashlib.sha256(json.dumps(canonical).encode()).hexdigest(),
            decision_statuses={status:sum(row[2]==status for row in rows) for status in sorted({row[2] for row in rows})},
            ha_calls=0,root_confidence_fixture=True,real_engine_scheduler=True,
            tournament=service.shadow_persistence_snapshot(),target_pi_verified=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--agents',type=int,default=4);p.add_argument('--events',type=int,default=12)
    p.add_argument('--entities',type=int,default=780);p.add_argument('--journal',action='store_true');a=p.parse_args()
    if not 1<=a.agents<=16 or not 2<=a.events<=100:p.error('bounded agents/events required')
    print(json.dumps(journal_one(a.source_root,a.agents) if a.journal else one(a.source_root,a.agents,a.events,a.entities)))


if __name__=='__main__':main()
