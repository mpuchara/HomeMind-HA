"""Measure native historical evidence on a disposable synthetic Engine/Store."""
import argparse
from collections import Counter
from contextlib import closing, contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
from queue import Empty, Full, Queue
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch

from runtime_scaling_probe import Probe, thread_cpu_delta, thread_cpu_snapshot
from universal_e2e_fixture import OriginalEvidenceScenario, ProducerClock, state


MODES = ("positive", "normal", "busy", "overload", "off", "incomplete")
SCALES = "5:1,50:10,200:30,1000:200"


def quantiles(values):
    ordered = sorted(values)
    result = {f"p{p}_ms": ordered[(len(ordered)-1)*p//100] if ordered else None
              for p in (50, 95, 99)}
    result["max_ms"] = ordered[-1] if ordered else None
    return result


def wait_for_writer(collector, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        with collector.cv:
            if not collector.pending and collector.pending_gap is None:
                return
        if not collector.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError(f"Collector did not drain: {collector.diagnostics()}")
        time.sleep(.005)


def inventory(scenario):
    store = scenario.store
    with store.conn() as conn:
        rows = conn.execute("""
            SELECT d.body, p.agent_id, p.created_time, p.model_version,
                   p.model_revision, p.generation_id
            FROM universal_collection_decisions d
            LEFT JOIN provenance_decisions p ON p.decision_id=d.id
        """)
        counts = Counter()
        reasons = Counter()
        for body, aid, stamp, version, model, generation in rows:
            decision = json.loads(body)
            evidence = decision.get("predecision")
            intent = decision["intent"]
            counts["written_decisions"] += 1
            counts["original_masks"] += bool(evidence and evidence.get("legal_mask"))
            counts["complete_predecision"] += bool(evidence)
            counts["incomplete_predecision"] += not evidence
            counts["affected_sensor_evidence"] += decision.get("affected_sensor_status") == "complete"
            counts["v2_receipts"] += (decision.get("receipt") or {}).get("version") == 2
            counts["valid_provenance"] += (
                (aid, stamp, version, model) ==
                (intent["agent_id"], intent["created_at"], intent["policy_version"], intent["model_revision"])
                and (not evidence or generation == evidence["generation_id"])
            )
            if decision.get("missing_proof"):
                reasons[decision["missing_proof"]] += 1
        segments = [dict(row) for row in conn.execute(
            "SELECT start,through,closed,reason,retention_epoch FROM universal_collection_segments ORDER BY start")]
        gaps = [dict(row) for row in conn.execute(
            "SELECT at,reason FROM universal_collection_gaps ORDER BY id")]
        counts["journal_rows"] = conn.execute("SELECT COUNT(*) FROM feature_observation_events").fetchone()[0]
        retention_epoch = conn.execute("SELECT retention_epoch FROM universal_collection_meta").fetchone()[0]
    return dict(counts=counts, missing_proof=reasons, segments=segments, gaps=gaps, retention_epoch=retention_epoch)


def qualify(scenario, through, *, run_lifecycle=False, current_labels=None):
    from universal_history_collector import source_from_collection
    started = time.perf_counter()
    try:
        with scenario.store.conn() as conn:
            conn.execute("BEGIN")
            source = source_from_collection(
                conn, [a["id"] for a in scenario.agents], start=scenario.start, through=through)
            export_ms = (time.perf_counter()-started)*1000
            doc = json.loads(source.raw)
            dataset = source.dataset(conn)
    except ValueError as exc:
        return dict(status="rejected_source", reason=str(exc), eligible=0,
                    elapsed_ms=(time.perf_counter()-started)*1000)
    result = dict(status="qualified_source", eligible=len(dataset.samples),
                receipts=len(doc["decisions"]), coverage_version=doc["coverage"]["version"],
                rejected=Counter(reason for _, reason in dataset.rejected),
                dataset_id=dataset.dataset_id, supervision_revision=dataset.supervision_revision,
                source_id=source.source_id, export_ms=export_ms,
                elapsed_ms=(time.perf_counter()-started)*1000)
    if current_labels is not None:
        result["current_window_rejected"] = Counter(
            reason for event_id, reason in dataset.rejected if event_id in current_labels)
    if run_lifecycle:
        result["lifecycle"] = validate_offline(scenario, source, dataset)
    return result


def validate_offline(scenario, source, dataset):
    from universal_checkpoint_lifecycle import CheckpointLifecycle
    from universal_scorer_training import TrainingConfig, context_from_sample

    clocks = sorted({sample.observation_timestamp for sample in dataset.samples})
    if len(clocks) < 2:
        return dict(status="insufficient_evidence", reason="fewer than two qualified decision clocks")
    cut = len(clocks)*2//3
    split = scenario.split() if scenario.frames else dict(
        train_until=clocks[cut-1], holdout_from=clocks[cut], holdout_until=clocks[-1])
    service = CheckpointLifecycle(scenario.store)
    started = time.perf_counter()
    trained = service.rebuild(source, parent_seed=1, encoder_seed=41, config=TrainingConfig(epochs=24), **split)
    result = dict(status=trained["status"], reason=trained.get("reason"), updates=trained.get("updates"),
                  training_seconds=time.perf_counter()-started, split=split)
    if trained["status"] == "optimization_complete":
        published = service.publish(trained["checkpoint"], source)
        result.update(publication_status=published["status"], validation=published.get("report"))
        if published["status"] == "published":
            model = service.load(published["artifact_id"], source)
            projection = model.rank_context(context_from_sample(dataset.samples[-1]))
            if projection["control_eligible"] or projection["promotion_eligible"]:
                raise AssertionError("Offline-only artifact became control or promotion eligible")
            result.update(artifact_id=published["artifact_id"], guarded_offline_inference=projection)
    return result


def seed_candidates(scenario, count=None):
    from agent_candidate_lineage import _ensure_root, _register_generation
    from agent_candidates import install, refresh_candidate_ids_cache
    from agent_candidate_shadow_runtime import install as install_shadow
    from agent_candidate_shadow_context import install as install_shadow_context
    store, engine = scenario.store, scenario.engine
    def offline_http(*args):
        raise AssertionError("Benchmark HTTP handler must never run")

    handler = type("OfflineHandler", (), {"do_GET": offline_http, "do_POST": offline_http})
    manager = install(SimpleNamespace(STORE=store, ENGINE=engine, Handler=handler,
                                      _final_explicit_http_only=True), start_worker=False)
    install_shadow(manager)
    install_shadow_context(manager)
    for agent in scenario.agents[:count]:
        child = store.create_agent({**agent, "id": None, "mode": "paused", "name": "Synthetic Candidate"})
        store.save_model(child["id"], engine.models[agent["id"]].serialize())
        store.set_training_state(child["id"], "qualified", score=1., samples=48,
                                 detail={"source": "synthetic copied policy"})
        parent = _ensure_root(store, agent["id"])
        _register_generation(store, agent["id"], parent, child["id"], 1,
                             "synthetic_fixture", state="comparing")
        with store.conn() as conn:
            conn.execute("""INSERT INTO agent_candidates
                (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                 dirty,queued_ts,comparison_json,updated_ts)
                VALUES(?,?,1,'comparing','synthetic_fixture',0,0,0,?,'{}',?)""",
                (agent["id"], child["id"], time.time(), time.time()))
    refresh_candidate_ids_cache(store)
    engine.agent_candidates.invalidate_candidate_shadow_cache()


def record_feedback(scenario, tick, desired, intent, *, undo=False):
    label = f"sustained:{tick}"
    scenario.feedback.record(agent_id=intent["agent_id"], selected_ts=intent["created_at"],
                             source="correct", correct_action=desired, feedback_id=label,
                             decision_id=intent["intent_id"], episode_id="continuous-stream")
    if undo:
        scenario.feedback.undo(label)


def busy_work(scenario, stop, stats, feedback_jobs=None, feedback_done=None):
    from policy import MultiHorizonPolicy
    from training_budget import TRAINING_BUDGET
    engine, store = scenario.engine, scenario.store
    agent = scenario.agents[0]
    model = MultiHorizonPolicy(agent, engine.state_map, {}, set(),
                               model=engine.models[agent["id"]].serialize(), context_engine=engine.context)
    features = model.features(engine.state_map, None, at_ts=time.time())[0]
    tick = 0
    TRAINING_BUDGET.begin()
    try:
        while not stop.is_set() or (feedback_jobs is not None and not feedback_jobs.empty()):
            if feedback_jobs is not None:
                try:
                    job = feedback_jobs.get_nowait()
                except Empty:
                    pass
                else:
                    record_feedback(scenario, *job, undo=True)
                    feedback_done.add(job[0])
                    stats["correct_undo_worker"] += 1
            TRAINING_BUDGET.checkpoint("qualified_evidence_fixture")
            model.update(min(model.horizons), tick % 2, features, 1., sample_ts=time.time())
            stats["policy_updates"] += 1
            if tick % 32 == 0:
                # A disposable policy copy, never installed into the observed agent.
                store.save_model(agent["id"], model.serialize())
                stats["model_writes"] += 1
            if tick % 8 == 0:
                engine.agent_candidates.drain_deferred_candidate_shadow(max_roots=4)
                engine.agent_candidates.drain_candidate_shadow_events(max_roots=4)
            if tick % 128 == 0:
                scenario.journal.prune()
                stats["prune_calls"] += 1
            tick += 1
            stop.wait(.005)
    except Exception as exc:
        stats["error"] = repr(exc)
    finally:
        TRAINING_BUDGET.end()


def publish_event(scenario, entity, value, **attributes):
    clock = scenario.clock
    ingress = time.perf_counter()
    stamp = clock()
    scenario.engine.on_state_changed(dict(
        entity_id=entity, new_state=state(entity, value, stamp, **attributes),
        _hm_ws_received_ts=stamp, _hm_ws_received_perf=ingress))
    return stamp


def stream(scenario, mode, ticks, period, writer_active, *, tick_offset=0, on_intent=None, fanout=1,
           timeline=None, inference=None):
    from settings import OPTIONS
    engine, collector = scenario.engine, scenario.collector
    timings, intents, outstanding = [], [], {}
    counters = Counter()
    event_count = 0
    fanout = min(fanout, len(scenario.agents))

    def infer(agent, frame, received_ts, decision_index):
        if timeline is not None:
            timeline.append(("agent_started", decision_index, time.perf_counter_ns()))
            inference.index = decision_index
            cpu = time.thread_time_ns()
        previous = engine.runtime.get(agent["id"], {}).get("last_intent")
        try:
            result = engine.process_agent(agent, frame, {"binary_sensor.motion"})
        finally:
            if timeline is not None:
                timeline.append(("agent_finished", decision_index, time.perf_counter_ns(),
                                 time.thread_time_ns()-cpu))
                del inference.index
        intent = engine.runtime[agent["id"]].get("last_intent")
        if not intent or intent is previous:
            return None, None, result["status"]
        return intent, (intent["created_at"]-received_ts)*1000, result["status"]

    def reap(block=False):
        for target, (future, desired, tick) in list(outstanding.items()):
            if not block and not future.done():
                continue
            intent, latency, status = future.result(timeout=30)
            counters[status] += 1
            if intent:
                timings.append(latency)
                intents.append((tick, desired, intent))
                if on_intent is not None:
                    on_intent(tick, desired, intent)
            del outstanding[target]

    begin = time.perf_counter()
    with patch.dict(OPTIONS, training_realtime_event_priority_seconds=.30):
        for step in range(ticks):
            tick = step+tick_offset
            if mode != "overload":
                time.sleep(max(0, begin+step*period-time.perf_counter()))
            reap()
            desired = tick % 2
            counters["ingress_while_writer_active"] += writer_active.is_set()
            if timeline is not None:
                timeline.append(("ingress", tick, time.perf_counter_ns(), writer_active.is_set()))
            received_ts = publish_event(scenario, "binary_sensor.motion", "on" if desired else "off",
                                    device_class="motion")
            event_count += 1
            frame = engine.state_map.copy()
            if timeline is not None:
                timeline.append(("inference_scheduled", tick, time.perf_counter_ns()))
            for lane in range(fanout):
                decision_index = tick*fanout+lane
                agent = scenario.agents[decision_index % len(scenario.agents)]
                target = agent["target_entity"]
                if target not in outstanding:
                    if timeline is not None:
                        timeline.append(("worker_submitted", decision_index, time.perf_counter_ns()))
                    future = engine.control_workers.submit(infer, agent, frame, received_ts, decision_index)
                    engine.in_flight[target] = future
                    outstanding[target] = (future, desired, decision_index)
                else:
                    counters["target_still_running"] += 1
            counters["peak_inference_outstanding"] = max(counters["peak_inference_outstanding"], len(outstanding))
            counters["ingress_while_writer_active"] += writer_active.is_set()
            if timeline is not None:
                timeline.append(("lux_ingress", tick, time.perf_counter_ns(), writer_active.is_set()))
            publish_event(scenario, "sensor.lux", 1000 if desired else 5, device_class="illuminance")
            event_count += 1
            if mode != "off":
                collector.barrier()
            if mode == "overload":
                for i in range(8):
                    eid = scenario.sensor_ids[(tick*8+i) % len(scenario.sensor_ids)]
                    cls = engine.state_map[eid]["attributes"]["device_class"]
                    value = ("on" if i%2 else "off") if cls == "motion" else tick*8+i
                    counters["ingress_while_writer_active"] += writer_active.is_set()
                    publish_event(scenario, eid, value, device_class=cls)
                    event_count += 1
        ingress_seconds = time.perf_counter()-begin
        reap(block=True)
    return event_count, intents, timings, dict(counters), ingress_seconds


def collect_case(path, sensors, devices, mode, ticks, period, *, run_lifecycle=False, fanout=1, candidates=None, profile_prepare=False):
    from storage import Store
    from telemetry import rss_mb
    from universal_history_collector import foreground_idle
    from universal_predecision import MAX_AGENTS

    store = Store(path)
    # Setup retains the original deterministic fixture clocks. During the
    # stream the producer reads a continuously advancing monotonic clock.
    base_clock = ProducerClock()
    anchor = None

    class Clock:
        set = base_clock.set

        def __call__(self):
            return base_clock() if anchor is None else base_clock()+(time.perf_counter()-anchor)

    with OriginalEvidenceScenario(store, sensors=sensors, devices=devices, clock=Clock()) as scenario, ExitStack() as stack:
        collector, engine = scenario.collector, scenario.engine
        background_stats = Counter()
        feedback_done = set()
        feedback_jobs = Queue(maxsize=32)

        def submit_feedback(tick, desired, intent):
            if tick % 8 == 0:
                try:
                    feedback_jobs.put_nowait((tick, desired, intent))
                except Full:
                    background_stats["correct_queue_full"] += 1
        candidate_count = (devices if mode == "busy" else 0) if candidates is None else candidates
        if not 0 <= candidate_count <= devices:
            raise ValueError("Candidate count must be between zero and the root count")
        if mode == "busy" or candidate_count:
            seed_candidates(scenario, candidate_count)
        with store.conn() as conn:
            durable_configs = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        if mode == "off":
            engine.universal_training_collector = None
            engine.executor.device_agents.universal_training_collector = None
        if mode == "incomplete":
            collector.predecision = None
        if mode == "overload":
            collector.capacity = 32  # Fault profile, after the original seed.
        errors = Counter()
        prepare = scenario.evidence.prepare_one
        preparation_probe = Probe(capacity=32768 if profile_prepare else 2048)
        timeline = [] if profile_prepare else None
        inference = threading.local() if profile_prepare else None
        materialization = Counter()
        descriptor_state_reads = Counter()
        cpu_profiles = []
        writer_cpu_profiles = []
        source_revisions = []
        if profile_prepare:
            from device_agents import DeviceAgentService
            from intent import ActionIntent
            from training_budget import TRAINING_BUDGET
            import universal_predecision
            import universal_affected_sensors
            import device_agents

            native_revision = universal_predecision.revision
            def measured_revision(conn):
                result = native_revision(conn)
                if getattr(preparing, 'active', False):
                    source_revisions.append(result)
                return result
            stack.enter_context(patch.object(universal_predecision, 'revision', measured_revision))

            preparing = threading.local()
            original_options = device_agents.target_options_for_state
            original_descriptor = DeviceAgentService.descriptor

            def measured_options(state):
                if getattr(preparing, "active", False):
                    descriptor_state_reads[str(state.get("entity_id"))] += 1
                return original_options(state)

            def measured_descriptor(service, agent):
                result = original_descriptor(service, agent)
                if getattr(preparing, "active", False):
                    # The descriptor and mapping reads still use the original
                    # detached database. Keep the full RAM snapshot unchanged.
                    timeline.append(("descriptor_scope", agent["id"], time.perf_counter_ns(),
                                     tuple(result["entities"]), result["logical_device_id"]))
                return result

            stack.enter_context(patch.object(device_agents, "target_options_for_state", measured_options))
            stack.enter_context(patch.object(DeviceAgentService, "descriptor", measured_descriptor))
            create_intent = ActionIntent.create

            def measured_intent(*args, **kwargs):
                intent = create_intent(*args, **kwargs)
                if hasattr(inference, "index"):
                    timeline.append(("intent_created", inference.index, time.perf_counter_ns(),
                                     intent.created_at))
                return intent

            stack.enter_context(patch.object(ActionIntent, "create", measured_intent))

            def observe_stage(owner, method, stage):
                original = getattr(owner, method)

                def measured(*args, **kwargs):
                    if getattr(preparing, "active", False):
                        with preparation_probe.span(stage):
                            return original(*args, **kwargs)
                    return original(*args, **kwargs)

                stack.enter_context(patch.object(owner, method, measured))

            conn = store.conn

            @contextmanager
            def measured_connection():
                span = (preparation_probe.span("native_snapshot_transaction")
                        if getattr(preparing, "active", False) else nullcontext())
                with span, conn() as connection:
                    yield connection

            stack.enter_context(patch.object(store, "conn", measured_connection))
            observe_stage(TRAINING_BUDGET, "checkpoint", "training_budget_checkpoint")
            observe_stage(universal_affected_sensors, "latest", "affected_declaration_read")
            observe_stage(universal_affected_sensors, "topology", "affected_topology_read")
            observe_stage(universal_affected_sensors, "prepare", "affected_evidence")
            observe_stage(DeviceAgentService, "legal_action_mask", "original_arbiter_mask")
            observe_stage(DeviceAgentService, "_persist_runtime_manual_holds", "manual_override")
            observe_stage(engine.context, "resolved_registry", "registry_snapshot")
            observe_stage(store, "_agent_dict", "agent_normalization")
            native_row_bytes = getattr(universal_predecision, '_sqlite_row_bytes', None)

            def measured_row_bytes(rows):
                with preparation_probe.span("sqlite_row_accounting"):
                    size = native_row_bytes(rows)
                materialization["native_snapshot_rows"] += len(rows)
                materialization["native_snapshot_structural_bytes"] += size
                return size

            # #368 predates this accounting helper. Installing a dormant probe
            # there preserves its original copy path and permits diagnostics.
            stack.enter_context(patch.object(universal_predecision, "_sqlite_row_bytes", measured_row_bytes, create=True))

            native_copy = universal_predecision.bounded_copy

            def measured_copy(value, **kwargs):
                snapshot = "states" in value
                phase = "ram_snapshot_copy" if snapshot else "bounded_evidence_copy"
                with preparation_probe.span(phase):
                    result, size = native_copy(value, **kwargs)
                materialization[phase + "_structural_bytes"] += size
                if snapshot:
                    materialization["state_entries"] += len(result["states"])
                    materialization["registry_entries"] += len(result["entities"])
                return result, size

            stack.enter_context(patch.object(universal_predecision, "bounded_copy", measured_copy))

            class DetachedConnection(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    with preparation_probe.span("detached_sql_execute"):
                        return super().execute(sql, parameters)

                def executemany(self, sql, parameters):
                    materialization["detached_insert_rows"] += len(parameters)
                    with preparation_probe.span("detached_sql_insert_batch"):
                        return super().executemany(sql, parameters)

                def close(self):
                    with preparation_probe.span('detached_cleanup'):
                        return super().close()

            def connect_detached(*args, **kwargs):
                with preparation_probe.span("detached_sql_connect"):
                    return sqlite3.connect(*args, factory=DetachedConnection, **kwargs)

            stack.enter_context(patch.object(universal_predecision, "sqlite3",
                                             SimpleNamespace(connect=connect_detached, Row=sqlite3.Row)))

            request = scenario.evidence.request
            matches = scenario.evidence._matches
            bind = scenario.evidence.bind

            def measured_request(agent_id):
                timeline.append(("requested", getattr(inference, "index", None), time.perf_counter_ns(),
                                 agent_id))
                return request(agent_id)

            def measured_match(evidence, agent, state, values):
                accepted = matches(evidence, agent, state, values)
                if not accepted:
                    timeline.append(("rejected_match", getattr(inference, "index", None),
                                     time.perf_counter_ns(), agent["id"]))
                return accepted

            def measured_bind(ticket, intent, agent, state, values):
                bound = bind(ticket, intent, agent, state, values)
                if bound is not None:
                    timeline.append(("consumed", getattr(inference, "index", None), time.perf_counter_ns(),
                                     agent["id"]))
                return bound

            stack.enter_context(patch.object(scenario.evidence, "request", measured_request))
            stack.enter_context(patch.object(scenario.evidence, "_matches", measured_match))
            stack.enter_context(patch.object(scenario.evidence, "bind", measured_bind))

        def observed_prepare():
            try:
                if profile_prepare and scenario.evidence.pending:
                    preparing.active = True
                    with scenario.evidence.lock:
                        agent_id = next(iter(scenario.evidence.pending), None)
                    timeline.append(("preparation_admitted", agent_id, time.perf_counter_ns()))
                    with preparation_probe.span("prepare_pending_turn"):
                        start, cpu = time.perf_counter_ns(), time.thread_time_ns()
                        prepared_before = scenario.evidence.stats["prepared"]
                        profiler = PreparationCPUProfile() if os.environ.get("HOMEMIND_PREPARATION_CPU_PROFILE") == "1" else None
                        try:
                            if profiler is not None: profiler.enable()
                            return prepare()
                        finally:
                            if profiler is not None: profiler.disable()
                            # Keep raw CPU rows; no profiler crosses threads.
                            if profiler is not None:
                                profiler.create_stats()
                                cpu_profiles.append(profiler.stats)
                            timeline.append(("preparation_turn", agent_id, start, time.perf_counter_ns(),
                                             time.thread_time_ns()-cpu,
                                             scenario.evidence.stats["prepared"] > prepared_before))
                return prepare()
            except Exception as exc:
                errors[str(exc)] += 1
                raise
            finally:
                if profile_prepare:
                    preparing.active = False

        scenario.evidence.prepare_one = observed_prepare
        collector.idle_ready = lambda: foreground_idle(engine)
        if profile_prepare:
            def measured_admission():
                with preparation_probe.span("foreground_idle_check"):
                    return foreground_idle(engine)

            collector.idle_ready = measured_admission
        if mode == "positive":
            # The existing positive control deliberately prepares per decision.
            collector.idle_ready = lambda: False
            scenario.drain = lambda: wait_for_writer(collector)
        probe = Probe(capacity=256)
        probe.install(stack, engine, store)
        if profile_prepare:
            from storage_budget import BudgetCursor

            observe_stage(BudgetCursor, "execute", "native_sql_execute")
        writer_active = threading.Event()
        native_write = collector._write

        def observed_write(*args):
            writer_active.set()
            writer_profiler = None
            if profile_prepare:
                wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
                if os.environ.get('HOMEMIND_COLLECTOR_CPU_PROFILE') == '1':
                    writer_profiler = PreparationCPUProfile()
                    writer_profiler.enable()
            try:
                return native_write(*args)
            finally:
                if writer_profiler is not None:
                    writer_profiler.disable()
                    writer_cpu_profiles.append(writer_profiler.stats)
                if profile_prepare:
                    timeline.append(("collector_write", None, wall, time.perf_counter_ns(),
                                     time.thread_time_ns()-cpu))
                writer_active.clear()

        collector._write = observed_write
        before = collector.diagnostics()
        if mode != "positive":
            anchor = time.perf_counter()
        if mode != "off":
            collector.start()
            if not collector.ready.wait(5):
                raise RuntimeError("Collector failed to start")
            stack.callback(collector.stop, 30)

        initial_files = {p.name: p.stat().st_size for p in path.parent.glob(path.name+"*")}
        cpu, started, rss_before = time.process_time(), time.perf_counter(), rss_mb()
        if profile_prepare:
            timeline.append(("load_started", None, time.perf_counter_ns()))
        threads_before = thread_cpu_snapshot() if profile_prepare else None
        if mode == "positive":
            count = max(128 if run_lifecycle else 8, devices)
            scenario.collect_events(count)
            intents = [(row["index"], row["desired"], intent)
                       for row, intent in zip(scenario.frames, scenario.intents)]
            event_count, timings, statuses = count*2, [], {}
            ingress_seconds = time.perf_counter()-started
        else:
            stop = threading.Event()
            worker = None
            if mode == "busy":
                worker = threading.Thread(target=busy_work, args=(scenario, stop, background_stats, feedback_jobs, feedback_done),
                                          daemon=True)
                worker.start()
            try:
                lock = sqlite3.connect(path, timeout=1) if mode == "overload" else None
                with closing(lock) if lock is not None else nullcontext():
                    if lock is not None:
                        # An independent writer blocks native admission while
                        # ingress continues past the collector's finite capacity.
                        lock.execute("BEGIN IMMEDIATE")
                    try:
                        event_count, intents, timings, statuses, ingress_seconds = stream(
                            scenario, mode, max((devices+fanout-1)//fanout, ticks), period, writer_active,
                            on_intent=submit_feedback if mode == "busy" else None, fanout=fanout,
                            timeline=timeline, inference=inference)
                    finally:
                        if lock is not None:
                            lock.rollback()
            finally:
                stop.set()
                if worker:
                    worker.join(30)
                    if worker.is_alive() or "error" in background_stats:
                        raise RuntimeError(dict(background_stats))

        if mode == "busy":
            candidate = engine.agent_candidates.candidate_shadow_async_diagnostics()
            background_stats["candidate_shadow"] = candidate
        signature = [(tick, desired, intent["target_entity"], intent["desired_value"], intent["decision_source"])
                     for tick, desired, intent in sorted(intents, key=lambda row: row[0])]
        collector_at_load_end = collector.diagnostics()
        with scenario.evidence.lock:
            preparation_pending_at_load_end = len(scenario.evidence.pending)
        load_cpu = time.process_time()-cpu
        load_seconds = time.perf_counter()-started
        if profile_prepare:
            timeline.append(("load_finished", None, time.perf_counter_ns()))
        thread_cpu = thread_cpu_delta(threads_before, thread_cpu_snapshot()) if profile_prepare else None
        finalization_started = time.perf_counter()
        load_sql_calls = sql_calls(probe)
        store._flush_all_provenance()
        if mode != "positive":
            # Label the original decision with an explicit synthetic Correct.
            # All stream rows share one episode so training cannot count them
            # as independent trials based on the sampling cadence.
            for tick, desired, intent in intents:
                if tick not in feedback_done:
                    undo = mode == "busy" and tick % 8 == 0
                    record_feedback(scenario, tick, desired, intent, undo=undo)
                    if undo:
                        background_stats["correct_undo_after_load"] += 1
        through = scenario.clock()
        before_final_drain = collector.diagnostics()
        drain_started = time.perf_counter()
        if mode != "off":
            collector.barrier(through)
            wait_for_writer(collector)
            if not collector.stop(30):
                raise RuntimeError("Collector failed to stop")
        drain_seconds = time.perf_counter()-drain_started
        finalization_seconds = time.perf_counter()-finalization_started

        end = collector.diagnostics()
        counts = inventory(scenario)
        dataset = (qualify(scenario, through, run_lifecycle=run_lifecycle) if mode != "off"
                   else dict(status="off", eligible=0))
        snapshot = probe.snapshot()["operations"]
        event_base = before["by_kind"].get("event", {})
        events = {key: value-event_base.get(key, 0)
                  for key, value in end["by_kind"].get("event", {}).items()}
        for key in ("queued", "written", "dropped", "rejected"):
            events.setdefault(key, 0)
        events.update(received=event_count, accepted=events["queued"])
        if mode != "off" and event_count != events["written"]+events["dropped"]+events["rejected"]:
            raise AssertionError(f"Unaccounted event envelopes: {events}")
        if mode == "positive" and dataset["eligible"] != len(intents):
            raise AssertionError(f"Positive control failed: {dataset}")
        if mode == "overload" and (not events["dropped"] or dataset["status"] != "rejected_source"):
            raise AssertionError(f"Overload failed to exercise fail-closed: {events}, {dataset}")
        if mode == "incomplete" and (dataset["status"] != "qualified_source" or dataset["eligible"]):
            raise AssertionError(f"Missing evidence qualified: {dataset}")
        result = dict(mode=mode, sensors=sensors, devices=devices, fanout=min(fanout, devices),
                      preparation_cpu_functions=(cpu_function_rows(cpu_profiles) if profile_prepare else None),
                      collector_cpu_functions=(cpu_function_rows(writer_cpu_profiles) if profile_prepare else None),
                      preparation_source_revisions=source_revisions if profile_prepare else None,
                      root_target_order=[a['target_entity'] for a in scenario.agents],
                      input_fingerprint=hashlib.sha256(json.dumps(dict(
                          mode=mode,sensors=sensors,devices=devices,ticks=ticks,period=period,
                          fanout=min(fanout,devices),candidates=candidate_count,
                          root_targets=[a['target_entity'] for a in scenario.agents]),
                          sort_keys=True).encode()).hexdigest(),
                      active_agents=len(scenario.agents),
                      candidates=candidate_count, durable_configs=durable_configs,
                      preparation_profile=preparation_probe.snapshot() if profile_prepare else None,
                      preparation_materialization=materialization if profile_prepare else None,
                      descriptor_state_reads=descriptor_state_reads if profile_prepare else None,
                      timeline=timeline, thread_cpu=thread_cpu,
                      preparation_pending_at_load_end=preparation_pending_at_load_end,
                      preparation_native_sql=snapshot.get("sqlite_execute.universal_predecision.prepare_one"),
                      preparation_engine_lock={k: v for k, v in snapshot.items()
                          if k.startswith("engine_lock_")
                          and (".prepare_one:" in k or ".observed_prepare:" in k)}
                          if profile_prepare else None,
                      predecision_max_agents=MAX_AGENTS,
                      prepare_error_reasons=errors, original_intents=len(intents), event=events,
                      decision_status=statuses, decision_signature=hashlib.sha256(json.dumps(signature).encode()).hexdigest(),
                      # Synthetic original decisions only. Retain the rows as well
                      # as their digest so any same-revision mismatch is diagnosable.
                      decision_trace=signature,
                      history=counts, dataset=dataset, collector=end,
                      collector_before_final_drain=before_final_drain, collector_at_load_end=collector_at_load_end,
                      background=background_stats,
                      requested_start=scenario.start, requested_through=through,
                      event_to_intent=quantiles(timings), event_to_intent_count=len(timings),
                      load_seconds=load_seconds, ingress_seconds=ingress_seconds, process_cpu_seconds=load_cpu,
                      finalization_seconds=finalization_seconds, final_drain_seconds=drain_seconds,
                      rss_before_mb=rss_before, rss_after_mb=rss_mb(),
                      load_sql_calls=load_sql_calls, total_sql_calls=sql_calls(probe),
                      store_lock_wait=snapshot.get("store_lock_wait"),
                      sqlite_files_before_load=initial_files,
                      sqlite_files={p.name: p.stat().st_size for p in path.parent.glob(path.name+"*")},
                      ha_service_calls=scenario.ha.call_count)
        if result["ha_service_calls"]:
            raise AssertionError(result)
        return result


class PreparationCPUProfile:
    """Thread-local CPU call tree; avoids Python 3.12 cProfile global hooks.

    Includes diagnostic hook overhead. Never enabled in acceptance runs.
    """
    def __init__(self):
        self.stack = []
        self.stats = {}

    def event(self, frame, event, arg):
        if event not in ('call', 'return', 'c_call', 'c_return', 'c_exception'):
            return
        at = time.thread_time_ns()
        if event in ('call', 'c_call'):
            if event == 'call':
                code = frame.f_code
                key = (Path(code.co_filename).name, code.co_firstlineno, code.co_name)
            else:
                key = ('C', 0, getattr(arg, '__qualname__', str(type(arg))))
            self.stack.append([key, at, 0])
        elif self.stack:
            key, started, child = self.stack.pop()
            elapsed = at-started
            old = self.stats.get(key, (0, 0, 0., 0., {}))
            self.stats[key] = (old[0]+1, old[1]+1, old[2]+(elapsed-child)/1e9,
                               old[3]+elapsed/1e9, {})
            if self.stack:
                self.stack[-1][2] += elapsed

    def enable(self):
        if sys.getprofile() is not None:
            raise RuntimeError('another profiler is active')
        sys.setprofile(self.event)

    def disable(self):
        sys.setprofile(None)

    def create_stats(self):
        pass


def cpu_function_rows(profiles):
    """Separate diagnostic only: exclusive/inclusive CPU in the preparation thread."""
    import pstats
    if not profiles:
        return []
    stats = pstats.Stats()
    stats.stats = profiles[0].copy()
    for profile in profiles[1:]:
        other = pstats.Stats()
        other.stats = profile
        stats.add(other)
    return [dict(file=Path(key[0]).name, line=key[1], function=key[2],
                 primitive_calls=value[0], calls=value[1],
                 exclusive_cpu_ms=value[2]*1000, inclusive_cpu_ms=value[3]*1000)
            for key, value in sorted(stats.stats.items(), key=lambda item: -item[1][3])]


def sql_calls(probe):
    return sum(row["count"] for key, row in probe.snapshot()["operations"].items()
               if key.startswith("sqlite_execute."))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=SCALES)
    parser.add_argument("--modes", default="positive,normal,busy,overload")
    parser.add_argument("--ticks", type=int, default=24)
    parser.add_argument("--fanout", type=int, default=1, help="Root targets admitted per motion event")
    parser.add_argument("--period", type=float, default=.4)
    parser.add_argument("--candidates", type=int, help="Override the number of seeded Candidate configs")
    parser.add_argument("--profile-prepare", action="store_true", help="Record bounded per-stage preparation spans")
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New JSONL checkpoint file")
    args = parser.parse_args()
    modes = args.modes.split(",")
    if (set(modes)-set(MODES) or not 1 <= args.ticks <= 1024 or not .01 <= args.period <= 10
            or not 1 <= args.fanout <= 64 or args.ticks*args.fanout > 2048):
        parser.error("unknown mode or invalid workload bounds")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"adaptive_ai/src"))
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    failed = False
    with tempfile.TemporaryDirectory(prefix="hm-load-") as tmp, args.output.open("x", encoding="utf-8") as output:
        os.environ["ADAPTIVE_AI_DATA"] = str(Path(tmp)/"bootstrap")
        output.write(json.dumps(dict(scope="host synthetic", python=sys.version, platform=platform.platform(),
                                     cpus=os.cpu_count(), sqlite=sqlite3.sqlite_version,
                                     ticks=args.ticks, period=args.period, lifecycle=args.lifecycle,
                                     fanout=args.fanout, candidates=args.candidates,
                                     profile_prepare=args.profile_prepare))+"\n")
        output.flush()
        for index, item in enumerate(args.cases.split(",")):
            sensors, devices = map(int, item.split(":"))
            for mode in modes:
                print(f"{sensors}/{devices} {mode}", file=sys.stderr, flush=True)
                folder = Path(tmp)/f"{index}-{mode}"
                folder.mkdir()
                try:
                    row = collect_case(folder/"native.db", sensors, devices, mode, args.ticks, args.period,
                                       run_lifecycle=args.lifecycle, fanout=args.fanout,
                                       candidates=args.candidates, profile_prepare=args.profile_prepare)
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    row = dict(mode=mode, sensors=sensors, devices=devices, error=repr(exc))
                    failed = True
                output.write(json.dumps(row)+"\n")
                output.flush()
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
