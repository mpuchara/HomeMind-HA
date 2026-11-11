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
import queue
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from runtime_scaling_probe import Probe, thread_cpu_snapshot, thread_cpu_delta


def run(source, count, events, independent=False, entities=780, debug=False,
        warmup=0, probe=False, legacy_storage_startup=False, legacy_lineage=False,
        candidates=False, training=False, correct=False, legacy_thread_settings=False, collector=False, sensor_count=None,
        event_timeout=30., matched_updates=0, matched_writes=1, correct_every=25, candidate_limit=None, history_preparation=False):
    if not 30. <= event_timeout <= 120.:
        raise ValueError('event observation timeout must be 30..120 seconds')
    if not 0 <= matched_updates <= 256 or not 0 <= matched_writes <= max(1, matched_updates):
        raise ValueError('bounded matched workload required')
    if matched_updates and (not training or not count or events + warmup > 100):
        raise ValueError('matched workload requires training, agents and at most 100 steps')
    if correct_every < 1 or (candidate_limit is not None and not 0 <= candidate_limit <= count):
        raise ValueError('invalid Correct cadence or Candidate count')
    with tempfile.TemporaryDirectory(prefix='hm-scaling-') as data, ExitStack() as stack:
        # Replay identical event/received wall-clock values in independent runs.
        # Scheduling, latency clocks and CPU accounting still use real time.
        logical_time = [1793275200.]
        stack.enter_context(patch('time.time', new=lambda: logical_time[0]))
        os.environ['ADAPTIVE_AI_DATA'] = data
        if not legacy_thread_settings:
            for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                os.environ[name] = '1'
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
        from runtime_threading import configure_runtime_threads, runtime_threading_snapshot
        thread_configuration = (runtime_threading_snapshot() if legacy_thread_settings
                                else configure_runtime_threads(product.OPTIONS))
        store = Store(Path(data)/'benchmark.sqlite')
        # Match main.initialize_runtime; composition installation alone omits
        # the bounded SQLite cache, WAL keeper and checkpoint worker.
        if not legacy_storage_startup:
            store.start_wal_keeper()
            store.start_runtime_cache()
            store.start_wal_checkpoint()
        stack.callback(store.stop_wal_keeper)
        stack.callback(store.stop_wal_checkpoint)
        stack.callback(store.stop_runtime_cache)
        for module in (product.engine_module, product.executor_module):
            stack.enter_context(patch.object(module, 'STORE', store))
        ha = stack.enter_context(patch.object(product.executor_module.HA, 'service',
            side_effect=AssertionError('Benchmark attempted physical dispatch')))
        stack.enter_context(patch.object(product.AUTOMATION_KNOWLEDGE, 'hints_for_target', return_value=(set(), [])))
        stack.enter_context(patch.dict(product.OPTIONS,universal_training_collector_enabled=collector))
        shipped, engine, descriptor = _prepare_shipped_runtime(store)
        states = {}
        agents = []
        for i in range(count):
            sensors = [f'binary_sensor.motion_{i if independent else 0}_{j}' for j in range(min(8,sensor_count) if sensor_count is not None else 8)]
            states.update({eid: support.state(eid, 'off', device_class='motion') for eid in sensors})
            target = f'light.target_{i}'
            states[target] = support.state(target, 'off')
            agent = store.create_agent(support.agent(target_entity=target,
                input_entities=sensors, action_interval=0, mode='shadow'))
            agents.append((agent, sensors))
        existing_sensors=sum(eid.startswith(('sensor.','binary_sensor.')) for eid in states)
        background_count=max(0,(sensor_count-existing_sensors) if sensor_count is not None else (entities-len(states)))
        if sensor_count is not None and existing_sensors>sensor_count:
            raise ValueError('requested sensor count smaller than independent routing fixture')
        for i in range(background_count):
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
            validation_correct = 0
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
                    validation_correct += int(int(float(prediction[0]['value']) >= .5) == on)
                    policy.heads[horizon].validate(on, vector, 1., sample_ts=base+tick)
            score = validation_correct/48
            qualified = score > float(product.OPTIONS.get('candidate_benchmark_threshold', .78))
            if not qualified:
                raise AssertionError(('Demonstration model did not qualify', score))
            store.set_training_state(agent['id'], 'qualified', score=score, samples=48,
                detail={'source':'synthetic_chronological_demonstrations'})
            agent = store.get_agent_config(agent['id'])
            policy.agent = agent
            engine.models[agent['id']] = policy
            if not legacy_lineage:
                from agent_candidate_lineage import _ensure_root
                store.save_model(agent['id'], policy.serialize())
                _ensure_root(store, agent['id'], generation_number=0)
            trained.append((agent, sensors))
        training_ms = (time.perf_counter()-training_start)*1000
        candidate_ids = []
        if candidates:
            from agent_candidate_lineage import _register_generation, _ensure_root
            from agent_candidates import refresh_candidate_ids_cache
            for agent, sensors in trained[:candidate_limit]:
                child = store.create_agent({**agent, 'id':None, 'mode':'paused', 'name':'Benchmark Candidate'})
                store.save_model(child['id'], engine.models[agent['id']].serialize())
                store.set_training_state(child['id'], 'qualified', score=1., samples=48,
                                         detail={'source':'copy_of_qualified_fixture_model'})
                parent = _ensure_root(store, agent['id'])
                _register_generation(store, agent['id'], parent, child['id'], 1,
                                     'synthetic_fixture', state='comparing')
                with store.lock, store.conn() as c:
                    c.execute('''INSERT INTO agent_candidates
                        (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                         dirty,queued_ts,comparison_json,offline_gate_json,updated_ts)
                        VALUES(?,?,1,'comparing','synthetic_fixture',0,0,0,?,'{}',?,?)''',
                        (agent['id'], child['id'], time.time(), '{"passed":true,"status":"passed"}', time.time()))
                candidate_ids.append(child['id'])
            refresh_candidate_ids_cache(store)
            engine.agent_candidates.invalidate_candidate_shadow_cache()
        # An isolated real policy fit plus normal durable model writes overlaps
        # Root inference. This is not a HistoryManager 14-day import benchmark.
        ui_http_ms, ingress_write_ms = [], []
        preparation_stats=dict(runs=0,errors=[],elapsed_seconds=[],wal_peak_bytes=0,snapshot_max_seconds=0.)
        if history_preparation:
            if not training or not trained:
                raise ValueError('History preparation requires concurrent policy training')
            # Same archived target/context records in both compared implementations.
            selected,sensors=trained[0]
            rows=[]
            for tick in range(10000):
                for entity in (selected['target_entity'],sensors[0]):
                    rows.append((entity,1700000000.+tick,'on' if tick%2 else 'off',{},None,'ha_history_full'))
            store.archive_batch(rows)
            checkpoint=store._wal_checkpoint_worker
            checkpoint.POLL_SECONDS=.05
            checkpoint.THRESHOLD_PAGES=1
            checkpoint.MAX_IDLE_SECONDS=1.
            from http.server import ThreadingHTTPServer
            from urllib.request import urlopen
            shipped.set_startup('ready',7,'Host WAL fixture ready',ready=True)
            ui_server=ThreadingHTTPServer(('127.0.0.1',0),shipped.Handler)
            ui_server.daemon_threads=True
            ui_server_thread=threading.Thread(target=ui_server.serve_forever,daemon=True)
            ui_server_thread.start()
            def close_ui():
                ui_server.shutdown()
                ui_server.server_close()
                ui_server_thread.join(5)
            stack.callback(close_ui)
            ui_url='http://127.0.0.1:'+str(ui_server.server_port)+'/api/agents'
            prep_root=Path(data)/'history_generations'
            prep_root.mkdir()
            def prepare_history():
                from history_recovery import prepare_generation
                began=time.perf_counter()
                destination=prep_root/('probe-'+str(preparation_stats['runs'])+'.db')
                with patch('storage_files.worker_storage_budget',return_value=store.storage_budget):
                    report=prepare_generation(store.path,destination,[selected['target_entity'],sensors[0]],
                        1700000000.,1700010000.,agent_id=selected['id'])
                preparation_stats['runs']+=1
                preparation_stats['elapsed_seconds'].append(time.perf_counter()-began)
                lifecycle=report.get('snapshot_lifecycle') or {}
                preparation_stats['snapshot_max_seconds']=max(preparation_stats['snapshot_max_seconds'],lifecycle.get('max_explicit_transaction_seconds',0.))
                destination.unlink()
                checkpoint.request_recovery()
        background_stop = threading.Event()
        background_stats = dict(updates=0, model_writes=0, correct_reads=0, correct_writes=0,
                                correct_gaps=0, errors=[])
        worker = None
        batches = queue.Queue(maxsize=1) if matched_updates else None
        batch_done = threading.Event()
        matched_timeline = []
        matched_offset_ns = time.perf_counter_ns()
        if training and trained:
            from training_budget import TRAINING_BUDGET
            agent, sensors = trained[0]
            trainer = store.create_agent({**agent, 'id':None, 'mode':'paused', 'name':'Isolated training fixture'})
            learning = MultiHorizonPolicy(trainer, states, {}, set(), model=engine.models[agent['id']].serialize(),
                                          context_engine=engine.context)
            vectors = []
            for value in (0, 1):
                frame = dict(states)
                for eid in sensors:
                    frame[eid] = support.state(eid, 'on' if value else 'off', device_class='motion')
                vectors.append(learning.features(frame, None, at_ts=time.time())[0])
            def update(tick, write):
                if history_preparation and tick%32==0:
                    prepare_history()
                TRAINING_BUDGET.checkpoint('scaling_policy_fit')
                learning.update(min(learning.horizons), tick%2, vectors[tick%2], 1., sample_ts=time.time())
                background_stats['updates'] += 1
                if write:
                    store.save_model(trainer['id'], learning.serialize())
                    background_stats['model_writes'] += 1

            def fit_background():
                TRAINING_BUDGET.begin()
                try:
                    if batches is None:
                        tick = 0
                        while not background_stop.is_set():
                            update(tick, tick % 32 == 0)
                            tick += 1
                    else:
                        write_offsets = {i * matched_updates // matched_writes for i in range(matched_writes)}
                        while True:
                            batch = batches.get()
                            if batch is None:
                                break
                            batch['trainer_start_ns'] = time.perf_counter_ns() - matched_offset_ns
                            for i in range(matched_updates):
                                update(batch['step'] * matched_updates + i, i in write_offsets)
                            batch['trainer_end_ns'] = time.perf_counter_ns() - matched_offset_ns
                            batch_done.set()
                            if background_stop.is_set():
                                break
                except BaseException as exc:
                    background_stats['errors'].append(repr(exc))
                    batch_done.set()
                finally:
                    TRAINING_BUDGET.end()
            worker = threading.Thread(target=fit_background, name='adaptive-ai-index-scaling-fixture', daemon=True)
        if correct:
            for state in states.values():
                engine._queue_archive_state(state, force=True)
            engine.flush_archive(force=True)
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
        diagnostic = Probe() if probe else None
        if diagnostic:
            diagnostic.install(stack, engine, store)
        # Finite benchmark window, independent of the production 512-sample ring.
        # Installed in both variants, with a hard per-metric bound.
        from collections import defaultdict
        complete_metrics = defaultdict(list)
        metric_lock = threading.Lock()
        collecting = [True]
        observe_metric = TELEMETRY.observe
        def retain_metric(name, value):
            observe_metric(name, value)
            with metric_lock:
                if collecting[0] and len(complete_metrics[name]) < 100000:
                    complete_metrics[name].append(float(value))
        stack.enter_context(patch.object(TELEMETRY, 'observe', retain_metric))
        samples, decisions = [], []
        input_digest = hashlib.sha256()
        input_digest.update(json.dumps(states, sort_keys=True).encode())
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
        if worker:
            worker.start()
        started, cpu = time.perf_counter(), time.process_time()
        thread_cpu_start = thread_cpu_snapshot()
        try:
            cold_samples = []
            for step in range(events+warmup):
                for future in list(engine.in_flight.values()):
                    future.result(event_timeout)
                if step == warmup:
                    cold_samples = list(samples)
                    samples.clear(); decisions.clear()
                    with metric_lock:
                        complete_metrics.clear()
                    if diagnostic:
                        diagnostic.reset()
                    started, cpu = time.perf_counter(), time.process_time()
                    thread_cpu_start = thread_cpu_snapshot()
                seen.clear(); done.clear(); received[0] = time.perf_counter()
                logical_time[0] = 1793275500.+step*.1
                if batches is not None:
                    batch_done.clear()
                    batch = dict(step=step, phase='warmup' if step < warmup else 'steady',
                                 release_ns=time.perf_counter_ns() - matched_offset_ns)
                    matched_timeline.append(batch)
                    batches.put_nowait(batch)
                inputs = sorted({sensors[0] for _, sensors in trained}) or ['sensor.background_0']
                for eid in inputs:
                    state = support.state(eid, 'on' if step%2 else 'off', device_class='motion')
                    stamp = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
                    state.update(last_updated=stamp, last_changed=stamp)
                    input_digest.update(json.dumps(state, sort_keys=True).encode())
                    engine.on_state_changed({'entity_id':eid, 'new_state':state})
                if count and not done.wait(event_timeout):
                    raise AssertionError(dict(seen=len(seen),expected=count,error=engine.error,
                        event_step=step,warmup=step<warmup,observation_timeout_seconds=event_timeout,
                        elapsed_ms=(time.perf_counter()-received[0])*1000))
                if not count:
                    time.sleep(.01)
                if batches is not None:
                    batch['inference_observed_ns'] = time.perf_counter_ns() - matched_offset_ns
                if history_preparation and step%10==0:
                    probe_start=time.perf_counter()
                    store.archive_batch([('sensor.ingress_probe',time.time(),str(step),{},None,'live')])
                    ingress_write_ms.append((time.perf_counter()-probe_start)*1000)
                    probe_start=time.perf_counter()
                    with urlopen(ui_url,timeout=5) as response:
                        assert response.status==200
                        json.loads(response.read())
                    ui_http_ms.append((time.perf_counter()-probe_start)*1000)
                    wal_path=Path(store.path+'-wal')
                    preparation_stats['wal_peak_bytes']=max(preparation_stats['wal_peak_bytes'],wal_path.stat().st_size if wal_path.exists() else 0)
                if correct and trained and step % correct_every == 0:
                    stamp = time.time()
                    manager = engine.agent_candidates
                    ref = trained[0][0]['id']
                    manager.workflow_correct_history(ref, stamp-300, stamp)
                    background_stats['correct_reads'] += 1
                    point = manager.workflow_correct_point(ref, stamp)
                    if point.get('desired') is not None and point.get('context_complete') and point.get('current') is not None:
                        result = manager.workflow_add_correct_label(ref, 1-float(point['desired']), stamp)
                        assert result['ok']
                        undone = manager.workflow_undo_correct_label(ref)
                        assert undone['ok'] and undone['undone_id'] == result['label_id']
                        with store.conn() as c:
                            assert c.execute('SELECT undone_ts FROM teaching_rl_labels WHERE id=?',
                                             (result['label_id'],)).fetchone()[0] is not None
                        background_stats['correct_writes'] += 1
                    else:
                        background_stats['correct_gaps'] += 1
                if batches is not None:
                    if not batch_done.wait(event_timeout) or background_stats['errors']:
                        raise AssertionError(('matched background batch failed', batch, background_stats))
        finally:
            background_stop.set()
            if batches is not None:
                try:
                    batches.put_nowait(None)
                except queue.Full:
                    # The pending batch belongs to an already-failed run.
                    background_stop.set()
            if worker:
                worker.join(10)
                if worker.is_alive():
                    raise AssertionError('Training fixture failed to stop')
            for future in list(engine.in_flight.values()):
                future.result(event_timeout)
            measurement_wall_ms = (time.perf_counter()-started)*1000
            measurement_cpu_ms = (time.process_time()-cpu)*1000
            cpu_threads = thread_cpu_delta(thread_cpu_start, thread_cpu_snapshot())
            measured_probe = diagnostic.snapshot() if diagnostic else None
            with metric_lock:
                collecting[0] = False
            engine.stop_event.set(); engine.wake_event.set(); loop.join(10)
            if loop.is_alive():
                raise AssertionError('Engine fixture did not stop')
            manager = getattr(engine, 'agent_candidates', None)
            if manager is not None:
                manager.stop()
                if manager.is_alive():
                    manager.join(30)
                    if manager.is_alive():
                        raise AssertionError('Candidate fixture worker did not stop')
            service = getattr(engine, 'context_tournament', None)
            if service is not None:
                service._deferred_observations.close(30)
                service._shadow_flush_event.set()
                service._shadow_writer_thread.join(30)
                if service._shadow_writer_thread.is_alive():
                    raise AssertionError('Tournament fixture writer did not stop')
            for name in ('control_workers','poll_worker','registry_worker','housekeeping_worker'):
                getattr(engine, name).shutdown(wait=True, cancel_futures=True)
            historical=getattr(engine,'universal_training_collector',None)
            if historical is not None and not historical.stop(10.):
                raise AssertionError('Collector fixture failed to stop')
            COMPUTE.close()
        ha.assert_not_called()
        if background_stats['errors']:
            raise AssertionError(background_stats['errors'])
        if matched_updates and (background_stats['updates'], background_stats['model_writes']) != (
                (events + warmup) * matched_updates, (events + warmup) * matched_writes):
            raise AssertionError(('matched background counts differ from plan', background_stats))
        if training and not background_stats['updates']:
            raise AssertionError('No concurrent policy training executed')
        metrics = TELEMETRY.snapshot()
        try:
            import resource
            peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        except ImportError:
            peak_rss_mb = None
        raw = dict(complete_metrics)
        canonical = [sorted(decisions[i:i+count]) for i in range(0,len(decisions),count)] if count else []
        def quantiles(values):
            values = sorted(values)
            return {f'p{p}_ms': values[min(len(values)-1,int((len(values)-1)*p/100))] if values else None for p in (50,95,99)}
        return dict(agents=count, events=events, independent=independent, debug=debug,
            observation_timeout_seconds=event_timeout,
            sensor_count=existing_sensors+background_count,entity_count=len(states),
            warmup_events=warmup, warmup_pipeline_samples_ms=cold_samples,
            production_storage_startup=not legacy_storage_startup,
            registered_root_lineage=not legacy_lineage,
            thread_configuration=thread_configuration, thread_cpu=cpu_threads,
            production_thread_settings=not legacy_thread_settings,
            concurrent_policy_training=training, correct_reads_enabled=correct,
            collector_enabled=collector,collector=(historical.diagnostics() if historical is not None else None),
            background_work=background_stats, history_preparation=preparation_stats, candidate_count=len(candidate_ids),
            ui_http=dict(endpoint='/api/agents',environment='loopback shipped Handler; not Supervisor ingress',
                         samples=len(ui_http_ms),max_ms=max(ui_http_ms,default=0.),**quantiles(ui_http_ms)),
            ingress_archive_write=dict(samples=len(ingress_write_ms),max_ms=max(ingress_write_ms,default=0.),**quantiles(ingress_write_ms)),
            matched_workload=(dict(updates_per_step=matched_updates, model_writes_per_step=matched_writes,
                                   queue_capacity=1, correct_every=correct_every, batches=matched_timeline)
                              if matched_updates else None),
            candidate_diagnostics=engine.agent_candidates.candidate_shadow_async_diagnostics(),
            input_sha256=input_digest.hexdigest(), event_clock='fixed synthetic 100ms increments; latency clocks real',
            storage_runtime=store.sqlite_snapshot(),
            probe=measured_probe,
            composition=descriptor['entrypoint_chain'], normal_policy=True, confidence_override=False,
            synthetic_demonstration_training_ms=training_ms, ha_calls=ha.call_count,
            event_to_pipeline_return=quantiles(samples), samples=samples, telemetry_raw_ms=raw,
            telemetry=metrics, decision_rows=canonical,
            anonymous_trace=RUNTIME_DEBUG.anonymous_spans() if hasattr(RUNTIME_DEBUG,'anonymous_spans') else None,
            decisions_sha256=hashlib.sha256(json.dumps(canonical,sort_keys=True).encode()).hexdigest(),
            process_cpu_ms=measurement_cpu_ms, wall_ms=measurement_wall_ms,
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
    p.add_argument('--probe', action='store_true', help='Opt-in bounded benchmark instrumentation')
    p.add_argument('--legacy-storage-startup', action='store_true', help='Reproduce old fixture omission')
    p.add_argument('--legacy-lineage', action='store_true', help='Reproduce unregistered fixture agents')
    p.add_argument('--candidates', action='store_true')
    p.add_argument('--history-preparation', action='store_true', help='Real history generation during policy training, on the same WAL source')
    p.add_argument('--training', action='store_true', help='Concurrent isolated real policy fit/model writes')
    p.add_argument('--correct', action='store_true', help='Read Correct history while inference/training run')
    p.add_argument('--legacy-thread-settings', action='store_true', help='Ablate omitted production BLAS/timeslice setup')
    p.add_argument('--collector', action='store_true', help='Opt-in historical collector; no training or HA authority')
    p.add_argument('--sensor-count',type=int,help='Exact sensor cardinality for the shared-input fixture')
    p.add_argument('--event-timeout',type=float,default=30.,help='Harness observation deadline in seconds (30..120); no runtime gate change')
    p.add_argument('--matched-updates', type=int, default=0, help='Isolated real policy updates released per ingress step')
    p.add_argument('--matched-writes', type=int, default=1, help='Evenly spaced model writes per matched batch')
    p.add_argument('--correct-every', type=int, default=25)
    p.add_argument('--candidate-limit', type=int)
    a = p.parse_args()
    if not 0 <= a.agents <= 200 or not 2 <= a.events <= 5000 or not 1 <= a.entities <= 4096:
        p.error('bounded agents/events/entities required')
    if a.sensor_count is not None and (not 1<=a.sensor_count<=1000 or a.scheduler):
        p.error('sensor-count requires a normal runtime case and 1..1000 sensors')
    if not 0 <= a.warmup <= 500:
        p.error('warmup must be 0..500')
    if not 30. <= a.event_timeout <= 120.:
        p.error('event-timeout must be 30..120 seconds')
    result = (scheduler(a.source_root,a.agents,a.events,a.entities) if a.scheduler else
              run(a.source_root,a.agents,a.events,a.independent,a.entities,a.debug,a.warmup,a.probe,a.legacy_storage_startup,a.legacy_lineage,
                  a.candidates,a.training,a.correct,a.legacy_thread_settings,a.collector,a.sensor_count,a.event_timeout,
                  a.matched_updates,a.matched_writes,a.correct_every,a.candidate_limit,a.history_preparation))
    print('SCALING_JSON='+json.dumps(result,sort_keys=True))
