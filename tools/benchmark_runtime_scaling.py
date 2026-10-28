"""Host scaling through shipped composition and real policies; never dispatch HA.

Fresh process per configuration. Synthetic demonstrations train normal policies;
confidence, predictions, decision gates and observer composition are unmodified.
This measures Engine ingress, not websocket/network or physical HA latency.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def run(source, count, events, independent=False, entities=780, debug=False,
        warmup=0, cadence=0., profile_dir=None):
    with tempfile.TemporaryDirectory(prefix='hm-scaling-') as data, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = data
        sys.path[:0] = [str(source/'tools'), str(source/'tests'), str(source/'adaptive_ai/src')]
        import support
        import benchmark_product_runtime as product
        from run_product_runtime_benchmark import _prepare_shipped_runtime
        from context import TemporalHistory
        from context_engine import ContextEngine
        from policy import MultiHorizonPolicy
        from telemetry import TELEMETRY, RUNTIME_DEBUG, rss_mb
        from storage import Store
        from runtime_background_compute import COMPUTE
        # The production ring intentionally keeps only 512 values. Retain the
        # entire finite benchmark window for descriptive tail comparisons.
        window_metrics = {key: [] for key in ('event_to_decision', 'context_shadow_capture')}
        observe_metric = TELEMETRY.observe
        def record_metric(name, value):
            observe_metric(name, value)
            if name in window_metrics:
                window_metrics[name].append(value)
        stack.enter_context(patch.object(TELEMETRY, 'observe', record_metric))
        profiles = []
        if profile_dir is not None:
            import profile
            # A main-thread profile misses Engine/target workers. Use independent
            # stdlib Python profilers: cProfile's monitoring tool ID is shared by
            # concurrent instances on Python 3.12+. These diagnostic runs are
            # separate from the unprofiled paired performance gate.
            original_thread_run = threading.Thread.run
            def profile_thread(thread):
                profiler = profile.Profile(timer=time.perf_counter)
                profiles.append(profiler)
                return profiler.runcall(original_thread_run, thread)
            stack.enter_context(patch.object(threading.Thread, 'run', profile_thread))
        store = Store(Path(data)/'benchmark.sqlite')
        stack.callback(store.stop_runtime_cache)
        for module in (product.engine_module, product.executor_module):
            stack.enter_context(patch.object(module, 'STORE', store))
        ha = stack.enter_context(patch.object(product.executor_module.HA, 'service',
            side_effect=AssertionError('Benchmark attempted physical dispatch')))
        stack.enter_context(patch.object(product.AUTOMATION_KNOWLEDGE, 'hints_for_target', return_value=(set(), [])))
        shipped, engine, descriptor = _prepare_shipped_runtime(store)
        states = {}
        agents = []
        for i in range(count):
            sensors = [f'binary_sensor.motion_{i if independent else 0}_{j}' for j in range(8)]
            states.update({eid: support.state(eid, 'off', device_class='motion') for eid in sensors})
            target = f'light.target_{i}'
            states[target] = support.state(target, 'off')
            agent = store.create_agent(support.agent(target_entity=target,
                input_entities=sensors, action_interval=0, mode='shadow'))
            agents.append((agent, sensors))
        for i in range(max(0, entities-len(states))):
            states[f'sensor.background_{i}'] = support.state(f'sensor.background_{i}', str(i%100))
        engine.state_map = states
        engine.context = ContextEngine(product.OPTIONS)
        engine.context.configure(states)
        engine.temporal_history = TemporalHistory(maxlen=96)
        trained = []
        training_start = time.perf_counter()
        for agent, sensors in agents:
            policy = MultiHorizonPolicy(agent, states, {}, set(), context_engine=engine.context)
            # Chronological demonstration fit then held-out calibration. No gate overrides.
            temporal = TemporalHistory(maxlen=96)
            base = time.time()-300
            correct = 0
            for tick in range(240):
                on = (tick//12)%2
                frame = dict(states)
                for eid in sensors:
                    frame[eid] = support.state(eid, 'on' if on else 'off', device_class='motion')
                    temporal.add(eid, base+tick, frame[eid])
                vector, _, _ = policy.features(frame, temporal, at_ts=base+tick)
                horizon = min(policy.horizons)
                if tick < 192:
                    policy.update(horizon, on, vector, 1., sample_ts=base+tick)
                else:
                    prediction = policy.predict(vector)
                    correct += int(int(float(prediction[0]['value']) >= .5) == on)
                    policy.heads[horizon].validate(on, vector, 1., sample_ts=base+tick)
            score = correct/48
            qualified = score > float(product.OPTIONS.get('candidate_benchmark_threshold', .78))
            if not qualified:
                raise AssertionError(('Demonstration model did not qualify', score))
            store.set_training_state(agent['id'], 'qualified', score=score, samples=48,
                detail={'source':'synthetic_chronological_demonstrations'})
            agent = store.get_agent_config(agent['id'])
            policy.agent = agent
            engine.models[agent['id']] = policy
            trained.append((agent, sensors))
        training_ms = (time.perf_counter()-training_start)*1000
        engine._refresh_agent_index(force=True)
        # Network, timer scheduling and maintenance are outside this isolated ingress workload.
        engine.refresh_states = lambda: None
        engine._startup_warmup_step = lambda _: 0
        engine._due_inference_targets = lambda: set()
        engine._schedule_housekeeping = lambda: None
        engine._maybe_schedule_state_resync = lambda: None
        engine.startup_inference_not_before = 0.
        engine.initial_inference_pending = False
        engine.inference_enabled.set()
        RUNTIME_DEBUG.set_enabled(debug, clear=True)
        samples, decisions = [], []
        original = engine.process_agent
        received = [0.]
        done = threading.Event()
        seen = set()
        def observe(agent, frame, changed=None):
            result = original(agent, frame, changed)
            rt = engine.runtime.get(agent['id'], {})
            if not rt.get('last_inference_ts'):
                raise AssertionError(('No real policy inference', agent['id'], rt.get('decision_state')))
            # Record blocked decisions as such; never force an ActionIntent.
            row = (int(agent['target_entity'].rsplit('_', 1)[1]), rt.get('last_prediction'),
                   rt.get('decision_state'), (rt.get('intent') or {}).get('status'))
            with engine.lock:
                if agent['id'] not in seen:
                    samples.append((time.perf_counter()-received[0])*1000)
                    decisions.append(row)
                    seen.add(agent['id'])
                    if len(seen) == count:
                        done.set()
            return result
        engine.process_agent = observe
        loop = threading.Thread(target=engine.run, daemon=True)
        loop.start()
        started, cpu = time.perf_counter(), time.process_time()
        try:
            for step in range(events+warmup):
                for future in list(engine.in_flight.values()):
                    future.result(30)
                if step == warmup:
                    samples.clear(); decisions.clear()
                    with TELEMETRY.lock:
                        TELEMETRY.samples.clear()
                    for values in window_metrics.values():
                        values.clear()
                    started, cpu = time.perf_counter(), time.process_time()
                seen.clear(); done.clear(); received[0] = time.perf_counter()
                inputs = sorted({sensors[0] for _, sensors in trained}) or ['sensor.background_0']
                for eid in inputs:
                    state = support.state(eid, 'on' if step%2 else 'off', device_class='motion')
                    stamp = datetime.now(timezone.utc).isoformat()
                    state.update(last_updated=stamp, last_changed=stamp)
                    engine.on_state_changed({'entity_id':eid, 'new_state':state})
                if count and not done.wait(30):
                    raise AssertionError((len(seen), count, engine.error))
                if not count:
                    time.sleep(.01)
                if cadence:
                    time.sleep(cadence)
        finally:
            for future in list(engine.in_flight.values()):
                future.result(30)
            engine.stop_event.set(); engine.wake_event.set(); loop.join(10)
            manager = getattr(engine, 'agent_candidates', None)
            if manager is not None:
                manager.stop()
            service = getattr(engine, 'context_tournament', None)
            if service is not None:
                service._deferred_observations.close(30)
                service._shadow_flush_event.set()
                service._shadow_writer_thread.join(30)
            for name in ('control_workers','poll_worker','registry_worker','housekeeping_worker'):
                getattr(engine, name).shutdown(wait=True, cancel_futures=True)
            COMPUTE.close()
            if profile_dir is not None:
                profile_dir.mkdir(parents=True, exist_ok=True)
                for index, profiler in enumerate(profiles):
                    profiler.dump_stats(str(profile_dir/f'thread-{index}.prof'))
        ha.assert_not_called()
        metrics = TELEMETRY.snapshot()
        try:
            import resource
            peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        except ImportError:
            peak_rss_mb = None
        raw = {key:list(values) for key, values in TELEMETRY.samples.items()}
        raw.update(window_metrics)
        canonical = [sorted(decisions[i:i+count]) for i in range(0,len(decisions),count)] if count else []
        def quantiles(values):
            values = sorted(values)
            return {f'p{p}_ms': values[min(len(values)-1,int((len(values)-1)*p/100))] if values else None for p in (50,95,99)}
        return dict(agents=count, events=events, warmup=warmup, cadence=cadence,
            independent=independent, debug=debug,
            composition=descriptor['entrypoint_chain'], normal_policy=True, confidence_override=False,
            synthetic_demonstration_training_ms=training_ms, ha_calls=ha.call_count,
            event_to_pipeline_return=quantiles(samples), samples=samples, telemetry_raw_ms=raw,
            telemetry=metrics, decision_rows=canonical,
            anonymous_trace=RUNTIME_DEBUG.anonymous_spans() if hasattr(RUNTIME_DEBUG,'anonymous_spans') else None,
            decisions_sha256=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest(),
            process_cpu_ms=(time.process_time()-cpu)*1000, wall_ms=(time.perf_counter()-started)*1000,
            rss_mb=rss_mb(), peak_rss_mb=peak_rss_mb, p99_sufficient=len(samples)>=1000,
            tournament=service.shadow_persistence_snapshot() if service else None,
            scope='host Engine ingress and composed pipeline; no HA socket/dispatch/ACK or Pi claim')


def scheduler(source, count, passes=200, entities=780):
    """Actual Engine.process with held workers isolates scheduler copy/allocation cost."""
    sys.path[:0] = [str(source), str(source/'tests'), str(source/'adaptive_ai/src')]
    import support
    from tools.benchmark_target_retries import fixture
    from types import SimpleNamespace
    from unittest.mock import Mock
    from engine import Engine
    runtime = fixture(SimpleNamespace(), targets=count)
    runtime._active_agents_for_changes = lambda changed: list(runtime.agent_configs.values())
    runtime.control_workers = Mock()
    runtime.control_workers.submit.side_effect = lambda *args: SimpleNamespace(done=lambda: True)
    for i in range(entities):
        eid = f'sensor.background_{i}'
        runtime.state_map[eid] = {'state':str(i)}
        runtime.entity_revisions[eid] = i
        runtime.dependency_agents[eid] = set(runtime.agent_configs)
    samples, cpus, digests = [], [], []
    for _ in range(passes):
        runtime.in_flight.clear()
        runtime.control_workers.reset_mock()
        wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
        Engine.process(runtime, runtime.state_map, {'binary_sensor.shared'})
        samples.append((time.perf_counter_ns()-wall)/1e6)
        cpus.append((time.thread_time_ns()-cpu)/1e6)
        calls = runtime.control_workers.submit.call_args_list
        digests.append(hashlib.sha256(repr([(c.args[1], c.args[2], c.args[3]) for c in calls]).encode()).hexdigest())
    return dict(agents=count, entities=entities, samples_ms=samples, thread_cpu_ms=cpus,
                snapshot_sha256=sorted(set(digests)), scope='actual scheduler; held workers; no inference/SQLite cost')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, default=ROOT)
    p.add_argument('--agents', type=int, default=3)
    p.add_argument('--events', type=int, default=20)
    p.add_argument('--entities', type=int, default=780)
    p.add_argument('--independent', action='store_true')
    p.add_argument('--debug', action='store_true')
    p.add_argument('--scheduler', action='store_true')
    p.add_argument('--warmup', type=int, default=0)
    p.add_argument('--cadence', type=float, default=0.)
    p.add_argument('--profile-dir', type=Path)
    a = p.parse_args()
    if not 0 <= a.agents <= 20 or not 2 <= a.events <= 5000 or not 32 <= a.entities <= 4096:
        p.error('bounded agents/events/entities required')
    result = (scheduler(a.source_root,a.agents,a.events,a.entities) if a.scheduler else
              run(a.source_root,a.agents,a.events,a.independent,a.entities,a.debug,
                  a.warmup,a.cadence,a.profile_dir))
    print('SCALING_JSON='+json.dumps(result,sort_keys=True))
