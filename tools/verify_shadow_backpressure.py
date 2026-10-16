"""Source-paired real SQLite quota/Candidate reproduction; never contacts HA."""
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

ROOT = Path(__file__).resolve().parents[1]


def one(source, blocked):
    with tempfile.TemporaryDirectory(prefix='shadow-backpressure-') as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        sys.path[:0] = [str(source/'adaptive_ai/src'), str(source/'tests')]
        from test_candidate_shadow_context import CandidateShadowExactContextTests
        import agent_candidate_shadow_runtime as runtime
        f = CandidateShadowExactContextTests(); f.setUp()
        try:
            budget = f.store.storage_budget
            original_limit = budget.db_limit
            with f.store.conn() as c:
                budget.observe_pages(c)
            budget.db_limit = budget.snapshot()['database_used_bytes']
            assert budget.sample()['reason'] == 'database_budget'
            def observe(index):
                ts = 20 + index * .1
                states = f.states('on' if index % 2 else 'off')
                f.engine.runtime[f.root['id']] = dict(last_prediction=float(index % 2),
                    last_confidence=.88, last_inference_ts=ts-.01)
                f.manager.before_live_process(f.root, states)
                f.engine.runtime[f.root['id']]['last_inference_ts'] = ts
                f.engine.policy(f.root).features(states, None, at_ts=ts)
                f.manager.after_live_process(f.root, states)
                f.manager.drain_deferred_candidate_shadow(max_roots=1)
            for index in range(128): observe(index)
            before = f.manager.candidate_shadow_persistence_snapshot()
            with patch.object(runtime, '_predict_candidate', wraps=runtime._predict_candidate) as predict:
                with patch.object(f.store, 'event') as event:
                    started = time.perf_counter()
                    for index in range(128,128+blocked): observe(index)
                    elapsed = (time.perf_counter()-started)*1000
                policy_calls, error_events = predict.call_count, event.call_count
            full = f.manager.candidate_shadow_persistence_snapshot()
            diagnostics = f.manager.candidate_shadow_async_diagnostics()
            budget.db_limit = original_limit; budget.sample()
            with patch.object(f.engine, 'policy', side_effect=AssertionError('policy replay')):
                flushed = f.manager.flush_candidate_shadow_decisions(force=True)
            with f.store.conn() as c:
                rows = [tuple(r) for r in c.execute('SELECT ts,current,desired FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts', (f.generation_id,))]
            exact = rows == [(20+i*.1,0.,float(i%2)) for i in range(128)]
            observe(128+blocked)
            recovered = f.manager.candidate_shadow_persistence_snapshot()
            f.executor.service.assert_not_called()
            return dict(blocked_observations=blocked, blocked_policy_calls=policy_calls,
                error_events=error_events, inference_errors=diagnostics['errors'],
                observation_gaps=diagnostics.get('observation_gaps',0),
                elapsed_blocked_ms=elapsed, pending=full['pending'], bytes=full['bytes'],
                pending_bytes_unchanged=full['bytes']==before['bytes'],
                exact_flush_count=flushed, exact_retained_rows=exact,
                retained_rows_sha256=hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                recovery=recovered.get('admission',{}), ha_service_calls=0,
                pass_all=policy_calls==0 and error_events==0 and diagnostics['errors']==0
                    and exact and flushed==128 and full['pending']==128
                    and not recovered.get('admission',{}).get('paused',True))
        finally: f.tearDown()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--baseline-root',type=Path)
    parser.add_argument('--one',action='store_true')
    parser.add_argument('--blocked',type=int,default=64)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if not 1 <= args.blocked <= 512: parser.error('--blocked must be 1..512')
    if args.one:
        result=one(args.source_root.resolve(),args.blocked)
        if args.check: assert result['pass_all'], result
    else:
        if args.baseline_root is None: parser.error('--baseline-root required')
        result=dict(synthetic=True, independent_sources=True,
            limitation='Host reproduction with real SQLite page accounting and production Candidate pipeline. No user database, HA device or Raspberry Pi latency measured.')
        for label,source in [('before',args.baseline_root),('after',args.source_root)]:
            output=subprocess.check_output([sys.executable,__file__,'--one','--source-root',str(source.resolve()),'--blocked',str(args.blocked)],text=True)
            result[label]=json.loads(output.strip().splitlines()[-1])
        assert result['after']['pass_all'],result
        assert result['before']['retained_rows_sha256']==result['after']['retained_rows_sha256']
    print(json.dumps(result,sort_keys=True))
