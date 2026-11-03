"""HOST native collected coverage, real prune, bounded metadata and scoped replay.

Run each source in its own process (--source-root) for an unbiased module boundary.
An explicit dependent effect sensor is unrelated to the required motion history;
removing it must not shrink the historical universe or invalidate that history.
"""
import argparse
from contextlib import contextmanager
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
    def measure(name,action):
        statements=[];original=f.store.conn
        @contextmanager
        def conn():
            with original() as c:
                c.set_trace_callback(statements.append)
                try:yield c
                finally:c.set_trace_callback(None)
        cpu=time.process_time();start=time.perf_counter();rss=rss_mb()
        with patch.object(f.store,'conn',conn):result=action()
        rows.append(dict(name=name,wall_ms=(time.perf_counter()-start)*1000,
            cpu_ms=(time.process_time()-cpu)*1000,rss_mb=rss_mb(),rss_before_mb=rss,
            sqlite_trace_statements=len(statements),
            sqlite_selects=sum(s.lstrip().upper().startswith('SELECT') for s in statements),result=result))
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
