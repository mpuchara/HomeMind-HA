"""HOST native collected coverage, real prune, bounded metadata and scoped replay.

Run each source in its own process (--source-root) for an unbiased module boundary.
An explicit dependent effect sensor is unrelated to the required motion history;
removing it must not shrink the historical universe or invalidate that history.
"""
import argparse
from contextlib import contextmanager,nullcontext
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from benchmark_universal_affected_sensors import case


def probe(f,source,data,effects):
    from observation_contract import FeatureJournal
    from telemetry import rss_mb
    rows=[]
    def measure(name,action,instrument=True):
        counts=[0,0,0];original=f.store.conn
        def trace(sql):
            counts[0]+=1;counts[1]+=sql.lstrip()[:6].upper()=='SELECT';counts[2]+=len(sql)
        @contextmanager
        def conn():
            with original() as c:
                c.set_trace_callback(trace)
                try:yield c
                finally:c.set_trace_callback(None)
        cpu=time.process_time();start=time.perf_counter();rss=rss_mb()
        with (patch.object(f.store,'conn',conn) if instrument else nullcontext()):result=action()
        rows.append(dict(name=name,wall_ms=(time.perf_counter()-start)*1000,
            cpu_ms=(time.process_time()-cpu)*1000,rss_mb=rss_mb(),rss_before_mb=rss,
            sqlite_trace_statements=counts[0] if instrument else None,
            sqlite_selects=counts[1] if instrument else None,
            sqlite_trace_rendered_characters=counts[2] if instrument else None,result=result))
        return result
    def verify():
        try:
            with f.store.conn() as c:
                c.execute('BEGIN');current=source.dataset(c)
            return dict(eligible=len(current.samples),same_dataset=current.dataset_id==data.dataset_id)
        except ValueError as exc:return dict(eligible=0,error=str(exc))
    def prune(eid):
        journal=FeatureJournal(f.store,retention_hours=1000000,max_events_per_entity=0)
        with f.store.conn() as c:before=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
        journal.prune(entity_id=eid)
        with f.store.conn() as c:after=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
        return dict(deleted=before-after)
    measure('verify_initial',verify)
    measure('prune_unrelated',lambda:prune(effects[0]))
    unrelated=measure('verify_after_unrelated',verify)
    measure('prune_required',lambda:prune('binary_sensor.motion'))
    required=measure('verify_after_required',verify)
    granular=json.loads(source.raw)['coverage']['version']==3
    assert unrelated['eligible']==(len(data.samples) if granular else 0),unrelated
    assert required['eligible']==0,required
    if granular:assert unrelated['same_dataset'],unrelated
    # Real protected hard-cap stress crosses multiple 512-row candidate chunks.
    # Future receipt clocks are a separate disposable load, never used to repair
    # or qualify the original source. Count trace calls without retaining SQL.
    burst=2048;base=time.time()+10
    journal=FeatureJournal(f.store,retention_hours=1000000,max_events_per_entity=100000)
    def append():
        journal.record_batch([dict(entity_id=effects[0],state=dict(entity_id=effects[0],state=str(i),attributes={}),
            event_time=base+i*.001,received_time=base+i*.001,source='synthetic-retention-stress') for i in range(burst)])
        with f.store.conn() as c:
            c.execute('UPDATE feature_observation_events SET protected_until=?',(base+10000,))
        return dict(appended=burst)
    measure('append_protected_stress',append)
    def hard_cap():
        with f.store.conn() as c:before=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
        journal.global_event_limit=16;journal.prune()
        with f.store.conn() as c:
            actual=c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0]
            if granular:assert c.execute('SELECT live_count FROM universal_retention_meta').fetchone()[0]==actual
        assert actual==16,actual
        assert before-actual>=burst-16,(before,actual)
        return dict(deleted=before-actual,remaining=actual,protected_hard_cap=True,candidate_batch_limit=512 if granular else None)
    measure('protected_global_hard_cap_stress',hard_cap)
    # Trigger trace callbacks re-render bulk INSERT SQL many times. Measure a
    # distinct second future load without tracing too; never confuse observer
    # overhead with an uninstrumented production throughput claim.
    base+=100
    journal.global_event_limit=100000
    measure('append_protected_stress_no_trace',append,instrument=False)
    measure('protected_global_hard_cap_stress_no_trace',hard_cap,instrument=False)
    with f.store.conn() as c:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta_count=c.execute('SELECT COUNT(*) FROM universal_retention_entities').fetchone()[0] if 'universal_retention_entities' in names else 0
        try:
            sizes={r[0]:r[1] for r in c.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name")}
        except Exception:sizes=None
        pages=c.execute('PRAGMA page_count').fetchone()[0];page_size=c.execute('PRAGMA page_size').fetchone()[0]
    return dict(coverage_version=json.loads(source.raw)['coverage']['version'],operations=rows,
        samples_initial=len(data.samples),samples_after_unrelated=unrelated['eligible'],
        false_invalidations_after_unrelated=len(data.samples)-unrelated['eligible'],
        samples_after_required=required['eligible'],damage_metadata_rows=meta_count,
        damage_metadata_row_budget=10000,database_page_bytes=pages*page_size,sqlite_object_bytes=sizes,
        limits='Trace includes trigger-expanded statements and benchmark COUNT instrumentation; dbstat includes page slack. One envelope/entity can conservatively merge disjoint time ranges (unit test).')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):os.environ[name]='1'
    results=[]
    for item in args.cases.split(','):
        sensors,devices=map(int,item.split(':'))
        if not 1<=devices<sensors<=1000 or devices>200:parser.error('bounded cardinalities required')
        results.append(case(args.source_root.resolve(),sensors,devices,retention_probe=probe))
    raw=json.dumps(dict(scope='HOST components, synthetic declarations, native original clocks; not Pi acceptance',cases=results),indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
    print(raw)


if __name__=='__main__':main()
