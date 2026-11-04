"""Opt-in monotonic timeline for the existing composed benchmark.

Timestamps are perf_counter_ns, shared with Engine receipt timestamps. Capture
uses bounded per-thread rings on CPython. Inference threads never write SQLite
for this diagnostic. Intervals are inclusive and may overlap.
"""
import argparse
from collections import deque
from functools import wraps
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

import benchmark_runtime_scaling as harness


class StoreWait:
    """Preserve the underlying lock API and time acquisition attempts."""

    def __init__(self, lock, probe):
        self.lock = lock
        self.probe = probe

    def acquire(self, *args, **kwargs):
        start = time.perf_counter_ns()
        acquired = self.lock.acquire(*args, **kwargs)
        self.probe.append('store_lock_wait', start, time.perf_counter_ns(), acquired=acquired)
        return acquired

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.lock.release()

    def __getattr__(self, name):
        return getattr(self.lock, name)


class TimelineProbe:
    def __init__(self, capacity=8192, thread_limit=32):
        self.capacity = capacity
        self.thread_limit = thread_limit
        self.local = threading.local()
        self.rings = []
        self.start_ns = time.perf_counter_ns()

    def append(self, kind, start, end, **details):
        if not hasattr(self.local, 'ring'):
            ring = (threading.current_thread().name, deque(maxlen=self.capacity), [0])
            self.rings.append(ring)
            self.local.ring = ring if len(self.rings) <= self.thread_limit else None
            if self.local.ring is None:
                self.rings.remove(ring)
        if self.local.ring is not None:
            _, rows, count = self.local.ring
            rows.append((kind, start, end, details))
            count[0] += 1

    def wrap(self, stack, owner, method, name, details=lambda _args: {}):
        original = getattr(owner, method)

        @wraps(original)
        def wrapped(*args, **kwargs):
            fields = details(args)
            started = time.perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                self.append(name, started, time.perf_counter_ns(), **fields)

        stack.enter_context(patch.object(owner, method, wrapped))

    def reset(self):
        # Keep both phases, including initial backlog, in the bounded rings.
        self.steady_start_ns = time.perf_counter_ns()

    def snapshot(self):
        threads = []
        for name, ring, count in list(self.rings):
            rows = list(ring)
            threads.append(dict(thread=name, rows=rows, dropped=max(0, count[0]-len(rows))))
        return dict(clock='perf_counter_ns', start_ns=self.start_ns, inclusive=True,
                    steady_start_ns=getattr(self, 'steady_start_ns', None),
                    capacity_per_thread=self.capacity, thread_limit=self.thread_limit,
                    threads=threads)

    def install(self, stack, engine, store):
        from policy import MultiHorizonPolicy
        from storage_budget import BudgetCursor
        import tiny_mlp_shadow

        stack.enter_context(patch.object(store, 'lock', StoreWait(store.lock, self)))
        self.wrap(stack, engine, 'on_state_changed', 'ingress')
        self.wrap(stack, engine, 'process_agent', 'agent', lambda args: {'agent': args[0]['id']})
        self.wrap(stack, MultiHorizonPolicy, 'features', 'context_features')
        self.wrap(stack, MultiHorizonPolicy, 'predict', 'prediction')
        self.wrap(stack, MultiHorizonPolicy, 'update', 'training_update')
        self.wrap(stack, store, 'save_model', 'model_write')
        self.wrap(stack, store, 'get_agent_config', 'agent_config')
        self.wrap(stack, engine.executor, 'submit', 'intent_submit')
        self.wrap(stack, tiny_mlp_shadow, 'ensure_tables', 'tiny_shadow_schema')
        self.wrap(stack, BudgetCursor, 'execute', 'sqlite_execute')
        collector = getattr(engine, 'universal_training_collector', None)
        if collector is not None:
            self.wrap(stack, collector, '_write', 'historical_collector_write')
        tournament = getattr(engine, 'context_tournament', None)
        if tournament is not None:
            self.wrap(stack, tournament._deferred_observations, '_execute', 'tournament_observation')

        process = engine.process

        @wraps(process)
        def scheduler_pass(*args, **kwargs):
            started = time.perf_counter_ns()
            ready = engine.ready_target_changes.copy()
            receipt_times = engine.entity_event_received_perf.copy()
            oldest = min((receipt_times[eid] for changed in ready.values() for eid in tuple(changed)
                          if receipt_times.get(eid) is not None), default=None)
            futures = tuple(engine.in_flight.copy().values())
            done = sum(f.done() for f in futures)
            try:
                return process(*args, **kwargs)
            finally:
                self.append('scheduler_pass', started, time.perf_counter_ns(),
                            ready_at_entry=len(ready), registered_unfinished=len(futures)-done,
                            registered_done=done, ready_at_exit=len(engine.ready_target_changes),
                            oldest_ready_event_ns=int(oldest*1e9) if oldest is not None else None)

        stack.enter_context(patch.object(engine, 'process', scheduler_pass))
        submit = engine.control_workers.submit

        def timed_submit(fn, agents, changed, snapshot, received):
            requested = time.perf_counter_ns()
            fields = dict(target=agents[0]['target_entity'],
                          event_ns=min((int(v*1e9) for v in (received or {}).values()
                                        if v is not None), default=None))

            def entered():
                entry = time.perf_counter_ns()
                self.append('executor_queue', requested, entry, **fields)
                try:
                    return fn(agents, changed, snapshot, received)
                finally:
                    self.append('worker_active', entry, time.perf_counter_ns(), **fields)

            try:
                return submit(entered)
            finally:
                self.append('submit_call', requested, time.perf_counter_ns(), **fields)

        stack.enter_context(patch.object(engine.control_workers, 'submit', timed_submit))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--agents', required=True, type=int)
    parser.add_argument('--sensor-count', required=True, type=int)
    parser.add_argument('--warmup', required=True, type=int)
    parser.add_argument('--events', required=True, type=int)
    parser.add_argument('--matched-updates', type=int, default=0)
    parser.add_argument('--matched-writes', type=int, default=1)
    for name in ('candidates', 'correct', 'training', 'collector'):
        parser.add_argument('--' + name, action='store_true')
    args = parser.parse_args()
    if not 1 <= args.agents <= 200 or not 1 <= args.sensor_count <= 1000:
        parser.error('bounded agent and sensor counts required')
    if not 0 <= args.warmup <= 8 or not 2 <= args.events <= 40:
        parser.error('timeline requires at most 8 warmup and 40 steady steps')
    with patch.object(harness, 'Probe', TimelineProbe):
        result = harness.run(args.source_root, args.agents, args.events, warmup=args.warmup,
                             sensor_count=args.sensor_count, candidates=args.candidates,
                             correct=args.correct, training=args.training, collector=args.collector,
                             probe=True, matched_updates=args.matched_updates,
                             matched_writes=args.matched_writes)
    print('SCALING_JSON='+json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
