"""Real Engine event queue, candidate observer and guarded Control dispatch to an HA stub."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, events=24, continuous=False):
    scratch = tempfile.TemporaryDirectory(prefix='control-priority-global-')
    os.environ['ADAPTIVE_AI_DATA'] = scratch.name
    sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source / 'tests'), str(source / 'tools'), str(source)]
    import support
    import engine as engine_module
    import executor as executor_module
    import benchmark_shadow_context as fixture_module
    import context_tournament_promotion as promotion
    from context_tournament import install as install_tournament
    from storage import Store
    from runtime_threading import configure_runtime_threads
    configure_runtime_threads({})
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        store = Store(Path(directory) / 'runtime.db')
        stack.callback(store.stop_runtime_cache)
        for module in (engine_module, executor_module):
            stack.enter_context(patch.object(module, 'STORE', store))
        stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
        stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
        stack.enter_context(fixture_module.training_contract())
        runtime = engine_module.Engine()
        for name in ('control_workers', 'poll_worker', 'registry_worker', 'housekeeping_worker'):
            stack.callback(getattr(runtime, name).shutdown, wait=True, cancel_futures=True)
        agent = store.create_agent(support.agent(id='reaction', target_entity='light.room_0',
            input_entities=['binary_sensor.room_1'], mode='control', action_interval=0))
        detail = {'balanced': True, 'counts': {'samples': 80, 'correct': 80, 'per_action': {
            '0': {'samples': 40, 'correct': 40}, '1': {'samples': 40, 'correct': 40}}}}
        store.set_training_state(agent['id'], 'qualified', score=1., samples=80, detail=detail)
        store.update_agent(agent['id'], {'mode': 'control'})
        agent = store.get_agent_config(agent['id'])
        def reuse_engine(**kwargs):
            runtime.__dict__.update(kwargs)
            return runtime
        with patch.object(fixture_module, 'SimpleNamespace', reuse_engine), \
             patch.object(fixture_module, 'ContextTournament', install_tournament):
            fixture = fixture_module.Fixture(store, prefix=agent['id'])
        fixture.service.background_persistence_available = lambda: True
        fixture.agent = agent
        fixture.observe(); fixture.observe()
        runtime._refresh_agent_index(force=True)
        runtime.refresh_states = lambda: None
        runtime._startup_warmup_step = lambda _: 0
        runtime._due_inference_targets = lambda: set()
        runtime._schedule_housekeeping = lambda: None
        runtime._maybe_schedule_state_resync = lambda: None
        runtime.startup_inference_not_before = 0.
        runtime.initial_inference_pending = False
        runtime.inference_enabled.set()
        stack.enter_context(patch.object(executor_module.AUTOMATION_KNOWLEDGE, 'hints_for_target', return_value=(set(), [])))
        # Deterministic Root action/confidence fixture; feature extraction, candidate
        # predictions, scoring, scheduler and all Executor guards remain production.
        desired = [0]
        original_predict = fixture.live.predict
        def root_predict(features):
            _, _, arms, horizon, _, _ = original_predict(features)
            chosen = dict(next(row for row in arms if row['index'] == desired[0]), value=float(desired[0]))
            return chosen, .99, arms, horizon, 1., 0.
        stack.enter_context(patch.object(fixture.live, 'predict', side_effect=root_predict))
        completed, dispatched, all_decided = threading.Event(), threading.Event(), threading.Event()
        received = [0.]
        decisions, latencies, calls = [], [], []
        original_submit = runtime.executor.submit
        def submit(*args, **kwargs):
            result = original_submit(*args, **kwargs)
            decisions.append((float(args[0].desired_value), result['status'], result['reason'].split(':')[0]))
            latencies.append((time.perf_counter() - received[0]) * 1000)
            if continuous and len(decisions) < events:
                # A real next event arrives after this Root decision, before its
                # optional observer. No artificial sleeps or application clock.
                inject(len(decisions))
            elif len(decisions) == events:
                all_decided.set()
            completed.set()
            return result
        runtime.executor.submit = submit
        def ha_service(domain, name, data):
            calls.append(dict(domain=domain, service=name, data=data,
                              event_to_service_ms=(time.perf_counter() - received[0]) * 1000))
            dispatched.set()
            return []
        stack.enter_context(patch.object(executor_module.HA, 'service', side_effect=ha_service))
        def inject(i):
            desired[0] = int(i == events - 1)
            stamp = datetime.now(timezone.utc).isoformat()
            received[0] = time.perf_counter()
            runtime.on_state_changed({'entity_id': 'binary_sensor.room_1', 'new_state': {
                'entity_id': 'binary_sensor.room_1', 'state': 'on' if i % 2 == 0 else 'off',
                'attributes': {'device_class': 'occupancy'}, 'last_changed': stamp,
                'last_updated': stamp, 'context': {}}})
        loop = threading.Thread(target=runtime.run, daemon=True)
        loop.start()
        started, cpu = time.perf_counter(), time.process_time()
        try:
            if continuous:
                inject(0)
                assert all_decided.wait(max(30, events / 4)), (len(decisions), runtime.error)
            else:
                for i in range(events):
                    completed.clear()
                    inject(i)
                    assert completed.wait(10), (i, runtime.error)
            assert dispatched.wait(10), decisions
        finally:
            elapsed, used_cpu = time.perf_counter() - started, time.process_time() - cpu
            runtime.stop_event.set(); runtime.wake_event.set(); loop.join(10)
            assert not loop.is_alive()
            for future in list(runtime.in_flight.values()):
                future.result(10)
            fixture.service._shadow_flush_event.set()
            fixture.service._shadow_writer_thread.join(10)
            assert not fixture.service._shadow_writer_thread.is_alive()
        assert len(decisions) == events, decisions
        assert len(calls) == 1 and calls[0]['service'] == 'turn_on', calls
        assert decisions[-1][1] == 'ACCEPTED', decisions[-1]
        times = sorted(latencies)
        q = getattr(fixture.service, '_observation_scheduler', None)
        return dict(events=events, continuous_next_event=continuous, all_events_completed=True, actual_executor_control=True,
            root_prediction_fixture=True, ha_network=False, decisions_sha256=hashlib.sha256(json.dumps(decisions).encode()).hexdigest(),
            event_to_executor_median_ms=statistics.median(times), event_to_executor_p95_ms=times[int((len(times)-1)*.95)],
            event_to_service_dispatch_ms=calls[0]['event_to_service_ms'], service_calls=len(calls),
            process_cpu_seconds=used_cpu, elapsed_seconds=elapsed,
            observation_scheduler=q.snapshot() if q else None,
            debounce_ms=engine_module.OPTIONS['realtime_inference_debounce_ms'])


def paired(baseline, events, continuous=False):
    series = []
    for repeat in range(3):
        pair = {}
        for name in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
            source = baseline if name == 'before' else ROOT
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), '--one',
                '--source-root', str(source), '--events', str(events)] + (['--continuous'] if continuous else []), text=True)
            pair[name] = json.loads(output.strip().splitlines()[-1])
        assert pair['before']['decisions_sha256'] == pair['after']['decisions_sha256']
        pair['reaction_median_speedup'] = pair['before']['event_to_executor_median_ms'] / pair['after']['event_to_executor_median_ms']
        pair['service_dispatch_speedup'] = pair['before']['event_to_service_dispatch_ms'] / pair['after']['event_to_service_dispatch_ms']
        series.append(pair)
    return dict(synthetic=True, independent_sources=True, series=series,
        median_reaction_speedup=statistics.median(row['reaction_median_speedup'] for row in series),
        median_service_dispatch_speedup=statistics.median(row['service_dispatch_speedup'] for row in series),
        limit='Real Engine.run, event receipt/backpressure, Root features, four real Tournament candidates and guarded Control Executor to an HA stub. Root action/confidence is deterministic. Excludes shipped outer observers, HA network, physical ACK, Pi microSD and concurrent checkpoint I/O; not a device latency forecast.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one', action='store_true')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--events', type=int, default=24)
    parser.add_argument('--continuous', action='store_true', help='inject each next real event at the preceding Executor return')
    args = parser.parse_args()
    if args.events < 4 or (not args.one and args.baseline_root is None):
        parser.error('at least four events and baseline-root required (except --one)')
    print(json.dumps(one(args.source_root.resolve(), args.events, args.continuous) if args.one else paired(args.baseline_root.resolve(), args.events, args.continuous), indent=None if args.one else 2))
