"""Bounded original arbiter evidence at requested cardinalities; HOST ONLY.

The RAM boundary metric is NOT composed Engine event-to-intent latency. Use
benchmark_runtime_scaling for that independent measurement. No trainer or HA.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_universal_predecision import PredecisionTests
from test_device_agents import _agent
from agent_candidate_lineage import _ensure_root
from telemetry import rss_mb


def quantiles(values):
    rows=sorted(values)
    return {f'p{p}_ms':rows[int((len(rows)-1)*p/100)] if rows else None for p in (50,95,99)}


def case(sensors,devices):
    test=PredecisionTests();test.setUp()
    try:
        for i in range(sensors):
            test.engine.state_map[f'binary_sensor.s{i}']=dict(entity_id=f'binary_sensor.s{i}',state='on',attributes={'device_class':'motion'})
        for i in range(1,devices):
            eid=f'light.l{i}'
            agent=_agent(test.store,eid,'power')
            cfg=test.store.get_agent_config(agent['id'])
            test.engine.agent_configs[cfg['id']]=cfg
            test.engine.state_map[eid]=dict(entity_id=eid,state='off',attributes={})
            test.engine.context.entities[eid]={'device_id':f'd{i}'}
            test.engine.context.devices[f'd{i}']={'area_id':'kitchen'}
            _ensure_root(test.store,cfg['id'],generation_number=0)
        test.engine.context.registry_revision+=1
        statements=[];conn=test.store.conn
        @contextmanager
        def measured_conn():
            with conn() as c:
                c.set_trace_callback(statements.append)
                try:yield c
                finally:c.set_trace_callback(None)
        prepare=[];boundary=[];accepted=0
        cpu=time.process_time();wall=time.perf_counter();rss_before=rss_mb()
        with patch.object(test.store,'conn',measured_conn):
            for aid,agent in test.engine.agent_configs.items():
                before=time.perf_counter()
                test.evidence.request(aid);test.evidence.prepare_one()
                prepare.append((time.perf_counter()-before)*1000)
                state=test.engine.state_map[agent['target_entity']]
                for i in range(5):
                    before=time.perf_counter()
                    with patch.object(test.store,'conn',side_effect=AssertionError('boundary SQL')):
                        ticket=test.evidence.begin(agent,state,[0.,1.])
                        intent=SimpleNamespace(intent_id=f'{aid}-{i}',created_at=time.time(),policy_version=12,model_revision='benchmark')
                        bound=test.evidence.bind(ticket,intent,agent,state,[0.,1.])
                    boundary.append((time.perf_counter()-before)*1000)
                    accepted+=bound is not None
        return dict(sensors=sensors,devices=devices,cpu_seconds=time.process_time()-cpu,
            wall_seconds=time.perf_counter()-wall,rss_before_mb=rss_before,rss_after_mb=rss_mb(),
            prepare=quantiles(prepare),ram_boundary_including_assertion_mock=quantiles(boundary),
            native_sql_statements=len(statements),native_selects=sum(s.lstrip().upper().startswith('SELECT') for s in statements),
            prepared=len(prepare),bound=accepted,training_eligible=0,
            reason='affected sensor set deliberately unknown; no synthetic causal claims',
            ha_calls=0,extra_intents=0,evidence=test.evidence.stats,prepared_bytes=test.evidence.prepared_bytes)
    finally:test.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    cases=[]
    for item in args.cases.split(','):
        sensors,devices=map(int,item.split(':'))
        if not 1<=sensors<=1000 or not 1<=devices<=200:parser.error('bounded cases only')
        cases.append(case(sensors,devices))
    result=dict(scope='host component measurements; no composed event-to-intent, Pi4 or physical HA acceptance',cases=cases)
    raw=json.dumps(result,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(raw,encoding='utf-8')
    print(raw)


if __name__=='__main__':main()
