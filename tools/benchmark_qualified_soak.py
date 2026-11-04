"""Persistent synthetic Engine/Store soak with JSONL checkpoints per window.

Synthetic collector disconnect/reconciliation exercises the native continuity
boundary. The WebSocket transport itself is covered separately by fake-WS tests.
"""
import argparse
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

from benchmark_qualified_evidence import (
    busy_work, publish_event, qualify, quantiles, seed_candidates, stream, wait_for_writer,
)
from universal_e2e_fixture import OriginalEvidenceScenario, ProducerClock


LIMITS = dict(drain_seconds=15, recovery_seconds=15, rss_growth_mb=128,
              cpu_last_over_first=1.5, p95_last_over_first=2.0, wal_bytes=32*1024*1024,
              sqlite_growth_bytes_per_event=128*1024, max_queue=1024)


def sqlite_sizes(path):
    return {p.name: p.stat().st_size for p in path.parent.glob(path.name+"*")}


def label_intents(scenario, intents):
    for tick, desired, intent in intents:
        label = f"soak:{tick}"
        scenario.feedback.record(agent_id=intent["agent_id"], selected_ts=intent["created_at"],
                                 source="correct", correct_action=desired, feedback_id=label,
                                 decision_id=intent["intent_id"], episode_id="continuous-soak")
        if tick % 8 == 0:
            scenario.feedback.undo(label)


def rotate_collector(scenario):
    from universal_history_collector import HistoricalTrainingCollector, foreground_idle
    from universal_predecision import PredecisionEvidence
    engine, store = scenario.engine, scenario.store
    clock = scenario.clock
    started = time.perf_counter()
    collector = scenario.collector
    collector.connection(False)
    collector.barrier()
    wait_for_writer(collector)
    crossing = qualify(scenario, clock())
    if crossing["status"] != "rejected_source" or crossing["reason"] != "incomplete replay coverage":
        raise AssertionError(f"Disconnected window exported: {crossing}")
    if not collector.stop(LIMITS["recovery_seconds"]):
        raise RuntimeError("Collector failed to shut down")

    collector = HistoricalTrainingCollector(store, scenario.journal, clock=clock,
                                            idle_ready=lambda: foreground_idle(engine))
    scenario.collector = engine.universal_training_collector = collector
    engine.executor.device_agents.universal_training_collector = collector
    scenario.evidence = collector.predecision = PredecisionEvidence(engine, store, collector)
    collector.start()
    if not collector.ready.wait(5):
        raise RuntimeError("Replacement collector failed to open")
    collector.connection(True)
    for kind, rows, callback in (
        ("entity", scenario.entities, engine.update_entity_registry),
        ("device", scenario.devices, engine.update_device_registry),
        ("area", scenario.areas, engine.update_area_registry),
    ):
        collector.apply_registry(kind, callback, rows, clock(), True, collector.epoch)
    wait_for_writer(collector)
    request = clock()
    states = engine.state_map.copy()
    received = clock()
    collector.snapshot(states, received_ts=received, requested_ts=request, epoch=collector.epoch)
    collector.barrier(received)
    wait_for_writer(collector)
    if not collector.segment:
        raise AssertionError(collector.diagnostics())
    scenario.start = received
    for agent in scenario.agents:
        scenario.declare(agent)
    return dict(seconds=time.perf_counter()-started, crossing_rejected=crossing["reason"])


def window(scenario, index, tick_offset, seconds):
    from telemetry import rss_mb
    store, engine, collector = scenario.store, scenario.engine, scenario.collector
    started, cpu = time.perf_counter(), time.process_time()
    idle_seconds = seconds*.12
    ticks = max(1, int(seconds*.75/.4))
    stop = threading.Event()
    stats = dict(policy_updates=0, model_writes=0, prune_calls=0)
    worker = threading.Thread(target=busy_work, args=(scenario, stop, stats), daemon=True)
    worker.start()
    try:
        time.sleep(idle_seconds)
        events, intents, latencies, statuses, _ = stream(
            scenario, "busy", ticks, .4, threading.Event(), tick_offset=tick_offset)
    finally:
        stop.set()
        worker.join(15)
        if worker.is_alive() or "error" in stats:
            raise RuntimeError(stats)

    before_busy = collector.diagnostics()
    with closing(sqlite3.connect(store.path, timeout=2)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        try:
            for index_in_burst in range(16):
                publish_event(scenario, "sensor.outdoor", index_in_burst, device_class="temperature")
            # Deliberate SQLite busy window. No drain or durability wait during ingress.
            time.sleep(.08)
        finally:
            lock.rollback()
    events += 16
    before_drain = collector.diagnostics()
    futures = list(engine.in_flight.values())
    candidate = engine.agent_candidates.candidate_shadow_async_diagnostics()
    drain_started = time.perf_counter()
    store._flush_all_provenance()
    label_intents(scenario, intents)
    through = scenario.clock()
    collector.barrier(through)
    wait_for_writer(collector)
    drain = time.perf_counter()-drain_started
    final = collector.diagnostics()
    event = final["by_kind"].get("event", {})
    if events != sum(event.get(key, 0) for key in ("written", "dropped", "rejected")):
        raise AssertionError(f"Unaccounted events in window {index}: {event}")
    dataset = qualify(scenario, through)
    if final["dropped"] and dataset["status"] != "rejected_source":
        raise AssertionError(f"Coverage accepted across dropped envelopes in window {index}")
    result = dict(index=index, elapsed_seconds=time.perf_counter()-started,
                  cpu_seconds=time.process_time()-cpu, rss_mb=rss_mb(), threads=threading.active_count(),
                  active_futures=sum(not f.done() for f in futures),
                  completed_futures=sum(f.done() for f in futures),
                  scheduler_pending_targets=len(engine.ready_target_changes)+len(engine.pending_target_changes),
                  collector_before_busy=before_busy, collector_before_drain=before_drain,
                  collector_after_drain=final, candidate=candidate, sqlite=store.sqlite_snapshot(),
                  sqlite_files=sqlite_sizes(Path(store.path)), dataset=dataset,
                  event_to_intent=quantiles(latencies), event_to_intent_count=len(latencies),
                  statuses=statuses, events=events, intents=len(intents), training=stats, drain_seconds=drain)
    if not intents or final["queue"] or result["active_futures"] or scenario.ha.call_count:
        raise AssertionError(f"Lost processing capacity or safety invariant in window {index}")
    return result, ticks


def trend(rows):
    failures = []
    for row in rows:
        idx = row["index"]
        if row["drain_seconds"] > LIMITS["drain_seconds"]:
            failures.append(f"drain:{idx}")
        if row["recovery"]["seconds"] > LIMITS["recovery_seconds"]:
            failures.append(f"recovery:{idx}")
        if row["collector_after_drain"]["max_queue"] > LIMITS["max_queue"]:
            failures.append(f"collector_queue:{idx}")
        if row["candidate"]["queue_depth"] > row["candidate"]["queue_limit"]:
            failures.append(f"candidate_queue:{idx}")
        if row["collector_after_drain"]["errors"] or row["candidate"]["errors"]:
            failures.append(f"worker_errors:{idx}")
        if any(size > LIMITS["wal_bytes"] for name, size in row["sqlite_files"].items() if name.endswith("-wal")):
            failures.append(f"wal_size:{idx}")
    rss = [r["rss_mb"] for r in rows if r["rss_mb"] is not None]
    if rss and max(rss)-rss[0] > LIMITS["rss_growth_mb"]:
        failures.append("rss_growth")
    if not rows:
        return dict(status="INCONCLUSIVE", failures=failures, intervals=[])
    end = rows[-1]["since_start_seconds"]
    if end >= 14400:
        starts = [0, (end-3600)/2, end-3600]
        groups = [[r for r in rows if start < r["since_start_seconds"] <= start+3600] for start in starts]
    else:
        cuts = [0, len(rows)//3, len(rows)*2//3, len(rows)]
        groups = [rows[lo:hi] for lo, hi in zip(cuts, cuts[1:])]
        starts = [rows[lo-1]["since_start_seconds"] if lo else 0 for lo in cuts[:-1]]
    intervals = []
    for start, group in zip(starts, groups):
        if not group:
            intervals.append(dict(from_seconds=start, windows=0))
            continue
        cpu = sum(r["cpu_seconds"] for r in group)
        elapsed = sum(r["elapsed_seconds"] for r in group)
        p95 = [r["event_to_intent"]["p95_ms"] for r in group if r["event_to_intent"]["p95_ms"] is not None]
        intervals.append(dict(from_seconds=start, windows=len(group), cpu_core_equivalents=cpu/elapsed,
                              median_window_p95_ms=statistics.median(p95) if p95 else None,
                              rss_mb=group[-1]["rss_mb"]))
    enough = end >= 14400 and all(interval["windows"] >= 10 for interval in intervals) and len(rss) == len(rows)
    if enough:
        first, _, last = intervals
        if last["cpu_core_equivalents"] > first["cpu_core_equivalents"]*LIMITS["cpu_last_over_first"]:
            failures.append("cpu_trend")
        if first["median_window_p95_ms"] and last["median_window_p95_ms"] > first["median_window_p95_ms"]*LIMITS["p95_last_over_first"]:
            failures.append("intent_latency_trend")
    db_growth = sum(rows[-1]["sqlite_files"].values())-sum(rows[0]["sqlite_files"].values())
    if db_growth > LIMITS["sqlite_growth_bytes_per_event"]*sum(r["events"] for r in rows[1:]):
        failures.append("sqlite_growth")
    # This harness bypasses full scheduler and WebSocket transport, so its
    # bounded observations cannot establish the complete runtime gate.
    return dict(status="FAIL" if failures else "INCONCLUSIVE", failures=failures, intervals=intervals,
                duration_gate_reached=enough, sqlite_growth_bytes=db_growth)


def run(path, output, args):
    from storage import Store
    from universal_history_collector import foreground_idle
    base = ProducerClock()
    anchor = None

    class Clock:
        set = base.set

        def __call__(self):
            return base() if anchor is None else base()+(time.perf_counter()-anchor)

    with ExitStack() as stack:
        store = Store(path)
        for start, stop in ((store.start_wal_keeper, store.stop_wal_keeper),
                            (store.start_runtime_cache, store.stop_runtime_cache),
                            (store.start_wal_checkpoint, store.stop_wal_checkpoint)):
            start()
            stack.callback(stop)
        scenario = stack.enter_context(OriginalEvidenceScenario(
            store, sensors=args.sensors, devices=args.devices, clock=Clock()))
        seed_candidates(scenario)
        anchor = time.perf_counter()
        scenario.collector.idle_ready = lambda: foreground_idle(scenario.engine)
        scenario.collector.start()
        try:
            if not scenario.collector.ready.wait(5):
                raise RuntimeError("Initial collector failed to start")
            rows, tick_offset = [], 0
            start = time.perf_counter()
            while (len(rows) < args.cycles if args.cycles else time.perf_counter()-start < args.duration):
                result, ticks = window(scenario, len(rows), tick_offset, args.window)
                result["since_start_seconds"] = time.perf_counter()-start
                try:
                    result["recovery"] = rotate_collector(scenario)
                except Exception as exc:
                    result["recovery"] = dict(error=repr(exc))
                    output.write(json.dumps(result)+"\n")
                    output.flush()
                    raise
                output.write(json.dumps(result)+"\n")
                output.flush()
                rows.append(result)
                tick_offset += ticks
            summary = trend(rows)
            summary.update(elapsed_seconds=time.perf_counter()-start, completed_windows=len(rows),
                           ha_service_calls=scenario.ha.call_count)
            output.write(json.dumps(dict(summary=summary))+"\n")
            output.flush()
            return int(bool(summary["failures"]))
        finally:
            if not scenario.collector.stop(30):
                raise RuntimeError("Soak collector failed to stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=14400)
    parser.add_argument("--cycles", type=int, default=0, help="Fixed-window regression profile")
    parser.add_argument("--window", type=float, default=45)
    parser.add_argument("--sensors", type=int, default=50)
    parser.add_argument("--devices", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.duration <= 28800 or not 2 <= args.window <= 60 or not 0 <= args.cycles <= 12:
        parser.error("Duration or window outside the bounded runner profile")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"adaptive_ai/src"))
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    with tempfile.TemporaryDirectory(prefix="hm-soak-") as folder, args.output.open("x", encoding="utf-8") as output:
        os.environ["ADAPTIVE_AI_DATA"] = str(Path(folder)/"bootstrap")
        output.write(json.dumps(dict(platform=platform.platform(), python=sys.version,
                                     duration=args.duration, cycles=args.cycles, window=args.window, sensors=args.sensors,
                                     devices=args.devices, limits=LIMITS))+"\n")
        output.flush()
        return run(Path(folder)/"soak.db", output, args)


if __name__ == "__main__":
    sys.exit(main())
