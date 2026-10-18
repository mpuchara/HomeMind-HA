"""Compare exact journal protection results and overlapping-window write cost."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def measure(source):
    sys.path.insert(0, str(source/'adaptive_ai/src'))
    with tempfile.TemporaryDirectory() as folder:
        os.environ['ADAPTIVE_AI_DATA'] = folder
        for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
            os.environ[name] = '1'
        from storage import Store
        from observation_contract import FeatureJournal
        store = Store(Path(folder)/'windows.db')
        journal = FeatureJournal(store, clock=lambda: 1000.)
        entities = [f'sensor.bench_{i}' for i in range(9)]
        journal.record_batch([dict(entity_id=eid, state=dict(entity_id=eid, state=str(t), attributes={}),
            event_time=float(t), received_time=float(t), source='benchmark')
            for eid in entities for t in range(120)])
        windows = [dict(window_id=f'w{i}', agent_id='a', entities=entities,
            anchor_time=50.+i*.05, before=12., after=6.) for i in range(128)]
        start = time.perf_counter()
        journal.open_windows_batch(windows)
        elapsed = (time.perf_counter()-start)*1000
        with store.conn() as c:
            result = {table:[tuple(row) for row in c.execute('SELECT * FROM '+table+' ORDER BY 1')]
                for table in ('feature_observation_events','feature_windows','feature_window_entities')}
        return dict(ms=elapsed, result_sha256=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-root',type=Path)
    p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--worker',action='store_true')
    a=p.parse_args()
    if a.worker:
        print(json.dumps(measure(a.source_root)));return
    runs=[]
    for pair in range(3):
        for kind in (('baseline','current') if pair%2==0 else ('current','baseline')):
            source=a.baseline_root if kind=='baseline' else a.source_root
            result=subprocess.run([sys.executable,__file__,'--worker','--source-root',str(source)],
                capture_output=True,text=True)
            if result.returncode:raise RuntimeError(result.stdout[-2000:]+result.stderr[-4000:])
            row=json.loads(result.stdout.strip().splitlines()[-1]);row.update(kind=kind,pair=pair);runs.append(row)
    assert len({r['result_sha256'] for r in runs})==1
    print(json.dumps(dict(runs=runs,limitation='Journal component only, no agent/HA end-to-end claim.')))


if __name__=='__main__':main()
