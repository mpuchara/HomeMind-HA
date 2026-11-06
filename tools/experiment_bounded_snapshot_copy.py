"""Isolated full-copy experiment; not imported by production.

Preserves every node, depth and byte budget. Inline only exact string mapping
keys; these are immutable and do not need a recursive call to detach them.
"""
import json
from pathlib import Path
import random
import sys
import time


def copy_snapshot(value, *, limit=262144):
    used=0;nodes=0
    def visit(item,depth=0):
        nonlocal used,nodes
        nodes+=1;used+=64
        if nodes>65536 or depth>10 or used>limit:raise ValueError('receipt structural bound')
        if isinstance(item,str):
            used+=len(item)*4
            if used>limit:raise ValueError('receipt string bound')
            return item
        if item is None or type(item) in (bool,int,float):return item
        if isinstance(item,dict):
            if len(item)>10000:raise ValueError('receipt mapping bound')
            result={}
            for key,value in item.items():
                if type(key) is str:
                    nodes+=1;used+=64
                    if nodes>65536 or depth+1>10 or used>limit:raise ValueError('receipt structural bound')
                    used+=len(key)*4
                    if used>limit:raise ValueError('receipt string bound')
                    copied_key=key
                else:
                    copied_key=visit(key,depth+1)
                result[copied_key]=visit(value,depth+1)
            return result
        if isinstance(item,(list,tuple)):
            if len(item)>10000:raise ValueError('receipt list bound')
            return [visit(v,depth+1) for v in item]
        raise ValueError('non-JSON receipt')
    return visit(value),used


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adaptive_ai/src'))
    from universal_history_collector import bounded_copy
    rng=random.Random(1934)
    def node(depth):
        if depth==0 or rng.random()<.5:
            return rng.choice([None,True,False,1,1.25,'key','longer string',object()])
        if rng.random()<.5:
            return [node(depth-1) for _ in range(rng.randrange(6))]
        return {str(i):node(depth-1) for i in range(rng.randrange(6))}
    checked=0
    for _ in range(2000):
        value=node(8);limit=rng.choice([64,128,512,4096,262144])
        outcomes=[]
        for fn in (bounded_copy,copy_snapshot):
            try:outcomes.append(('value',fn(value,limit=limit)))
            except ValueError as exc:outcomes.append(('error',str(exc)))
        assert outcomes[0]==outcomes[1],outcomes
        checked+=1
    value={'states':{f'sensor.fixture_{i}':dict(entity_id=f'sensor.fixture_{i}',state='12',
        attributes={'device_class':'power','nested':{'values':[1,2,3]}},
        last_changed='2026-11-06T12:00:00Z',last_updated='2026-11-06T12:00:00Z')
        for i in range(1200)}}
    # Alternate function order, preserve each observation; no Gate D claim.
    observations=[]
    for repeat in range(10):
        row={}
        for fn in ((bounded_copy,copy_snapshot) if repeat%2==0 else (copy_snapshot,bounded_copy)):
            started=time.thread_time()
            for _ in range(20):result=fn(value,limit=4*1024*1024)
            row[fn.__name__]=(time.thread_time()-started)*1000/20
        observations.append(row)
    print(json.dumps(dict(random_differential_cases=checked,observations=observations,
        scope='synthetic CPU microbenchmark, full snapshot, not acceptance'),indent=2))


if __name__=='__main__':main()
