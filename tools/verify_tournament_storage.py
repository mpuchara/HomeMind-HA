"""Independent sources, real SQLite storage quota and dispatched Tournament wrapper."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def one(source,steps):
    with tempfile.TemporaryDirectory(prefix='tournament-storage-') as directory:
        os.environ['ADAPTIVE_AI_DATA']=directory
        sys.path[:0]=[str(source/'adaptive_ai/src'),str(source/'tests')]
        import threading
        from test_context_tournament_shadow import ContextTournamentShadowTests
        f=ContextTournamentShadowTests();f.setUp()
        try:
            f.engine._inference_tls=threading.local();f.engine._inference_tls.state_revision=1
            normal=[]
            for value in ('off','on','off'):
                result=f._process(value)
                model=f.service._load_shadow_model(f.agent['id'],f.challenger,2)
                normal.append([result,model['samples'],model.get('active_correct'),model.get('shadow_correct')])
            samples_before=model['samples']
            b=f.store.storage_budget;limit=b.db_limit
            with f.store.conn() as c:b.observe_pages(c)
            b.db_limit=b.snapshot()['database_used_bytes'];assert b.sample()['reason']=='database_budget'
            original=f.service.observe_shadow
            with patch.object(f.service,'observe_shadow',wraps=original) as observe:
                started=time.perf_counter()
                results=[f._process('on' if i%2 else 'off') for i in range(steps)]
                elapsed=(time.perf_counter()-started)*1000
                calls=observe.call_count
            stats=f.service.shadow_persistence_snapshot().get('storage_admission',{})
            b.db_limit=limit;b.sample()
            f._process('on')
            fresh=f.service._load_shadow_model(f.agent['id'],f.challenger,2)
            no_cross_gap=fresh['samples']==samples_before
            return dict(steps=steps,normal_signature_sha256=hashlib.sha256(json.dumps(normal).encode()).hexdigest(),
                optional_observer_calls_while_paused=calls,live_results_ok=all(r=='production-result' for r in results),
                paused_loop_ms=elapsed,storage_admission=stats,
                preserved_samples=samples_before,no_cross_gap_score=no_cross_gap,
                ha_service_calls=f.engine.executor_calls,
                pass_all=calls==0 and no_cross_gap and all(r=='production-result' for r in results))
        finally:f.tearDown()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--one',action='store_true');p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--baseline-root',type=Path);p.add_argument('--steps',type=int,default=24)
    p.add_argument('--check',action='store_true');a=p.parse_args()
    if not 1<=a.steps<=256:p.error('--steps must be 1..256')
    if a.one:
        result=one(a.source_root.resolve(),a.steps)
        if a.check:assert result['pass_all'],result
    else:
        if a.baseline_root is None:p.error('--baseline-root required')
        result=dict(synthetic=True,independent_sources=True,
            limit='Production dispatched Tournament wrapper and real SQLite quota with fake Live/Executor fixture. Demonstrates optional-work admission, not physical HA/Pi latency. Normal observation math is unchanged.')
        for label,source in [('before',a.baseline_root),('after',a.source_root)]:
            output=subprocess.check_output([sys.executable,__file__,'--one','--source-root',str(source.resolve()),'--steps',str(a.steps)],text=True)
            result[label]=json.loads(output.strip().splitlines()[-1])
        assert result['after']['pass_all'],result
        assert result['before']['normal_signature_sha256']==result['after']['normal_signature_sha256']
    print(json.dumps(result,sort_keys=True))
