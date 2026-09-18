#!/usr/bin/env python3
"""Reproducible F22 benchmark for history/diagnostic/training cost.

Run from the repository root:
    python tools/benchmark_history_costs.py
    python tools/benchmark_history_costs.py --pairs 3000 --teach-sensors 96 --teach-labels 24

The script prints JSON.  It always reports the machine it actually ran on.  A desktop/CI
result is never labelled Raspberry Pi; copy the repository to the Pi and run the exact
same command there for hardware evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adaptive_ai" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import agent_candidate_preference_metrics as pref
import performance_f22 as f22
import confidence_contract as confidence
import teaching_rl
from policy import DiagonalLinUCB
from test_performance_f22 import MemoryStore, FastMetricFixture


def percentile(values, q):
    values = sorted(float(x) for x in values)
    if not values:
        return None
    index = min(len(values) - 1, max(0, int((len(values) - 1) * q)))
    return values[index]


def rss_mb():
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Linux reports KiB; macOS reports bytes.
    if sys.platform == "darwin":
        return value / (1024.0 * 1024.0)
    return value / 1024.0


def pi_model():
    path = Path("/proc/device-tree/model")
    try:
        return path.read_text(errors="ignore").strip("\x00\n ")
    except Exception:
        return None


def timed(fn, repeats=1):
    samples = []
    result = None
    for _ in range(max(1, int(repeats))):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return result, samples


def fast_metric_benchmark(pair_count):
    store = MemoryStore()
    FastMetricFixture.schema(store)
    FastMetricFixture.data(store, n=pair_count)
    manager = SimpleNamespace(store=store)
    row = {"candidate_id": "child", "parent_agent_id": "parent", "queued_ts": 100.0}
    parent = {"id": "parent", "target_entity": "light.x", "target_property": "power"}
    child = {"id": "child", "training_state": "qualified"}
    status = {"teach_fit_total": 0, "teach_fit_after_count": 0}

    legacy = pref._fast_metrics
    store.reset_trace()
    before, legacy_ms = timed(lambda: legacy(manager, row, parent, child, {}, status))
    legacy_queries = len(store.selects())

    f22.ensure_tables(store)
    diag = f22.PerformanceDiagnostics()
    old_flag = getattr(pref, "_f22_fast_metrics_installed", False)
    pref._f22_fast_metrics_installed = False
    f22._install_fast_metrics(manager, diag)
    try:
        store.reset_trace()
        after, cold_ms = timed(lambda: pref._fast_metrics(manager, row, parent, child, {}, status))
        cold_queries = len(store.selects())
        store.reset_trace()
        warm, warm_ms = timed(lambda: pref._fast_metrics(manager, row, parent, child, {}, status), repeats=20)
        warm_queries = len(store.selects())
    finally:
        pref._fast_metrics = legacy
        pref._f22_fast_metrics_installed = old_flag

    keys = (
        "meaningful_opportunities", "parent_transition_accuracy", "candidate_transition_accuracy",
        "timing_parent_utility", "timing_candidate_utility", "timing_objective_gain",
        "preference_success_weight", "preference_failure_weight",
        "fast_on_parent_lead_seconds", "fast_on_candidate_lead_seconds",
        "fast_off_parent_lead_seconds", "fast_off_candidate_lead_seconds",
    )
    diffs = {}
    for key in keys:
        a, b = before.get(key), after.get(key)
        if isinstance(a, (float, int)) and isinstance(b, (float, int)):
            diffs[key] = abs(float(a) - float(b))
        else:
            diffs[key] = 0.0 if a == b else None
    return {
        "pairs": pair_count,
        "legacy": {"time_ms": legacy_ms[0], "select_statements": legacy_queries},
        "optimized_cold": {"time_ms": cold_ms[0], "select_statements": cold_queries},
        "optimized_warm_20_polls": {
            "total_time_ms": sum(warm_ms), "p95_time_ms": percentile(warm_ms, .95),
            "select_statements_total": warm_queries,
        },
        "max_absolute_metric_difference": max(v for v in diffs.values() if v is not None),
        "metric_differences": diffs,
        "diagnostics": diag.snapshot(),
        "warm_result_samples": warm.get("meaningful_opportunities"),
    }, (store, manager, row, parent, child, status, diag)


def confidence_fixed_future_benchmark(pair_count):
    store = MemoryStore()
    FastMetricFixture.schema(store)
    with store.conn() as db:
        for ddl in (
            "ALTER TABLE candidate_generation_pairs ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_eligible INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN dependency_cluster TEXT",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_outcome REAL",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_parent_correct INTEGER",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_child_correct INTEGER",
            "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_source_id TEXT",
        ):
            db.execute(ddl)
    confidence.ensure_tables(store)
    epochs = confidence.EvaluationEpochJournal(store)
    diag = f22.PerformanceDiagnostics()
    epochs._performance_diagnostics = diag

    def insert(i, eligible=False):
        outcome = float(i % 2)
        ts = 1000.0 + i * 10.0
        kind = "manual_user_target_change" if eligible else "external_target_transition"
        with store.conn() as db:
            db.execute(
                """INSERT INTO candidate_generation_pairs
                   (root_agent_id,parent_generation_id,child_generation_id,prediction_event_id,
                    prediction_ts,outcome_ts,outcome,parent_prediction,child_prediction,
                    parent_confidence,child_confidence,parent_correct,child_correct,paired_result,
                    parent_lead_seconds,child_lead_seconds,lead_gain_seconds,
                    evidence_kind,calibration_eligible,dependency_cluster,calibration_outcome,
                    calibration_parent_correct,calibration_child_correct,calibration_source_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "root","g0","g1",f"cb-{i}",ts-1.0,ts,outcome,outcome,outcome,
                    .8,.8,1,1,"both_correct",1.0,1.0,0.0,kind,1 if eligible else 0,
                    f"cluster-{i}",outcome if eligible else None,1 if eligible else None,
                    1 if eligible else None,f"label-{i}" if eligible else None,
                ),
            )

    for i in range(max(12, int(pair_count))):
        insert(i, eligible=False)
    screening = confidence._pair_rows(store, "g0", "g1")
    epoch = epochs.ensure(
        "g0","g1","rev-bench","diagonal_linucb:v11",screening,
        selection_target=12,final_target=12,min_per_action=4,
    )
    start_future = max(12, int(pair_count)) + 10
    for i in range(start_future, start_future + 12):
        insert(i, eligible=True)

    store.reset_trace()
    legacy_pairs, legacy_load_ms = timed(lambda: confidence._pair_rows(store, "g0", "g1"))
    legacy_report, legacy_report_ms = timed(
        lambda: epochs.final_report(epoch, legacy_pairs, scope_id="root")
    )
    locked = epochs.get("g0","g1","rev-bench","diagonal_linucb:v11")

    store.reset_trace()
    optimized, optimized_ms = timed(
        lambda: epochs.final_report_from_store(
            locked,"g0","g1",scope_id="root"
        )
    )
    cold_queries = len(store.selects())
    store.reset_trace()
    warm, warm_ms = timed(
        lambda: epochs.final_report_from_store(
            locked,"g0","g1",scope_id="root"
        ),
        repeats=20,
    )
    warm_sql = [x.lower() for x in store.selects()]
    pair_scans = sum("from candidate_generation_pairs" in x for x in warm_sql)

    equality_keys = (
        "episodes","effective_n","promotion_quality_passed","status","final_end_ts",
        "paired_delta","per_action_delta",
    )
    same = all(legacy_report.get(k) == optimized.get(k) for k in equality_keys)
    return {
        "screening_pairs": max(12, int(pair_count)),
        "fixed_future_rows": 12,
        "legacy": {
            "rows_materialized": len(legacy_pairs),
            "load_time_ms": legacy_load_ms[0],
            "report_time_ms": legacy_report_ms[0],
        },
        "optimized_cold": {
            "time_ms": optimized_ms[0],
            "select_statements": cold_queries,
            "max_rows_materialized_per_batch": diag.snapshot()["max_rows_materialized_per_batch"],
        },
        "optimized_warm_20_polls": {
            "total_time_ms": sum(warm_ms),
            "p95_time_ms": percentile(warm_ms, .95),
            "candidate_pair_full_scans": pair_scans,
        },
        "reports_equal": same,
        "warm_report_equal": warm == optimized,
        "cache_diagnostics": diag.snapshot(),
    }


def teach_fixture(sensor_count, label_count):
    store = MemoryStore()
    FastMetricFixture.schema(store)
    agent = {
        "id": "teach", "target_entity": "light.t", "target_property": "power",
        "min_value": 0.0, "max_value": 1.0, "input_entities": ["*"],
    }
    fp = teaching_rl.fingerprint(agent)
    base = 1_700_000_000.0
    labels = []
    step = max(6 * 3600.0, (2.2 * 86400.0) / max(1, label_count - 1))
    with store.conn() as c:
        for i in range(label_count):
            ts = base + i * step
            desired = float(i % 2)
            labels.append({"sample_ts": ts, "desired": desired, "fingerprint": fp})
            for j in range(sensor_count):
                value = desired if j < sensor_count // 2 else float((i + j) % 3) / 2.0
                c.execute(
                    "INSERT INTO entity_history(entity_id,ts,state,attributes_json,source) VALUES(?,?,?,?,?)",
                    (f"sensor.s{j}", ts - (j % 5), str(value), "{}", "synthetic"),
                )
    fake = SimpleNamespace(store=store)
    fake.labels = lambda agent_id: list(labels)
    fake.eligible_entities = lambda a: [f"sensor.s{j}" for j in range(sensor_count)]
    fake._f22_diagnostics = f22.PerformanceDiagnostics()
    return store, fake, agent


def teach_benchmark(sensor_count, label_count):
    store, fake, agent = teach_fixture(sensor_count, label_count)
    store.reset_trace()
    before, legacy_ms = timed(lambda: teaching_rl.RLTeaching.supervised_scores(fake, agent))
    legacy_queries = len(store.selects())
    store.reset_trace()
    after, batch_ms = timed(lambda: f22._batched_supervised_scores(fake, agent))
    batch_queries = len(store.selects())
    left, right = before[0], after[0]
    score_diff = max((abs(float(left[k]) - float(right[k])) for k in set(left) | set(right)
                      if k in left and k in right), default=0.0)
    return {
        "sensors": sensor_count,
        "labels": label_count,
        "legacy": {"time_ms": legacy_ms[0], "select_statements": legacy_queries},
        "optimized": {"time_ms": batch_ms[0], "select_statements": batch_queries},
        "scores_equal": left == right,
        "max_score_difference": score_diff,
        "max_rows_materialized_per_batch": fake._f22_diagnostics.snapshot()["max_rows_materialized_per_batch"],
    }


def inference_under_load(fast_context, teach_sensors, teach_labels):
    store, manager, row, parent, child, status, _ = fast_context
    # Reinstall the optimized function for the duration of this benchmark.
    legacy = pref._fast_metrics
    old_flag = getattr(pref, "_f22_fast_metrics_installed", False)
    pref._f22_fast_metrics_installed = False
    diag = f22.PerformanceDiagnostics()
    f22._install_fast_metrics(manager, diag)
    pref._fast_metrics(manager, row, parent, child, {}, status)  # warm cursor/state

    policy = DiagonalLinUCB(128, [0.0, 1.0])
    x = {i: ((i % 7) - 3) / 3.0 for i in range(32)}
    for i in range(80):
        policy.update(i % 2, x, 1.0 if i % 3 else -0.3)

    started = threading.Event()
    finished = threading.Event()

    def heavy():
        # Build its SQLite fixture inside this thread (sqlite connections are thread-affine).
        tstore, fake, agent = teach_fixture(teach_sensors, teach_labels)
        started.set()
        try:
            for _ in range(4):
                f22._batched_supervised_scores(fake, agent)
        finally:
            finished.set()

    worker = threading.Thread(target=heavy, name="f22-synthetic-heavy", daemon=True)
    worker.start()
    started.wait(5.0)
    inference_ms = []
    status_ms = []
    calls = 0
    try:
        while not finished.is_set() or calls < 1000:
            t0 = time.perf_counter()
            policy.choose(x, explore=False)
            inference_ms.append((time.perf_counter() - t0) * 1000.0)
            calls += 1
            if calls % 25 == 0:
                t1 = time.perf_counter()
                pref._fast_metrics(manager, row, parent, child, {}, status)
                status_ms.append((time.perf_counter() - t1) * 1000.0)
            if calls >= 10000:
                break
    finally:
        worker.join(timeout=10.0)
        pref._fast_metrics = legacy
        pref._f22_fast_metrics_installed = old_flag
    return {
        "inference_calls": len(inference_ms),
        "inference_p50_ms": percentile(inference_ms, .50),
        "inference_p95_ms": percentile(inference_ms, .95),
        "inference_max_ms": max(inference_ms) if inference_ms else None,
        "status_polls": len(status_ms),
        "status_poll_p95_ms": percentile(status_ms, .95),
        "status_poll_max_ms": max(status_ms) if status_ms else None,
        "workload": "DiagonalLinUCB.choose while batched Teach as-of scoring runs in another thread; fast Candidate status polled every 25 inference calls",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=1000)
    parser.add_argument("--teach-sensors", type=int, default=72)
    parser.add_argument("--teach-labels", type=int, default=24)
    args = parser.parse_args()
    model = pi_model()
    before_rss = rss_mb()
    fast, context = fast_metric_benchmark(max(20, args.pairs))
    confidence_future = confidence_fixed_future_benchmark(max(20, args.pairs))
    teach = teach_benchmark(max(12, args.teach_sensors), max(12, min(256, args.teach_labels)))
    under_load = inference_under_load(context, max(24, args.teach_sensors), max(12, min(256, args.teach_labels)))
    after_rss = rss_mb()
    result = {
        "benchmark": "HomeMind F22 history-cost benchmark v1",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "raspberry_pi": bool(model and "raspberry pi" in model.lower()),
            "raspberry_pi_model": model,
            "measurement_scope": "Raspberry Pi hardware" if model else "non-Pi host; do not quote as Raspberry Pi performance",
        },
        "dataset": {
            "candidate_pairs": max(20, args.pairs),
            "teach_sensors": max(12, args.teach_sensors),
            "teach_labels": max(12, min(256, args.teach_labels)),
            "decision_rows_per_pair": 4,
        },
        "fast_metrics": fast,
        "confidence_fixed_future": confidence_future,
        "teach_supervised_scores": teach,
        "concurrent_load": under_load,
        "peak_rss_mb": after_rss,
        "peak_rss_delta_from_start_mb": max(0.0, after_rss - before_rss),
        "proposed_pi_acceptance_budgets_not_measurements": {
            "inference_p95_ms_under_one_heavy_job": 50.0,
            "status_poll_p95_ms_under_one_heavy_job": 150.0,
            "training_queue_pending_max": 16,
            "write_commit_latency_p95_ms": 100.0,
            "teach_rows_materialized_per_batch_max": f22.TEACH_CANDIDATE_CHUNK * 256,
            "addon_peak_rss_mb": 512.0,
        },
        "pi_command": "python tools/benchmark_history_costs.py --pairs 3000 --teach-sensors 96 --teach-labels 24",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if (fast["max_absolute_metric_difference"] > 1e-9
            or not confidence_future["reports_equal"]
            or not confidence_future["warm_report_equal"]
            or confidence_future["optimized_warm_20_polls"]["candidate_pair_full_scans"] != 0
            or not teach["scores_equal"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
