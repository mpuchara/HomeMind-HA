"""Actual Engine.run/process scheduler cost with immediate held workers; no inference claim."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, passes, entities):
    sys.path[:0] = [str(source), str(source/'tests'), str(source/'adaptive_ai/src')]
    import support
    import engine as module
    from tools.benchmark_target_retries import fixture
    runtime = fixture(SimpleNamespace(), targets=4)
    # Time the production builtin dict fast path. Copy counts are verified by
    # behavioral tests; a dict subclass here would inflate copy cost artificially.
    for i in range(entities-len(runtime.state_map)):
        runtime.state_map[f'sensor.extra_{i}'] = {'state': str(i)}
    frame = [None]
    def submit(fn, agents, changed, snapshot, receipts):
        frame[0] = snapshot
        return SimpleNamespace(done=lambda: True)
    runtime.control_workers = SimpleNamespace(submit=submit)
    runtime.refresh_states = lambda: None
    runtime.context.home.expire = lambda stamp: None
    runtime.inference_enabled = threading.Event(); runtime.inference_enabled.set()
    runtime.startup_inference_not_before = 0.
    runtime.initial_inference_pending = False
    runtime.inference_scheduler = dict(event_passes=0, timer_passes=0, idle_skips=0)
    runtime._startup_warmup_step = lambda states: 0
    runtime._due_inference_targets = lambda: set()
    runtime._maybe_schedule_state_resync = lambda: None
    counter = [0]
    def finish():
        counter[0] += 1
        runtime.dirty_entities.add('binary_sensor.shared')
        if counter[0] >= passes:
            runtime.stop_event.set()
    runtime._schedule_housekeeping = finish
    runtime.dirty_entities.add('binary_sensor.shared')
    runtime.wake_event = SimpleNamespace(wait=lambda seconds: True, clear=lambda: None)
    with patch.object(module.STORE, 'event'), patch.object(module.TELEMETRY, 'start_trend_sampling'):
        wall, cpu = time.perf_counter(), time.thread_time()
        runtime.run()
        wall_ms, cpu_ms = (time.perf_counter()-wall)*1000, (time.thread_time()-cpu)*1000
    assert frame[0] is not None and frame[0][0] == runtime.state_map
    return dict(passes=counter[0], entities=entities,
        wall_ms=wall_ms, thread_cpu_ms=cpu_ms,
        frame_sha256=hashlib.sha256(repr(frame[0]).encode()).hexdigest())


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, default=ROOT)
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--passes', type=int, default=5000)
    p.add_argument('--entities', type=int, default=780)
    a = p.parse_args()
    if a.baseline_root:
        pairs = []
        for repeat in range(5):
            pair = {}
            for label in (('before','after') if repeat%2 == 0 else ('after','before')):
                source = a.baseline_root if label == 'before' else ROOT
                raw = subprocess.check_output([sys.executable, __file__, '--source-root', str(source),
                    '--passes', str(a.passes), '--entities', str(a.entities)], text=True)
                pair[label] = json.loads(raw.splitlines()[-1])
            assert pair['before']['frame_sha256'] == pair['after']['frame_sha256']
            pairs.append(pair)
        print(json.dumps(pairs))
    else:
        print(json.dumps(one(a.source_root, a.passes, a.entities)))
