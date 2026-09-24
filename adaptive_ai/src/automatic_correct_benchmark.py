"""Synthetic host benchmark for Stage-6 Automatic Correct reward buffering."""
from __future__ import annotations

import argparse
import json
import resource
import statistics
import tempfile
import time
from pathlib import Path

from automatic_correct_rewards import AutomaticRewardJournal


def _rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024)


def _percentile(values, q):
    values = sorted(float(v) for v in values)
    if not values:
        return 0.0
    pos = (len(values) - 1) * float(q)
    low = int(pos)
    high = min(len(values) - 1, low + 1)
    frac = pos - low
    return values[low] * (1.0 - frac) + values[high] * frac


def run(rows=512, summaries=2000):
    from storage import Store

    rows = max(32, int(rows))
    summaries = max(100, int(summaries))
    with tempfile.TemporaryDirectory(prefix="hm-auto-reward-bench-") as root:
        store = Store(Path(root) / "automatic-correct.db")
        journal = AutomaticRewardJournal(store)
        rss_before = _rss_bytes()
        start_times = []
        resolve_times = []
        wall_start = time.perf_counter()

        for idx in range(rows):
            created = 1000.0 + idx
            key = f"decision:bench-{idx}"
            payload = {
                "resolution_key": key,
                "agent_id": "bench-agent",
                "generation_id": "generation:bench",
                "decision_id": f"bench-{idx}",
                "trial_id": None,
                "action_index": idx % 2,
                "action_value": float(idx % 2),
                "action_ts": created,
                "observation_start": created,
                "observation_end": created + 90.0,
                "target_entity": "light.bench",
                "target_property": "power",
                "area_id": "bench-room",
                "observation_schema_id": "obs-v1:bench",
                "observation_mask_id": "mask-bench",
                "observation": {
                    "timestamp": created,
                    "feature_ids": ["time:hour_sin"],
                    "values": [0.25],
                },
                "observation_mask": {
                    "schema_id": "obs-v1:bench",
                    "mask_id": "mask-bench",
                    "feature_ids": ["time:hour_sin"],
                },
                "prediction_inputs": ["binary_sensor.bench_presence"],
                "background_dependencies": [],
                "outcome_sources": {},
                "reward_sources": [],
                "metadata": {"benchmark": True},
            }
            t0 = time.perf_counter_ns()
            saved, inserted = journal.start(payload)
            start_times.append((time.perf_counter_ns() - t0) / 1000.0)
            if not inserted or saved is None:
                raise AssertionError("fresh benchmark row was not inserted")
            t0 = time.perf_counter_ns()
            resolved, changed = journal.resolve(
                key,
                status="trusted" if idx % 4 == 0 else "unknown",
                outcome=(
                    "verified_same_area_presence_outcome"
                    if idx % 4 == 0 else "no_override_observed"
                ),
                proposed_reward=.6 if idx % 4 == 0 else .15,
                trusted_reward=.6 if idx % 4 == 0 else None,
                confidence=.95 if idx % 4 == 0 else .2,
                attribution_reason=(
                    "synthetic verified source"
                    if idx % 4 == 0 else "synthetic silence"
                ),
                source_entity_id=(
                    "binary_sensor.bench_presence"
                    if idx % 4 == 0 else None
                ),
                source_reliability=.95 if idx % 4 == 0 else .2,
                reward_sources=(
                    ["binary_sensor.bench_presence"]
                    if idx % 4 == 0 else ["absence_of_override_only"]
                ),
            )
            resolve_times.append((time.perf_counter_ns() - t0) / 1000.0)
            if not changed or resolved is None:
                raise AssertionError("fresh benchmark row was not resolved")

        # Idempotence is a correctness/performance gate: duplicate decisions must not
        # grow the table or throw.
        duplicate, inserted = journal.start({
            **payload,
            "resolution_key": "decision:duplicate-alias",
            "decision_id": f"bench-{rows - 1}",
        })
        if inserted or duplicate is None:
            raise AssertionError("decision deduplication failed")

        summary_times = []
        for _ in range(summaries):
            t0 = time.perf_counter_ns()
            summary = journal.summary("bench-agent")
            summary_times.append((time.perf_counter_ns() - t0) / 1000.0)
        if summary["counts"]["trusted"] != rows // 4:
            raise AssertionError("trusted row count mismatch")
        if sum(summary["counts"].values()) != rows:
            raise AssertionError("reward buffer count mismatch")

        with store.conn() as c:
            stored = int(c.execute(
                "SELECT COUNT(*) FROM automatic_reward_experiences"
            ).fetchone()[0])
        if stored != rows:
            raise AssertionError("deduplication changed durable row count")

        elapsed = time.perf_counter() - wall_start
        rss_after = _rss_bytes()
        return {
            "contract": "automatic_correct_stage6_buffer_benchmark_v1",
            "pass": True,
            "rows": rows,
            "trusted_rows": rows // 4,
            "unknown_rows": rows - rows // 4,
            "durable_rows": stored,
            "wall_seconds": elapsed,
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_after,
            "rss_delta_bytes": max(0, rss_after - rss_before),
            "start_us": {
                "p50": _percentile(start_times, .50),
                "p95": _percentile(start_times, .95),
                "p99": _percentile(start_times, .99),
            },
            "resolve_us": {
                "p50": _percentile(resolve_times, .50),
                "p95": _percentile(resolve_times, .95),
                "p99": _percentile(resolve_times, .99),
            },
            "ram_summary_us": {
                "p50": _percentile(summary_times, .50),
                "p95": _percentile(summary_times, .95),
                "p99": _percentile(summary_times, .99),
                "mean": statistics.fmean(summary_times),
            },
            "policy_updates": False,
            "physical_authority": False,
            "note": (
                "GitHub-host synthetic SQLite timing; not Raspberry Pi 4 acceptance."
            ),
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=512)
    parser.add_argument("--summaries", type=int, default=2000)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    report = run(args.rows, args.summaries)
    print(json.dumps(report, separators=(",", ":") if args.compact else None))


if __name__ == "__main__":
    main()
