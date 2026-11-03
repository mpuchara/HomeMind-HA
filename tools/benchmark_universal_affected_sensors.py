"""HOST original evidence -> native dataset at exact sensor/device cardinalities.

Use --source-root to exercise the exact PR #361 implementation with this same
fixture; it has no declarations and must yield zero eligible samples. No HA,
automatic trainer or synthetic timestamp override is used by this component.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


def quantiles(rows):
    rows=sorted(rows)
    return {f'p{p}_ms':rows[int((len(rows)-1)*p/100)] if rows else None for p in (50,95,99)}


def case(root,sensors,devices):
    sys.path[:0]=[str(root/'tools'),str(root/'tests'),str(root/'adaptive_ai/src')]
    from test_universal_predecision import PredecisionTests
    from test_device_agents import _agent
    from agent_candidate_lineage import _ensure_root
    from manual_feedback_contract import ManualFeedbackJournal
    from correct_data_foundation import _ensure_schema
    from teaching_rl import RLTeaching
    from provenance import ProvenanceJournal
    from intent import ActionIntent
    from telemetry import rss_mb
    from universal_history_collector import source_from_collection
    f=PredecisionTests();f.setUp()
    try:
        agents=[f.agent]
        for i in range(1,devices):
            created=_agent(f.store,f'light.l{i}','power')
            agent=f.store.get_agent_config(created['id']);agents.append(agent)
            _ensure_root(f.store,agent['id'],generation_number=0)
        states={'binary_sensor.motion':dict(entity_id='binary_sensor.motion',state='on',attributes={'device_class':'motion'})}
        effects=[f'sensor.effect_{i}' for i in range(devices)]
        for eid in effects:states[eid]=dict(entity_id=eid,state='12',attributes={'device_class':'temperature'})
        for i in range(sensors-len(states)):
            eid=f'sensor.background_{i}';states[eid]=dict(entity_id=eid,state='12',attributes={'device_class':'temperature'})
        assert len(states)==sensors
        entities={eid:{'device_id':'measurement-device'} for eid in states}
        devs={'measurement-device':{'area_id':'fixture-room'}}
        for i,agent in enumerate(agents):
            eid=agent['target_entity'];states[eid]=dict(entity_id=eid,state='off',attributes={})
            entities[eid]={'device_id':f'device-{i}'};devs[f'device-{i}']={'area_id':'fixture-room'}
        f.engine.agent_configs={a['id']:a for a in agents}
        f.engine.state_map=states;f.engine.context.entities=entities;f.engine.context.devices=devs
        f.engine.context.registry_revision+=1
        for kind,rows in [('entity',[dict(entity_id=eid,**v) for eid,v in entities.items()]),
                          ('device',[dict(id=eid,**v) for eid,v in devs.items()]),
                          ('area',[dict(area_id='fixture-room')])]:f.collector.registry(kind,rows)
        f.drain();requested=time.time()
        seeds={eid:dict(st,last_updated=datetime.now(timezone.utc).isoformat()) for eid,st in states.items() if eid.startswith(('sensor.','binary_sensor.'))}
        f.collector.snapshot(seeds,requested_ts=requested,received_ts=time.time(),epoch=f.collector.epoch)
        f.collector.barrier();f.drain()
        supported=bool((root/'adaptive_ai/src/universal_affected_sensors.py').exists())
        if supported:
            from universal_affected_sensors import AffectedSensorDeclarations
            api=AffectedSensorDeclarations(f.store)
            for i,agent in enumerate(agents):
                api.declare(agent['id'],logical_device_id=f.service.descriptor(agent)['logical_device_id'],
                    topology_revision=f.collector.topology,affected_sensor_ids=[effects[i]],complete=True,
                    approved_by='synthetic fixture operator',rationale='explicit independent fixture dependency, not a physical home claim')
        RLTeaching(f.store,type('Runtime',(),{})());_ensure_schema(f.store)
        feedback=ManualFeedbackJournal(f.store);provenance=ProvenanceJournal(f.store)
        statements=[];original_conn=f.store.conn
        @contextmanager
        def measured_conn():
            with original_conn() as c:
                c.set_trace_callback(statements.append)
                try:yield c
                finally:c.set_trace_callback(None)
        prepare=[];boundary=[];complete=0
        initial=sum(p.stat().st_size for p in Path(f.store.path).parent.glob('data.db*'))
        cpu=time.process_time();wall=time.perf_counter();rss_before=rss_mb()
        with patch.object(f.store,'conn',measured_conn):
            for i,agent in enumerate(agents):
                before=time.perf_counter();f.evidence.request(agent['id']);f.evidence.prepare_one()
                prepare.append((time.perf_counter()-before)*1000)
                state=states[agent['target_entity']]
                with patch.object(f.store,'conn',side_effect=AssertionError('hot SQL')):
                    before=time.perf_counter()
                    ticket=f.evidence.begin(agent,state,[0.,1.])
                    intent=ActionIntent.create(agent_id=agent['id'],target_entity=agent['target_entity'],target_property='power',
                        desired_value=1.,confidence=.8,support=.8,novelty=.1,prediction_horizon=1,created_at=time.time(),
                        ttl=2,policy_version=12,model_revision='fixture',context_revision=1,target_revision=0,reason='fixture')
                    bound=f.evidence.bind(ticket,intent,agent,state,[0.,1.])
                    boundary.append((time.perf_counter()-before)*1000)
                    assert bound is not None
                    complete+=bound.get('affected_sensor_status')=='complete'
                    assert f.collector.decision(intent,agent,state,required=('binary_sensor.motion',),predecision=bound)
                generation=bound['generation_id']
                provenance.record_decision(decision_id=intent.intent_id,created_time=intent.created_at,agent_id=agent['id'],
                    generation_id=generation,model_version=12,model_revision='fixture')
                feedback.record(agent_id=agent['id'],selected_ts=intent.created_at,source='correct',correct_action=1.,feedback_id=f'fixture-correct-{i}')
                if i%8==7:f.drain()
            f.collector.barrier();f.drain()
            with f.store.conn() as c:
                start,through=c.execute('SELECT start,through FROM universal_collection_segments ORDER BY start DESC LIMIT 1').fetchone()
                c.execute('BEGIN')
                source=source_from_collection(c,[a['id'] for a in agents],start=start,through=through)
                dataset=source.dataset(c)
        result=dict(sensors=sensors,devices=devices,declarations_supported=supported,
            cpu_seconds=time.process_time()-cpu,wall_seconds=time.perf_counter()-wall,
            rss_before_mb=rss_before,rss_after_mb=rss_mb(),prepare=quantiles(prepare),ram_boundary=quantiles(boundary),
            sqlite_statements=len(statements),sqlite_selects=sum(s.lstrip().upper().startswith('SELECT') for s in statements),
            database_growth_bytes=sum(p.stat().st_size for p in Path(f.store.path).parent.glob('data.db*'))-initial,
            complete_evidence=complete,training_eligible=len(dataset.samples),rejections=list(dataset.rejected),
            collector=f.collector.diagnostics(),ha_calls=0,extra_intents=0,
            note='explicit synthetic operator declarations; source-native clocks; no physical causal truth/Pi4 acceptance')
        assert len(dataset.samples)==(devices if supported else 0),result
        return result
    finally:f.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):os.environ[name]='1'
    rows=[]
    for item in args.cases.split(','):
        sensors,devices=map(int,item.split(':'))
        if not 1<=devices<sensors<=1000 or devices>200:parser.error('bounded sensor/device cases required')
        rows.append(case(args.source_root.resolve(),sensors,devices))
    raw=json.dumps(dict(scope='host evidence preparation and native dataset; not Engine event-to-intent',cases=rows),indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
    print(raw)


if __name__=='__main__':main()
