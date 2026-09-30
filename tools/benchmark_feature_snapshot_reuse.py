#!/usr/bin/env python3
"""Full historical replay A/B benchmark for 0.14.113 feature snapshot reuse.

Runs the real isolated training worker twice against byte-identical seeded databases:
cache disabled vs enabled. This is a synthetic host benchmark, not Raspberry Pi timing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
sys.path.insert(0, str(SRC))

# storage.py creates its module-global Store at import time. Keep benchmark imports
# runner-safe; the real A/B workers below still receive their own isolated data roots.
os.environ.setdefault(
    "ADAPTIVE_AI_DATA",
    str(Path(tempfile.gettempdir()) / "homemind-feature-cache-import"),
)

from settings import APP_VERSION, DEFAULT_OPTIONS, TRAINING_REVISION
from storage import Store
from training_process import (
    JOB_FORMAT,
    JOB_VERSION,
    agent_config_fingerprint,
    descriptor_checksum,
    runtime_context_fingerprint,
)
from tiny_mlp_shadow import load_training_record


def seed_database(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "adaptive_ai.db")
    agent = store.create_agent({
        "name": "Feature snapshot benchmark",
        "target_entity": "light.bench",
        "target_property": "power",
        "min_value": 0,
        "max_value": 1,
        "deadband": 0.5,
        "exploration_step": 1,
        "confidence_threshold": 0.78,
        "action_interval": 0.25,
        "input_entities": [
            "binary_sensor.motion",
            "binary_sensor.radar",
            "sensor.illuminance",
        ],
    })
    base = 1_800_000_000.0
    dwell_seconds = 180.0
    dwell_count = 28
    end = base + dwell_count * dwell_seconds

    rows = []
    # Context is denser than the target stream and includes a few delayed receipts.
    tick = 0
    ts = base - 30.0
    while ts <= end + 30.0:
        phase = int(max(0.0, ts - base) // dwell_seconds)
        on = bool(phase % 2)
        late = 3.0 if tick and tick % 97 == 0 else 0.05
        rows.extend([
            (
                "binary_sensor.motion", ts,
                "on" if on or (tick % 11 == 0) else "off",
                {"device_class": "motion"}, None, "test", ts + late,
            ),
            (
                "binary_sensor.radar", ts + 0.1,
                "on" if on or (tick % 7 == 0) else "off",
                {"device_class": "occupancy"}, None, "test", ts + 0.15 + late,
            ),
            (
                "sensor.illuminance", ts + 0.2,
                str(8 + (tick % 9) if not on else 120 + (tick % 30)),
                {"device_class": "illuminance", "unit_of_measurement": "lx"},
                None, "test", ts + 0.25 + late,
            ),
        ])
        tick += 1
        ts += 15.0

    for idx in range(dwell_count + 1):
        event_ts = base + idx * dwell_seconds
        rows.append((
            "light.bench",
            event_ts,
            "on" if idx % 2 else "off",
            {},
            None,
            "test",
            event_ts + 0.02,
        ))
    rows.sort(key=lambda row: (float(row[1]), str(row[0])))
    for offset in range(0, len(rows), 4000):
        store.archive_batch(rows[offset:offset + 4000])

    # Make the source DB self-contained before SQLite backup/copy.
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    state_map = {
        "binary_sensor.motion": {
            "entity_id": "binary_sensor.motion",
            "state": "off",
            "attributes": {"device_class": "motion"},
        },
        "binary_sensor.radar": {
            "entity_id": "binary_sensor.radar",
            "state": "off",
            "attributes": {"device_class": "occupancy"},
        },
        "sensor.illuminance": {
            "entity_id": "sensor.illuminance",
            "state": "10",
            "attributes": {
                "device_class": "illuminance",
                "unit_of_measurement": "lx",
            },
        },
        "light.bench": {
            "entity_id": "light.bench",
            "state": "off",
            "attributes": {},
        },
    }
    registry = {
        "binary_sensor.motion": {"area_id": "bench"},
        "binary_sensor.radar": {"area_id": "bench"},
        "sensor.illuminance": {"area_id": "bench"},
        "light.bench": {"area_id": "bench"},
    }
    return store.path, agent["id"], base, end, state_map, registry


def sqlite_backup(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def worker_job(root, aid, base, end, state_map, registry, cache_enabled):
    options = dict(DEFAULT_OPTIONS)
    options.update({
        "training_cpu_duty_cycle": 1.0,
        "training_max_continuous_work_ms": 1000,
        "training_throttle_max_sleep_seconds": 0.001,
        "training_feature_snapshot_cache_entries": 512 if cache_enabled else 0,
        "training_feature_snapshot_cache_units": 65536 if cache_enabled else 0,
        "training_worker_effective_feature_snapshot_cache_entries": (
            512 if cache_enabled else 0
        ),
        "training_worker_effective_feature_snapshot_cache_units": (
            65536 if cache_enabled else 0
        ),
        "training_replay_ram_cache_rows": 65536,
        "training_replay_ram_cache_entry_rows": 2048,
        "training_home_context_cache_entries": 64,
        "training_home_context_cache_units": 32768,
        "training_ram_replay_index_mb": 192,
        "prediction_horizons_seconds": "1,3,10",
        "feature_dimensions": 128,
        "tiny_mlp_supervised_training_enabled": True,
        "tiny_mlp_train_epochs": 2,
    })
    store = Store(root / "adaptive_ai.db")
    job = {
        "format": JOB_FORMAT,
        "version": JOB_VERSION,
        "job_id": "feature-cache-" + ("on" if cache_enabled else "off"),
        "app_version": APP_VERSION,
        "training_revision": TRAINING_REVISION,
        "created_at": base,
        "agent_id": aid,
        "agent_fingerprint": agent_config_fingerprint(store.get_agent_config(aid)),
        "context_fingerprint": runtime_context_fingerprint(
            state_map, registry, options
        ),
        "state_map": state_map,
        "entity_registry": registry,
        "context_relevance": {},
        "automation_hints": [],
        "schema_cache_item": {},
        "options": options,
        "start_ts": base - 45.0,
        "end_ts": end + 1.0,
        "train_kwargs": {
            "qualify": False,
            "include_candidates": True,
            "benchmark": True,
            "accumulate_benchmark": False,
            "progress_lo": 0.0,
            "progress_hi": 1.0,
            "progress_label": "Feature snapshot benchmark",
        },
        "status_path": str(root / "status.json"),
        "result_path": str(root / "result.json"),
        "log_path": str(root / "worker.log"),
    }
    job["checksum"] = descriptor_checksum(job)
    path = root / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def run_worker(root, aid, base, end, state_map, registry, cache_enabled):
    import training_process

    job_path = worker_job(
        root, aid, base, end, state_map, registry, cache_enabled
    )
    env = dict(os.environ)
    env["ADAPTIVE_AI_DATA"] = str(root)
    env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, training_process.__file__, "--worker", str(job_path)],
        env=env,
        cwd=str(Path(training_process.__file__).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=90,
    )
    wall = time.perf_counter() - started
    if completed.returncode:
        log = root / "worker.log"
        raise RuntimeError(
            f"worker exited {completed.returncode}: {completed.stdout}\n"
            + (log.read_text(errors="replace") if log.exists() else "")
        )
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise RuntimeError("worker result failed: " + json.dumps(result))
    store = Store(root / "adaptive_ai.db")
    return {
        "wall_seconds": wall,
        "result": result,
        "store": store,
    }


def semantic_experiences(store, aid):
    rows = store.list_historical_experiences(aid, limit=10000)
    cleaned = []
    for row in rows:
        cleaned.append({
            key: value
            for key, value in row.items()
            if key not in ("id", "agent_id", "created_at")
        })
    return sorted(
        cleaned,
        key=lambda row: (
            int(row.get("target_history_id") or 0),
            float(row.get("dwell_seconds") or 0.0),
        ),
    )


def semantic_ridge_model(store, aid):
    model = dict(store.get_model(aid) or {})
    heads = {}
    for horizon, raw in dict(model.get("heads") or {}).items():
        head = dict(raw or {})
        head.pop("last_decay_ts", None)
        heads[str(horizon)] = head
    return {
        "version": model.get("version"),
        "dims": model.get("dims"),
        "actions": model.get("actions"),
        "horizons": model.get("horizons"),
        "schema": model.get("schema"),
        "selection_meta": model.get("selection_meta"),
        "heads": heads,
    }


def _strip_neural_nondeterminism(value):
    """Remove transport/profiling identity fields, never learned semantics.

    TinyMLP supervised training intentionally assigns a fresh UUID model_revision on each
    successful offline fit. model_checksum therefore changes with that UUID, and
    elapsed_seconds is wall-clock telemetry. Neither affects inference, learned tensors,
    normalization, sample counts, tournament scoring or backend selection.
    """
    if isinstance(value, dict):
        return {
            key: _strip_neural_nondeterminism(item)
            for key, item in value.items()
            if key not in {
                "model_revision",
                "model_checksum",
                "elapsed_seconds",
            }
        }
    if isinstance(value, list):
        return [_strip_neural_nondeterminism(item) for item in value]
    return value


def semantic_neural_model(store, aid):
    record = load_training_record(store, aid)
    if not record:
        return None
    return _strip_neural_nondeterminism({
        "model": record.get("model"),
        "mask": record.get("mask"),
        "training": record.get("training"),
        "tournament": record.get("tournament"),
        "selected_backend": record.get("selected_backend"),
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hm-feature-cache-bench-") as tmp:
        root = Path(tmp)
        seed_root = root / "seed"
        source_db, aid, base, end, state_map, registry = seed_database(seed_root)

        off_root = root / "off"
        on_root = root / "on"
        sqlite_backup(source_db, off_root / "adaptive_ai.db")
        sqlite_backup(source_db, on_root / "adaptive_ai.db")

        baseline = run_worker(
            off_root, aid, base, end, state_map, registry, False
        )
        cached = run_worker(
            on_root, aid, base, end, state_map, registry, True
        )

        exp_off = semantic_experiences(baseline["store"], aid)
        exp_on = semantic_experiences(cached["store"], aid)
        ridge_off = semantic_ridge_model(baseline["store"], aid)
        ridge_on = semantic_ridge_model(cached["store"], aid)
        neural_off = semantic_neural_model(baseline["store"], aid)
        neural_on = semantic_neural_model(cached["store"], aid)

        status_off = dict(
            baseline["result"].get("training_feature_snapshot_cache") or {}
        )
        status_on = dict(
            cached["result"].get("training_feature_snapshot_cache") or {}
        )
        builds_off = int(status_off.get("builds") or 0)
        builds_on = int(status_on.get("builds") or 0)
        build_reduction = (
            1.0 - builds_on / max(1, builds_off)
        )
        wall_speedup = (
            baseline["wall_seconds"] / max(cached["wall_seconds"], 1e-9)
        )

        parity = {
            "return_value": (
                baseline["result"].get("return_value")
                == cached["result"].get("return_value")
            ),
            "experiences": exp_off == exp_on,
            "ridge_model": ridge_off == ridge_on,
            "neural_model": neural_off == neural_on,
        }
        result = {
            "contract": "feature_snapshot_full_replay_benchmark_v1",
            "environment": (
                "synthetic isolated-worker historical replay; "
                "not Raspberry Pi timing"
            ),
            "parity": parity,
            "parity_all": all(parity.values()),
            "historical_experiences": len(exp_on),
            "baseline_wall_seconds": baseline["wall_seconds"],
            "cached_wall_seconds": cached["wall_seconds"],
            "wall_speedup": wall_speedup,
            "baseline_feature_builds": builds_off,
            "cached_feature_builds": builds_on,
            "feature_build_reduction_ratio": build_reduction,
            "cache_hits": int(status_on.get("hits") or 0),
            "cache_misses": int(status_on.get("misses") or 0),
            "duplicate_hits": int(status_on.get("duplicate_hits") or 0),
            "unique_feature_timestamps": int(
                status_on.get("unique_feature_timestamps") or 0
            ),
            "feature_build_seconds": float(
                status_on.get("build_seconds") or 0.0
            ),
            "cache_entries": int(status_on.get("entries") or 0),
            "cache_units": int(status_on.get("units") or 0),
            "pass": bool(
                all(parity.values())
                and int(status_on.get("hits") or 0) > 0
                and builds_on < builds_off
            ),
        }
        print(json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":") if args.compact else None,
            indent=None if args.compact else 2,
        ))
        if not result["pass"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
