"""Compare actual device descriptors/manual-hold guards using independent sources.

This is a SQL/guard component measurement; no policy, Executor or physical HA timing.
"""
import argparse
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
from types import SimpleNamespace


def one(source,passes):
    with tempfile.TemporaryDirectory() as directory:
        os.environ['ADAPTIVE_AI_DATA']=directory
        sys.path.insert(0,str(source/'adaptive_ai/src'))
        from storage import Store
        from device_agents import DeviceAgentService
        store=Store(Path(directory)/'guard.db')
        registry={f'sensor.extra_{i}':dict(device_id=f'extra-{i}') for i in range(758)}
        states={}
        agents=[]
        for i in range(20):
            eid=f'light.fixture_{i}'
            registry[eid]=dict(device_id=f'light-{i}',area_id=f'area-{i%9}')
            states[eid]=dict(entity_id=eid,state='off',attributes={})
            agents.append(store.create_agent(dict(name=f'Agent {i}',target_entity=eid,target_property='power',
                min_value=0,max_value=1,mode='paused',input_entities=['*'])))
        context=SimpleNamespace(resolved_registry=lambda:registry,devices={})
        engine=SimpleNamespace(context=context,state_map=states,runtime={},lock=threading.RLock())
        service=DeviceAgentService(engine,store);agent=agents[0]
        durations=[];outputs=[]
        with store.connection_session() as c:
            service.shared_manual_hold_until(agent,{})
            for _ in range(passes):
                start=time.perf_counter()
                result=(service.descriptor(agent),service.shared_manual_hold_until(agent,{}))
                durations.append((time.perf_counter()-start)*1000)
                outputs.append(result)
            sql_counts=dict(select=0,other=0)
            def count(sql):
                key='select' if sql.lstrip().upper().startswith('SELECT') else 'other'
                sql_counts[key]+=1
            c.set_trace_callback(count)
            service.descriptor(agent);service.shared_manual_hold_until(agent,{})
            c.set_trace_callback(None)
        return dict(passes=passes,entities=len(registry),agents=len(agents),median_ms=statistics.median(durations),
            samples_ms=durations,sql_counts_outside_timing=sql_counts,
            result_sha256=hashlib.sha256(json.dumps(outputs,sort_keys=True).encode()).hexdigest(),
            scope='actual DeviceAgentService descriptor + shared manual-hold guard; no Executor/HA')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--baseline-root',type=Path);p.add_argument('--one',action='store_true')
    p.add_argument('--passes',type=int,default=8);a=p.parse_args()
    if a.one:return print(json.dumps(one(a.source_root,a.passes)))
    assert a.baseline_root is not None
    pairs=[]
    for i in range(3):
        pair={}
        for kind in (('baseline','current') if i%2==0 else ('current','baseline')):
            source=a.baseline_root if kind=='baseline' else a.source_root
            result=subprocess.run([sys.executable,__file__,'--source-root',str(source),'--one','--passes',str(a.passes)],
                capture_output=True,text=True,check=True)
            pair[kind]=json.loads(result.stdout.strip().splitlines()[-1])
        assert pair['baseline']['result_sha256']==pair['current']['result_sha256']
        pair['median_speedup']=pair['baseline']['median_ms']/pair['current']['median_ms']
        pairs.append(pair)
    print(json.dumps(dict(pairs=pairs,all_results_identical=True,
        limitation='Host SQL component; not end-to-end or physical HA latency.')))


if __name__=='__main__':main()
