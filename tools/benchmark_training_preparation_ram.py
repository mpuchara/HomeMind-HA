"""Compare bounded Recorder buffer profiles on a healthy local SQLite fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--rows-per-entity',type=int,default=2000)
    parser.add_argument('--pairs',type=int,default=5)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='hm-prep-ram-') as scratch:
        os.environ['ADAPTIVE_AI_DATA']=scratch
        root=Path(__file__).resolve().parents[1]
        sys.path.insert(0,str(root/'adaptive_ai/src'));sys.path.insert(0,str(root))
        import history
        from storage import Store
        from settings import DEFAULT_OPTIONS
        from training_preparation import preparation_profile
        from tools.benchmark_history_import import payload,manager,canonical
        data=payload(args.rows_per_entity);pairs=[]
        for pair in range(args.pairs):
            result={}
            for rows in ((128,1024) if pair%2==0 else (1024,128)):
                with patch.dict(history.OPTIONS,storage_budget_mode='manual'):
                    store=Store(Path(scratch)/f'{pair}-{rows}.db')
                m=manager();m.training_job_agent_id='fixture'
                m.training_preparation_profile=preparation_profile(dict(DEFAULT_OPTIONS,training_recorder_batch_rows=rows),
                    {'total_mb':7806.,'available_mb':5600.},320.)
                m.set_status=lambda *a,**kw:None
                durations=[];original=store.archive_packed_batch
                def write(items):
                    start=time.perf_counter();n=original(items);durations.append((time.perf_counter()-start)*1000);return n
                with patch.object(history,'STORE',store),patch.object(store,'archive_packed_batch',side_effect=write), \
                        patch.object(history.TRAINING_BUDGET,'checkpoint',return_value=0):
                    start=time.perf_counter();count=m._archive_history_payload(data,'ha_history_full')
                    elapsed=time.perf_counter()-start
                digest=hashlib.sha256(json.dumps(canonical(store),sort_keys=True).encode()).hexdigest()
                result[str(rows)]=dict(rows=count,seconds=elapsed,batches=len(durations),
                    max_batch_write_ms=max(durations),checksum=digest,disk_samples=store.storage_budget.snapshot()['disk_samples'])
            assert result['128']['checksum']==result['1024']['checksum']
            pairs.append(result)
        med={k:statistics.median(p[k]['seconds'] for p in pairs) for k in ('128','1024')}
        print(json.dumps(dict(scope='healthy local SQLite component; QoS sleeps disabled; not Pi, full training or device latency',
            pairs=pairs,median_seconds=med,relative_change_percent=(med['1024']/med['128']-1)*100,parity='PASS')))


if __name__=='__main__':main()
