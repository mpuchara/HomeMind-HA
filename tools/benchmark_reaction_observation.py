"""Compare decision observation and two explicit-feedback signatures in real sources.

This measures CPU work before Executor, with Teach + Preference facts requiring
matching. It is not an HA round trip or a device reaction benchmark.
"""
import argparse
from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, passes):
    sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source)]
    from tools.benchmark_shadow_context import Fixture, STAMP, training_contract
    from storage import Store
    import context_tournament_promotion as promotion
    import teaching
    from shared_inference_context import shared_inference_temporal
    optimized = (source / 'adaptive_ai/src/decision_observation.py').exists()
    if optimized:
        from decision_observation import decision_observation
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        stack.enter_context(training_contract())
        stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
        stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
        stack.enter_context(patch('time.time', return_value=STAMP))
        f = Fixture(Store(Path(tmp)/'bench.db'))
        from collections import deque
        for eid,rows in f.temporal.samples.items():
            f.temporal.samples[eid] = deque(list(rows)[-24:], maxlen=24)
        rows,times,calls = [],[],[0]
        features = f.live.features
        def counted(*args,**kw):
            calls[0] += 1
            return features(*args,**kw)
        f.live.features = counted
        for _ in range(passes):
            started = time.perf_counter()
            temporal = shared_inference_temporal(f.temporal, STAMP, home_provider=f.home)
            observation = f.live.features(f.states,temporal,at_ts=STAMP)
            prediction = f.live.predict(observation[0])
            with (decision_observation(f.live,f.states,temporal,STAMP,observation) if optimized else nullcontext()):
                signatures = [teaching.signature(f.live,f.states,temporal,STAMP) for _ in range(2)]
            times.append((time.perf_counter()-started)*1000)
            rows.append((observation, prediction, signatures))
        return dict(passes=passes, feature_calls=calls[0], median_ms=statistics.median(times),
                    total_ms=sum(times),sha256=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest())


def paired(baseline,passes):
    pairs=[]
    for n in range(3):
        pair={}
        for label in (('previous','current') if n % 2 == 0 else ('current','previous')):
            source = baseline if label == 'previous' else ROOT
            pair[label]=json.loads(subprocess.check_output([sys.executable,str(Path(__file__).resolve()),'--one','--source-root',str(source),'--passes',str(passes)],text=True))
        assert pair['previous']['sha256']==pair['current']['sha256']
        assert pair['previous']['feature_calls']==passes*3
        assert pair['current']['feature_calls']==passes
        pair['speedup']=pair['previous']['total_ms']/pair['current']['total_ms']
        pairs.append(pair)
    return dict(baseline_version=json.loads((baseline / 'adaptive_ai/BUILD_INFO.json').read_text(encoding='utf-8'))['version'],
                current_version=json.loads((ROOT / 'adaptive_ai/BUILD_INFO.json').read_text(encoding='utf-8'))['version'],synthetic=True,
                real_policy_and_both_signature_builds=True,passes=passes,series=pairs,
                speedup_median=statistics.median(x['speedup'] for x in pairs),
                limit='Explicit-feedback signature component before Executor; excludes ingress, queue, SQL guards, HA and device. No improvement expected from signature reuse when no facts need matching.')


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--one',action='store_true');ap.add_argument('--source-root',type=Path,default=ROOT);ap.add_argument('--baseline-root',type=Path);ap.add_argument('--passes',type=int,default=64);a=ap.parse_args()
    if a.passes < 1 or (not a.one and a.baseline_root is None):
        ap.error('positive passes and baseline-root required (except --one)')
    print(json.dumps(one(a.source_root.resolve(),a.passes) if a.one else paired(a.baseline_root.resolve(),a.passes),indent=2))
