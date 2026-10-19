"""Host component probes for receipt startup and causal read reuse, not Pi acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rows',type=int,default=50000)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'adaptive_ai/src'))
    from detached_mapping_snapshots import DetachedMappingSnapshots
    results={}
    with tempfile.TemporaryDirectory() as tmp:
        db=sqlite3.connect(str(Path(tmp)/'audit.db'))
        db.execute('CREATE TABLE provenance_events(event_id TEXT PRIMARY KEY, received_time REAL, state_json TEXT)')
        payload=json.dumps(dict(state='on',audit='x'*2048))
        db.executemany('INSERT INTO provenance_events VALUES(?,?,?)',
                       ((str(i),float(i//3),payload) for i in range(a.rows)))
        db.commit()
        started=time.perf_counter()
        db.execute('CREATE INDEX idx_provenance_events_received_219 ON provenance_events(received_time DESC)')
        db.commit();creation_ms=(time.perf_counter()-started)*1000
        sql='SELECT * FROM provenance_events{} ORDER BY received_time DESC LIMIT 8192'
        timings={False:[],True:[]};checks={}
        for pair in range(5):
            for indexed in ((False,True) if pair%2==0 else (True,False)):
                started=time.perf_counter();rows=db.execute(sql.format('' if indexed else ' NOT INDEXED')).fetchall()
                timings[indexed].append((time.perf_counter()-started)*1000)
                checks[indexed]=hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        assert checks[False]==checks[True]
        results['receipt_order']=dict(rows=a.rows,payload_bytes=len(payload),result_rows=len(rows),
            first_index_creation_ms=creation_ms,baseline_ms=timings[False],indexed_ms=timings[True],
            baseline_median_ms=statistics.median(timings[False]),indexed_median_ms=statistics.median(timings[True]),
            exact_result_parity=True,query_plan=[list(row) for row in db.execute('EXPLAIN QUERY PLAN '+sql.format(''))])
        db.close()
    source={f'sensor.{i}':dict(entity_id=f'sensor.{i}',device_id=f'device.{i//4}',
                area_id=f'area.{i%9}',labels=['presence','local'],capabilities=dict(values=list(range(12)))) for i in range(779)}
    timings={False:[],True:[]}
    for pair in range(5):
        for fast in ((False,True) if pair%2==0 else (True,False)):
            cache=DetachedMappingSnapshots(max_bytes=8*1024**2,reuse_complete=fast)
            expected,charge=cache.capture(source)
            started=time.perf_counter()
            for _ in range(200):result,current_charge=cache.capture(source)
            timings[fast].append((time.perf_counter()-started)*1000/200)
            assert result==source and current_charge==charge and cache.snapshot()['bytes']<=cache.max_bytes
    results['stable_registry']=dict(entities=779,captures_per_run=200,baseline_ms=timings[False],complete_ms=timings[True],
        baseline_median_ms=statistics.median(timings[False]),complete_median_ms=statistics.median(timings[True]),exact_result_parity=True)
    sources=['provenance.py','detached_mapping_snapshots.py']
    report=dict(scope='Host component microbenchmarks, not event-to-decision, Pi startup or physical action latency',results=results,
        source_hashes={name:hashlib.sha256((root/'adaptive_ai/src'/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in sources})
    a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
