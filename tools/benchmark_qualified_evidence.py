"""Measure native historical evidence on a disposable synthetic Engine/Store."""
import argparse
from collections import Counter
from contextlib import closing, ExitStack, nullcontext
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

from runtime_scaling_probe import Probe
from universal_e2e_fixture import OriginalEvidenceScenario, ProducerClock, state


MODES = ("positive", "normal", "busy", "overload", "off", "incomplete")
SCALES = "5:1,50:10,200:30,1000:200"


def quantiles(values):
    ordered = sorted(values)
    return {f"p{p}_ms": ordered[(len(ordered)-1)*p//100] if ordered else None
            for p in (50, 95, 99)}


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


def seed_candidates(scenario):
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
    for agent in scenario.agents:
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


def stream(scenario, mode, ticks, period, writer_active, *, tick_offset=0, on_intent=None, fanout=1):
    from settings import OPTIONS
    engine, collector = scenario.engine, scenario.collector
    timings, intents, outstanding = [], [], {}
    counters = Counter()
    event_count = 0
    fanout = min(fanout, len(scenario.agents))

    def infer(agent, frame, received_ts):
        previous = engine.runtime.get(agent["id"], {}).get("last_intent")
        result = engine.process_agent(agent, frame, {"binary_sensor.motion"})
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
            received_ts = publish_event(scenario, "binary_sensor.motion", "on" if desired else "off",
                                    device_class="motion")
            event_count += 1
            frame = engine.state_map.copy()
            for lane in range(fanout):
                decision_index = tick*fanout+lane
                agent = scenario.agents[decision_index % len(scenario.agents)]
                target = agent["target_entity"]
                if target not in outstanding:
                    future = engine.control_workers.submit(infer, agent, frame, received_ts)
                    engine.in_flight[target] = future
                    outstanding[target] = (future, desired, decision_index)
                else:
                    counters["target_still_running"] += 1
            counters["peak_inference_outstanding"] = max(counters["peak_inference_outstanding"], len(outstanding))
            counters["ingress_while_writer_active"] += writer_active.is_set()
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


def collect_case(path, sensors, devices, mode, ticks, period, *, run_lifecycle=False, fanout=1):
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
        if mode == "busy":
            seed_candidates(scenario)
        if mode == "off":
            engine.universal_training_collector = None
            engine.executor.device_agents.universal_training_collector = None
        if mode == "incomplete":
            collector.predecision = None
        if mode == "overload":
            collector.capacity = 32  # Fault profile, after the original seed.
        errors = Counter()
        prepare = scenario.evidence.prepare_one

        def observed_prepare():
            try:
                return prepare()
            except Exception as exc:
                errors[str(exc)] += 1
                raise

        scenario.evidence.prepare_one = observed_prepare
        collector.idle_ready = lambda: foreground_idle(engine)
        if mode == "positive":
            # The existing positive control deliberately prepares per decision.
            collector.idle_ready = lambda: False
            scenario.drain = lambda: wait_for_writer(collector)
        probe = Probe(capacity=256)
        probe.install(stack, engine, store)
        writer_active = threading.Event()
        native_write = collector._write

        def observed_write(*args):
            writer_active.set()
            try:
                return native_write(*args)
            finally:
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
                            on_intent=submit_feedback if mode == "busy" else None, fanout=fanout)
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
        load_cpu = time.process_time()-cpu
        load_seconds = time.perf_counter()-started
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
                      active_agents=len(scenario.agents),
                      candidates=devices if mode == "busy" else 0, predecision_max_agents=MAX_AGENTS,
                      prepare_error_reasons=errors, original_intents=len(intents), event=events,
                      decision_status=statuses, decision_signature=hashlib.sha256(json.dumps(signature).encode()).hexdigest(),
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
                                     cpus=os.cpu_count(), ticks=args.ticks, period=args.period, lifecycle=args.lifecycle, fanout=args.fanout))+"\n")
        output.flush()
        for index, item in enumerate(args.cases.split(",")):
            sensors, devices = map(int, item.split(":"))
            for mode in modes:
                print(f"{sensors}/{devices} {mode}", file=sys.stderr, flush=True)
                folder = Path(tmp)/f"{index}-{mode}"
                folder.mkdir()
                try:
                    row = collect_case(folder/"native.db", sensors, devices, mode, args.ticks, args.period,
                                       run_lifecycle=args.lifecycle, fanout=args.fanout)
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
