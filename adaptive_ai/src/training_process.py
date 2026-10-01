"""Single-process historical training isolation for Raspberry Pi deployments.

The realtime process remains authoritative for HA ingress, HTTP, queue admission and
physical control. One clean Python child at a time executes a versioned historical replay
chunk against the same WAL database. The parent supervises progress, RSS/CPU/I/O and only
accepts the result if the agent/runtime contract still matches the submitted job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid

from settings import (
    APP_VERSION, DATA_DIR, OPTIONS, TRAINING_REVISION, now_ts,
)


JOB_FORMAT = "homemind-isolated-training-job"
JOB_VERSION = 1
RESULT_FORMAT = "homemind-isolated-training-result"
RESULT_VERSION = 1
RESOURCE_PROFILE_CONTRACT = "adaptive_ram_first_worker_profile_v1"

AGENT_CONFIG_KEYS = (
    "id", "enabled", "input_entities", "target_entity", "target_property",
    "min_value", "max_value", "confidence_threshold", "deadband",
    "action_interval", "exploration_step", "exploration_interval",
    "micro_exploration", "ack_timeout", "settling_seconds",
    "manual_hold_seconds",
)
STATE_METADATA_KEYS = (
    "device_class", "unit_of_measurement", "friendly_name", "supported_features",
    "supported_color_modes", "options", "min", "max", "step",
    "min_temp", "max_temp", "target_temp_step", "min_humidity", "max_humidity",
)
TRAINING_STATE_ATTRIBUTE_KEYS = STATE_METADATA_KEYS + (
    # Current target-property attributes are needed to interpret the snapshot, but they
    # are deliberately excluded from runtime_context_fingerprint: normal device changes
    # during a multi-minute replay must not make a finished worker stale.
    "brightness", "temperature", "current_position", "percentage", "volume_level",
    "humidity",
)
REGISTRY_METADATA_KEYS = ("device_id", "area_id", "platform", "integration")

# These knobs affect only worker scheduling/cache pressure. Changing them while a job is
# running must not invalidate a semantically identical model at publication time.
NON_SEMANTIC_TRAINING_OPTION_KEYS = frozenset({
    "background_cpu_duty_cycle",
    "history_background_pause_ms",
    "process_nice",
    "training_archive_batch_rows",
    "training_cpu_duty_cycle",
    "training_experience_batch_rows",
    "training_feature_snapshot_cache_entries",
    "training_feature_snapshot_cache_units",
    "training_home_context_cache_entries",
    "training_home_context_cache_units",
    "training_max_continuous_work_ms",
    "training_process_isolation",
    "training_persistent_worker_enabled",
    "training_persistent_worker_cache_enabled",
    "max_concurrent_training_jobs",
    "training_parallel_agent_workers",
    "training_parallel_worker_memory_limit_mb",
    "training_parallel_min_available_mb",
    "training_replay_ram_cache_entry_rows",
    "training_replay_ram_cache_rows",
    "training_ram_replay_index_mb",
    "training_transition_edge_max_rows_per_entity",
    "training_sqlite_cache_mb",
    "training_throttle_max_sleep_seconds",
    "training_worker_memory_available_fraction",
    "training_worker_memory_floor_mb",
    "training_worker_memory_limit_mb",
    "training_worker_memory_reserve_mb",
    "training_worker_memory_total_fraction",
    "training_worker_memory_unknown_fallback_mb",
    "training_worker_nice",
    "training_worker_poll_ms",
    "training_worker_terminate_grace_seconds",
})


class StaleTrainingJob(RuntimeError):
    preserve_training_state = True

    def __init__(self, message, *, preserve_lifecycle=True):
        super().__init__(message)
        self.preserve_lifecycle = bool(preserve_lifecycle)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def agent_config_fingerprint(agent):
    agent = dict(agent or {})
    return _digest({key: agent.get(key) for key in AGENT_CONFIG_KEYS})


def _compact_training_state_map(state_map):
    """Keep only fields consumed by offline feature selection/replay.

    Home Assistant state attributes can contain very large payloads (maps, media metadata,
    forecasts, device-specific blobs). Shipping the full realtime state snapshot into each
    isolated 6 h worker duplicates those payloads in JSON and again in Python objects.
    The trainer only needs the scalar state, timestamps and the small metadata set below.
    """
    out = {}
    for entity_id, state in (state_map or {}).items():
        state = dict(state or {})
        attrs = dict(state.get("attributes") or {})
        compact_attrs = {
            key: attrs.get(key) for key in TRAINING_STATE_ATTRIBUTE_KEYS if key in attrs
        }
        out[str(entity_id)] = {
            "entity_id": str(state.get("entity_id") or entity_id),
            "state": state.get("state"),
            "attributes": compact_attrs,
            "last_changed": state.get("last_changed"),
            "last_updated": state.get("last_updated"),
        }
    return out


def _compact_training_registry(registry):
    out = {}
    for entity_id, row in (registry or {}).items():
        row = dict(row or {})
        out[str(entity_id)] = {
            key: row.get(key) for key in REGISTRY_METADATA_KEYS if row.get(key) is not None
        }
    return out


def _state_contract(state_map):
    out = {}
    for entity_id, state in sorted((state_map or {}).items()):
        attrs = dict((state or {}).get("attributes") or {})
        out[str(entity_id)] = {
            key: attrs.get(key) for key in STATE_METADATA_KEYS if key in attrs
        }
    return out


def runtime_context_fingerprint(state_map, registry, options):
    """Fingerprint structural training inputs, deliberately ignoring live state values."""
    return _digest({
        "state_metadata": _state_contract(state_map),
        "registry": _compact_training_registry(registry),
        # Full descriptor fingerprint retained for diagnostics/backward compatibility.
        "options": options or {},
        "training_revision": TRAINING_REVISION,
    })


def _semantic_training_options(options):
    out = dict(options or {})
    for key in list(out):
        if (
            key in NON_SEMANTIC_TRAINING_OPTION_KEYS
            or str(key).startswith("training_worker_effective_")
        ):
            out.pop(key, None)
    return out


def training_options_fingerprint(options):
    return _digest({
        "options": _semantic_training_options(options),
        "training_revision": TRAINING_REVISION,
    })


def _payload_training_entities(payload):
    payload = dict(payload or {})
    out = set()
    schema = dict(payload.get("schema") or {})
    out.update(str(x) for x in (schema.get("entities") or ()) if x)
    mask = dict(payload.get("mask") or {})
    out.update(str(x) for x in (mask.get("selected_entities") or ()) if x)
    out.update(str(x) for x in (payload.get("selected_entities") or ()) if x)
    model = payload.get("model")
    if isinstance(model, dict) and model is not payload:
        out.update(_payload_training_entities(model))
    return out


def training_relevant_entities(agent, model=None, schema_item=None, neural_artifact=None):
    """Entities whose structural topology can affect the model being published."""
    agent = dict(agent or {})
    out = set()
    target = agent.get("target_entity")
    if target:
        out.add(str(target))
    out.update(
        str(x) for x in (agent.get("input_entities") or ())
        if x and str(x) != "*"
    )
    out.update(_payload_training_entities(model))
    out.update(_payload_training_entities(schema_item))
    out.update(_payload_training_entities(neural_artifact))
    return out


def _expand_topology_scope(entity_ids, *registries):
    """Include device siblings because actuator exclusion is device-level."""
    scope = {str(x) for x in (entity_ids or ()) if x}
    device_ids = set()
    compact_registries = []
    for registry in registries:
        compact = _compact_training_registry(registry)
        compact_registries.append(compact)
        for entity_id in list(scope):
            device_id = (compact.get(entity_id) or {}).get("device_id")
            if device_id:
                device_ids.add(str(device_id))
    if device_ids:
        for compact in compact_registries:
            for entity_id, row in compact.items():
                if str((row or {}).get("device_id") or "") in device_ids:
                    scope.add(str(entity_id))
    return scope


def runtime_topology_fingerprint(state_map, registry, entity_ids):
    scope = sorted({str(x) for x in (entity_ids or ()) if x})
    state_map = dict(state_map or {})
    registry = _compact_training_registry(registry)
    return _digest({
        "entity_ids": scope,
        "state_metadata": {
            entity_id: _state_contract({
                entity_id: state_map.get(entity_id)
            }).get(entity_id)
            for entity_id in scope
        },
        "registry": {
            entity_id: registry.get(entity_id)
            for entity_id in scope
        },
        "training_revision": TRAINING_REVISION,
    })


def topology_changed_entities(before_state, before_registry, after_state, after_registry, entity_ids):
    changed = []
    before_state = dict(before_state or {})
    after_state = dict(after_state or {})
    before_registry = _compact_training_registry(before_registry)
    after_registry = _compact_training_registry(after_registry)
    for entity_id in sorted({str(x) for x in (entity_ids or ()) if x}):
        before_contract = _state_contract({
            entity_id: before_state.get(entity_id)
        }).get(entity_id)
        after_contract = _state_contract({
            entity_id: after_state.get(entity_id)
        }).get(entity_id)
        if (
            before_contract != after_contract
            or before_registry.get(entity_id) != after_registry.get(entity_id)
        ):
            changed.append(entity_id)
    return changed


def descriptor_checksum(payload):
    clean = {k: v for k, v in dict(payload or {}).items() if k != "checksum"}
    return _digest(clean)


def _process_start_token(pid):
    """Return Linux process start ticks when available, otherwise None."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
        return str(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _same_process_alive(pid, start_token=None):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    if start_token is None:
        return True
    current = _process_start_token(pid)
    return current is not None and str(current) == str(start_token)


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(_canonical(payload), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _host_memory_mb(path="/proc/meminfo"):
    """Return Linux MemTotal/MemAvailable in MB without importing psutil."""
    result = {"total_mb": None, "available_mb": None}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key not in ("MemTotal", "MemAvailable"):
                continue
            value = raw.strip().split()
            if not value:
                continue
            mb = float(value[0]) / 1024.0
            if key == "MemTotal":
                result["total_mb"] = mb
            else:
                result["available_mb"] = mb
    except (OSError, ValueError):
        pass
    return result


def resolve_training_resource_profile(options=None, memory=None):
    """Resolve a bounded RAM-first worker profile from current host headroom.

    The configured values remain hard ceilings. The effective worker RSS limit is also
    capped by fractions of MemTotal/MemAvailable, leaving explicit memory for HA and the
    realtime parent. Cache sizes are performance-only hints and scale in four tiers.
    """
    options = dict(options or {})
    memory = dict(memory or _host_memory_mb())

    ceiling = max(
        128.0, float(options.get("training_worker_memory_limit_mb", 1024) or 1024)
    )
    floor = max(
        128.0, min(
            ceiling,
            float(options.get("training_worker_memory_floor_mb", 256) or 256),
        )
    )
    total_fraction = max(
        0.10, min(
            0.50,
            float(options.get("training_worker_memory_total_fraction", 0.30) or 0.30),
        )
    )
    available_fraction = max(
        0.20, min(
            0.80,
            float(
                options.get("training_worker_memory_available_fraction", 0.50)
                or 0.50
            ),
        )
    )
    reserve = max(
        128.0, float(options.get("training_worker_memory_reserve_mb", 512) or 512)
    )
    unknown_fallback = max(
        floor, min(
            ceiling,
            float(
                options.get("training_worker_memory_unknown_fallback_mb", 520)
                or 520
            ),
        )
    )

    total_mb = memory.get("total_mb")
    available_mb = memory.get("available_mb")
    candidates = [ceiling]
    if isinstance(total_mb, (int, float)) and float(total_mb) > 0.0:
        candidates.append(max(floor, float(total_mb) * total_fraction))
    if isinstance(available_mb, (int, float)) and float(available_mb) > 0.0:
        available = float(available_mb)
        by_fraction = available * available_fraction
        by_reserve = max(floor, available - reserve)
        candidates.append(max(floor, min(by_fraction, by_reserve)))
    elif not (isinstance(total_mb, (int, float)) and float(total_mb) > 0.0):
        candidates.append(unknown_fallback)

    effective = int(max(floor, min(candidates)))

    if effective >= 896:
        tier = "large"
        target = {
            "replay_rows": 65536, "replay_entry_rows": 2048,
            "ram_index_mb": 192,
            "feature_entries": 512, "feature_units": 65536,
            "home_entries": 64, "home_units": 32768, "sqlite_mb": 64,
        }
    elif effective >= 640:
        tier = "medium_plus"
        target = {
            "replay_rows": 32768, "replay_entry_rows": 1536,
            "ram_index_mb": 128,
            "feature_entries": 256, "feature_units": 32768,
            "home_entries": 32, "home_units": 16384, "sqlite_mb": 24,
        }
    elif effective >= 448:
        tier = "medium"
        target = {
            "replay_rows": 16384, "replay_entry_rows": 1024,
            "ram_index_mb": 64,
            "feature_entries": 128, "feature_units": 16384,
            "home_entries": 16, "home_units": 8192, "sqlite_mb": 16,
        }
    else:
        tier = "small"
        target = {
            "replay_rows": 8192, "replay_entry_rows": 512,
            "ram_index_mb": 24,
            "feature_entries": 64, "feature_units": 8192,
            "home_entries": 8, "home_units": 4096, "sqlite_mb": 8,
        }

    def bounded_int(key, default, target_value, *, minimum=0):
        configured = int(options.get(key, default) or 0)
        if configured <= 0 and minimum <= 0:
            return 0
        return max(minimum, min(configured, int(target_value)))

    worker_options = {
        "training_worker_effective_memory_limit_mb": int(effective),
        "training_worker_effective_replay_cache_rows": bounded_int(
            "training_replay_ram_cache_rows", 65536, target["replay_rows"]
        ),
        "training_worker_effective_replay_cache_entry_rows": bounded_int(
            "training_replay_ram_cache_entry_rows", 2048, target["replay_entry_rows"],
            minimum=64,
        ),
        "training_worker_effective_ram_replay_index_mb": bounded_int(
            "training_ram_replay_index_mb", 192, target["ram_index_mb"]
        ),
        "training_worker_effective_feature_snapshot_cache_entries": bounded_int(
            "training_feature_snapshot_cache_entries", 512, target["feature_entries"]
        ),
        "training_worker_effective_feature_snapshot_cache_units": bounded_int(
            "training_feature_snapshot_cache_units", 65536, target["feature_units"]
        ),
        "training_worker_effective_home_context_cache_entries": bounded_int(
            "training_home_context_cache_entries", 64, target["home_entries"]
        ),
        "training_worker_effective_home_context_cache_units": bounded_int(
            "training_home_context_cache_units", 32768, target["home_units"]
        ),
        "training_worker_effective_sqlite_cache_mb": bounded_int(
            "training_sqlite_cache_mb", 64, target["sqlite_mb"], minimum=2
        ),
    }
    return {
        "contract": RESOURCE_PROFILE_CONTRACT,
        "tier": tier,
        "host_memory_mb": {
            "total": None if total_mb is None else round(float(total_mb), 1),
            "available": (
                None if available_mb is None else round(float(available_mb), 1)
            ),
        },
        "configured_memory_ceiling_mb": round(ceiling, 1),
        "effective_memory_limit_mb": int(effective),
        "reserved_for_parent_mb": round(reserve, 1),
        "worker_options": worker_options,
    }


def _proc_metrics(pid):
    result = {
        "rss_mb": None,
        "rss_anon_mb": None,
        "rss_file_mb": None,
        "rss_shmem_mb": None,
        "vm_size_mb": None,
        "cpu_seconds": None,
        "read_bytes": None,
        "write_bytes": None,
    }
    try:
        status = Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8")
        wanted = {
            "VmRSS": "rss_mb",
            "RssAnon": "rss_anon_mb",
            "RssFile": "rss_file_mb",
            "RssShmem": "rss_shmem_mb",
            "VmSize": "vm_size_mb",
        }
        for line in status.splitlines():
            key, _, raw = line.partition(":")
            field = wanted.get(key)
            if field is None:
                continue
            parts = raw.split()
            if parts:
                result[field] = float(parts[0]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
        ticks = float(os.sysconf("SC_CLK_TCK"))
        result["cpu_seconds"] = (float(fields[13]) + float(fields[14])) / ticks
    except (OSError, ValueError, IndexError):
        pass
    try:
        io = Path(f"/proc/{int(pid)}/io").read_text(encoding="utf-8")
        for line in io.splitlines():
            key, _, raw = line.partition(":")
            if key == "read_bytes":
                result["read_bytes"] = int(raw.strip())
            elif key == "write_bytes":
                result["write_bytes"] = int(raw.strip())
    except (OSError, ValueError):
        pass
    return result


def _job_dir():
    path = Path(DATA_DIR) / "training_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_job_files(keep=12):
    try:
        files = sorted(
            _job_dir().glob("*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        job_ids = []
        for path in files:
            stem = path.name.split(".", 1)[0]
            if stem not in job_ids:
                job_ids.append(stem)
        for old in job_ids[int(keep):]:
            for path in _job_dir().glob(old + ".*"):
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _snapshot_parent_context(history, agent_id):
    with history.engine.lock:
        state_map = _compact_training_state_map(history.engine.state_map)
        registry = _compact_training_registry(history.engine.entity_registry)
        relevance = dict(history.engine.context_relevance.get(agent_id) or {})
    return state_map, registry, relevance


def _build_job(history, start_ts, end_ts, kwargs):
    from ha import AUTOMATION_KNOWLEDGE
    from storage import STORE

    agent_ids = sorted(set(kwargs.get("agent_ids") or ()))
    if len(agent_ids) != 1:
        raise ValueError("isolated training requires exactly one agent")
    agent_id = str(agent_ids[0])
    agent = STORE.get_agent_config(agent_id)
    if not agent:
        raise ValueError("agent not found")

    state_map, registry, relevance = _snapshot_parent_context(history, agent_id)
    hints, _ = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
    schema_item = dict(
        (getattr(history, "training_schema_cache", {}) or {}).get(agent_id) or {}
    )
    job_id = uuid.uuid4().hex[:16]
    root = _job_dir()
    job = {
        "format": JOB_FORMAT,
        "version": JOB_VERSION,
        "job_id": job_id,
        "app_version": APP_VERSION,
        "training_revision": TRAINING_REVISION,
        "created_at": now_ts(),
        "parent_pid": os.getpid(),
        "parent_start_token": _process_start_token(os.getpid()),
        "agent_id": agent_id,
        "agent_fingerprint": agent_config_fingerprint(agent),
        "context_fingerprint": runtime_context_fingerprint(
            state_map, registry, dict(OPTIONS)
        ),
        "options_fingerprint": training_options_fingerprint(dict(OPTIONS)),
        "state_map": state_map,
        "entity_registry": registry,
        "context_relevance": relevance,
        "automation_hints": list(hints or ()),
        "schema_cache_item": schema_item,
        "options": dict(OPTIONS),
        "context_snapshot": {
            "state_entities": len(state_map),
            "registry_entities": len(registry),
            "contract": "compact_training_context_v1",
        },
        "start_ts": float(start_ts),
        "end_ts": float(end_ts),
        "train_kwargs": {
            "qualify": bool(kwargs.get("qualify", False)),
            "include_candidates": bool(kwargs.get("include_candidates", False)),
            "benchmark": bool(kwargs.get("benchmark", False)),
            "accumulate_benchmark": bool(kwargs.get("accumulate_benchmark", False)),
            "progress_lo": kwargs.get("progress_lo"),
            "progress_hi": kwargs.get("progress_hi"),
            "progress_label": kwargs.get("progress_label"),
            "continuation_from_ts": kwargs.get("continuation_from_ts"),
            "include_long_memory": bool(kwargs.get("include_long_memory", False)),
            "long_memory_recent_start_ts": kwargs.get("long_memory_recent_start_ts"),
            "long_memory_reference_end_ts": kwargs.get("long_memory_reference_end_ts"),
        },
        "status_path": str(root / f"{job_id}.status.json"),
        "result_path": str(root / f"{job_id}.result.json"),
        "log_path": str(root / f"{job_id}.log"),
    }
    job["checksum"] = descriptor_checksum(job)
    job["job_path"] = str(root / f"{job_id}.job.json")
    # job_path is transport metadata and is intentionally outside the checksum contract.
    return job


def _build_sequence_job(history, chunks):
    """Build one isolated descriptor containing the existing logical 6 h chunks."""
    chunks = list(chunks or ())
    if not chunks:
        raise ValueError("persistent training sequence requires at least one chunk")
    first = dict(chunks[0])
    first_kwargs = dict(first.get("train_kwargs") or {})
    job = _build_job(
        history,
        float(first["start_ts"]),
        float(first["end_ts"]),
        first_kwargs,
    )
    root = Path(job["job_path"]).parent
    normalized = []
    for index, raw in enumerate(chunks):
        row = dict(raw or {})
        kwargs = dict(row.get("train_kwargs") or {})
        if sorted(set(kwargs.get("agent_ids") or ())) != [str(job["agent_id"])]:
            raise ValueError("persistent sequence chunks must target the same agent")
        normalized.append({
            "index": int(index),
            "start_ts": float(row["start_ts"]),
            "end_ts": float(row["end_ts"]),
            "train_kwargs": {
                key: value for key, value in kwargs.items()
                if key != "agent_ids"
            },
            "checkpoint_cursor_ts": float(
                row.get("checkpoint_cursor_ts", row["end_ts"])
            ),
            "checkpoint_meta": dict(row.get("checkpoint_meta") or {}),
        })
    job["sequence_chunks"] = normalized
    job["sequence_start_ts"] = float(
        chunks[0].get("sequence_start_ts", normalized[0]["start_ts"])
    )
    job["sequence_target_end_ts"] = float(
        chunks[-1].get("sequence_target_end_ts", normalized[-1]["end_ts"])
    )
    job["rollback_path"] = str(root / f"{job['job_id']}.rollback.json")
    job["sequence_contract"] = "persistent_agent_training_worker_v1"
    job["checksum"] = descriptor_checksum(job)
    return job


def _apply_status(history, payload):
    if not isinstance(payload, dict):
        return
    allowed = {
        "phase", "progress", "message", "chunk_done", "chunk_total",
        "stage_eta_seconds", "work_done", "work_total", "work_unit",
        "eta_source", "phase_detail",
    }
    kwargs = {key: payload[key] for key in allowed if key in payload}
    if kwargs:
        history.set_status(**kwargs)


def _terminate(process, grace_seconds=2.0):
    if process.poll() is not None:
        return
    process.terminate()
    deadline = time.monotonic() + max(0.1, float(grace_seconds))
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def _restore_rejected_chunk(
    store, agent_id, agent_before, model_before, *, preserve_lifecycle=False
):
    if preserve_lifecycle:
        # A concurrent configuration edit is authoritative. Restore only the previous
        # model checkpoint, then explicitly invalidate training authority. The child may
        # have reached benchmark/qualification after the edit, so keeping whichever
        # lifecycle row happens to be latest is not safe.
        store.restore_model_snapshot(agent_id, model_before)
        store.set_training_state(
            agent_id,
            "needs_retrain",
            score=None,
            samples=0,
            source=None,
            detail={
                "reason": (
                    "Configuration changed during isolated training; "
                    "stale worker result rejected"
                )
            },
        )
    else:
        restore = getattr(store, "restore_training_chunk_snapshot", None)
        if callable(restore):
            restore(agent_id, agent_before, model_before)
        else:
            store.restore_model_snapshot(agent_id, model_before)
    store.discard_uncommitted_experiences(agent_id)
    store.touch_agent_index()


def aggregate_training_sequence_profile(chunk_reports):
    """Aggregate one selected-agent persistent-worker pass for diagnostics.

    Timers in training_phase_timings are deliberately reported as observed metrics,
    not an additive flame graph: feature-build/SQLite timers are nested inside replay.
    Per-chunk timings remain authoritative and this summary makes the complete job
    visible without retaining unbounded trace entries.
    """
    reports = list(chunk_reports or ())
    seconds = {}
    counters = {}
    for report in reports:
        timings = dict((report or {}).get("training_phase_timings") or {})
        for key, value in timings.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if str(key).endswith("_seconds"):
                seconds[key] = float(seconds.get(key, 0.0)) + float(value)
            elif str(key).endswith((
                "_rows", "_lookups", "_hits", "_misses", "_builds",
                "_timestamps", "_dwells", "_processed", "_planned",
            )):
                counters[key] = float(counters.get(key, 0.0)) + float(value)

    elapsed = sum(float((row or {}).get("elapsed_seconds") or 0.0) for row in reports)
    slowest = sorted(
        (
            {
                "index": int((row or {}).get("index") or 0),
                "elapsed_seconds": float((row or {}).get("elapsed_seconds") or 0.0),
                "replay_seconds": float(
                    ((row or {}).get("training_phase_timings") or {}).get(
                        "replay_seconds"
                    ) or 0.0
                ),
                "finalization_seconds": float(
                    ((row or {}).get("training_phase_timings") or {}).get(
                        "finalization_seconds"
                    ) or 0.0
                ),
            }
            for row in reports
        ),
        key=lambda row: row["elapsed_seconds"],
        reverse=True,
    )[:5]
    final_cache = dict(
        (reports[-1].get("training_feature_snapshot_cache") or {})
        if reports else {}
    )
    return {
        "contract": "persistent_training_session_profile_v1",
        "chunks": len(reports),
        "chunk_elapsed_seconds": round(elapsed, 4),
        "phase_seconds": {
            key: round(value, 6) for key, value in sorted(seconds.items())
        },
        "counters": {
            key: int(value) if float(value).is_integer() else float(value)
            for key, value in sorted(counters.items())
        },
        "slowest_chunks": slowest,
        "feature_snapshot_session": {
            "persistent": bool(final_cache.get("session_persistent")),
            "entries": int(final_cache.get("entries") or 0),
            "units": int(final_cache.get("units") or 0),
            "hits": int(final_cache.get("hits") or 0),
            "misses": int(final_cache.get("misses") or 0),
            "builds": int(final_cache.get("builds") or 0),
            "evictions": int(final_cache.get("evictions") or 0),
            "build_seconds": float(final_cache.get("build_seconds") or 0.0),
            "hit_rate": final_cache.get("hit_rate"),
        },
        "note": "phase timers can be nested; do not sum them as exclusive CPU time",
    }


def run_isolated_training_sequence(history, chunks):
    """Run all logical chunks for one agent in one supervised child process."""
    chunks = list(chunks or ())
    if not chunks:
        return 0
    first = dict(chunks[0])
    kwargs = dict(first.get("train_kwargs") or {})
    kwargs["_persistent_sequence_chunks"] = chunks
    return run_isolated_training_chunk(
        history,
        float(first["start_ts"]),
        float(chunks[-1]["end_ts"]),
        **kwargs,
    )


def run_isolated_training_chunk(history, start_ts, end_ts, **kwargs):
    """Supervise one clean worker process while realtime remains in the parent."""
    from storage import STORE

    sequence_chunks = kwargs.pop("_persistent_sequence_chunks", None)
    if sequence_chunks:
        job = _build_sequence_job(history, sequence_chunks)
    else:
        job = _build_job(history, start_ts, end_ts, kwargs)
    job_path = Path(job.pop("job_path"))
    parallel_slots = max(
        1, int(getattr(history, "training_parallel_slot_count", 1) or 1)
    )
    profile_options = dict(OPTIONS)
    if parallel_slots > 1:
        parallel_cap = max(
            448,
            int(OPTIONS.get("training_parallel_worker_memory_limit_mb", 768) or 768),
        )
        profile_options["training_worker_memory_limit_mb"] = min(
            int(profile_options.get("training_worker_memory_limit_mb", 1024) or 1024),
            parallel_cap,
        )
    resource_profile = resolve_training_resource_profile(profile_options)
    resource_profile["parallel_slots"] = int(parallel_slots)
    resource_profile["parallel_memory_cap_applied"] = bool(parallel_slots > 1)
    job["resource_profile"] = resource_profile
    # _build_job checksums the semantic descriptor before transport metadata is added.
    # Recompute after adding the non-semantic resource profile so the worker also verifies
    # that its RAM/SQLite limits were not corrupted in transit.
    job["checksum"] = descriptor_checksum(job)
    _atomic_json(job_path, job)
    _prune_job_files()

    agent_id = job["agent_id"]
    agent_before = STORE.get_agent_config(agent_id)
    model_before = STORE.get_model(agent_id)
    try:
        from tiny_mlp_shadow import load_training_record
        neural_before = load_training_record(STORE, agent_id)
    except Exception:
        neural_before = None
    poll_seconds = max(
        0.05, float(OPTIONS.get("training_worker_poll_ms", 200) or 200) / 1000.0
    )
    memory_limit = max(
        128.0, float(resource_profile.get("effective_memory_limit_mb") or 128.0)
    )
    grace = max(
        0.2, float(OPTIONS.get("training_worker_terminate_grace_seconds", 2.0) or 2.0)
    )
    env = dict(os.environ)
    env["ADAPTIVE_AI_DATA"] = str(DATA_DIR)
    env["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Tiny MLP training is vectorized in the child process. These matrices are small;
    # one BLAS thread is faster/more predictable than a thread pool and preserves CPU
    # headroom for the realtime HA parent on Raspberry Pi.
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    env["NUMEXPR_NUM_THREADS"] = "1"

    log_handle = open(job["log_path"], "ab", buffering=0)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(job_path)],
        cwd=str(Path(__file__).resolve().parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()

    history.active_training_process = process
    started = time.monotonic()
    peak_rss = 0.0
    last_status_mtime = None
    latest_metrics = {}
    last_cpu_seconds = None
    last_cpu_wall = None
    cancelled = False
    memory_exceeded = False
    history.training_process_status = {
        "enabled": True,
        "state": "running",
        "contract": (
            "persistent_agent_training_worker_v1"
            if sequence_chunks else "isolated_training_process_v1"
        ),
        "sequence_chunks": len(sequence_chunks or ()),
        "job_id": job["job_id"],
        "pid": process.pid,
        "agent_id": agent_id,
        "started_at": now_ts(),
        "worker_nice": int(OPTIONS.get("training_worker_nice", 10) or 10),
        "memory_limit_mb": memory_limit,
        "parallel_slots": int(parallel_slots),
        "resource_profile": dict(resource_profile),
        "checkpointed_wal_reads": True,
        "context_snapshot": dict(job.get("context_snapshot") or {}),
        "job_descriptor_bytes": int(job_path.stat().st_size) if job_path.exists() else None,
    }

    try:
        while process.poll() is None:
            status_path = Path(job["status_path"])
            try:
                mtime = status_path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime is not None and mtime != last_status_mtime:
                _apply_status(history, _read_json(status_path, {}))
                last_status_mtime = mtime

            latest_metrics = _proc_metrics(process.pid)
            sample_wall = time.monotonic()
            sample_cpu = latest_metrics.get("cpu_seconds")
            if sample_cpu is not None and last_cpu_seconds is not None and last_cpu_wall is not None:
                wall_delta = max(1e-6, sample_wall - last_cpu_wall)
                cpu_delta = max(0.0, float(sample_cpu) - float(last_cpu_seconds))
                one_core = 100.0 * cpu_delta / wall_delta
                cpu_count = max(1, int(os.cpu_count() or 1))
                latest_metrics["cpu_one_core_percent"] = round(one_core, 2)
                latest_metrics["cpu_total_percent_estimate"] = round(
                    one_core / cpu_count, 2
                )
                latest_metrics["logical_cpu_count"] = cpu_count
            if sample_cpu is not None:
                last_cpu_seconds = float(sample_cpu)
                last_cpu_wall = sample_wall
            rss = latest_metrics.get("rss_mb")
            if rss is not None:
                peak_rss = max(peak_rss, float(rss))
                if float(rss) > memory_limit:
                    memory_exceeded = True
                    _terminate(process, grace)
                    break

            cancel_event = getattr(history, "job_cancel_event", None)
            if history.stop_event.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            ):
                cancelled = True
                _terminate(process, grace)
                break

            history.training_process_status.update({
                "wall_seconds": round(time.monotonic() - started, 3),
                "peak_rss_mb": round(peak_rss, 3),
                **latest_metrics,
            })
            time.sleep(poll_seconds)

        return_code = process.wait(timeout=max(1.0, grace + 1.0))
        _apply_status(history, _read_json(job["status_path"], {}))
        result = _read_json(job["result_path"], None)

        if cancelled:
            raise InterruptedError("Isolated training worker cancelled")
        if memory_exceeded:
            worker_status = _read_json(job["status_path"], {}) or {}
            phase = str(worker_status.get("phase") or "unknown")
            progress = worker_status.get("progress")
            message = str(worker_status.get("message") or "").strip()
            failure_context = {
                "phase": phase,
                "progress": progress,
                "message": message or None,
                "bootstrap_stage": worker_status.get("bootstrap_stage"),
                "peak_rss_mb": round(peak_rss, 3),
                "rss_mb": latest_metrics.get("rss_mb"),
                "rss_anon_mb": latest_metrics.get("rss_anon_mb"),
                "rss_file_mb": latest_metrics.get("rss_file_mb"),
                "rss_shmem_mb": latest_metrics.get("rss_shmem_mb"),
                "vm_size_mb": latest_metrics.get("vm_size_mb"),
                "job_descriptor_bytes": history.training_process_status.get(
                    "job_descriptor_bytes"
                ),
                "context_snapshot": history.training_process_status.get(
                    "context_snapshot"
                ),
                "resource_profile": history.training_process_status.get(
                    "resource_profile"
                ),
            }
            history.training_process_status["memory_failure_context"] = failure_context
            progress_text = (
                f" at {float(progress):.0%}" if progress is not None else ""
            )
            detail_text = f" during {phase}{progress_text}"
            if message:
                detail_text += f" · {message}"
            memory_parts = []
            for label, key in (
                ("rss", "rss_mb"),
                ("anon", "rss_anon_mb"),
                ("file", "rss_file_mb"),
            ):
                value = latest_metrics.get(key)
                if value is not None:
                    memory_parts.append(f"{label}={float(value):.0f}MB")
            if memory_parts:
                detail_text += " · " + ", ".join(memory_parts)
            raise MemoryError(
                f"Isolated training worker exceeded {memory_limit:.0f} MB RSS"
                + detail_text
            )
        if return_code != 0 or not isinstance(result, dict) or not result.get("ok"):
            detail = (result or {}).get("error") if isinstance(result, dict) else None
            error_type = (result or {}).get("error_type") if isinstance(result, dict) else None
            if error_type == "StaleTrainingJob":
                raise StaleTrainingJob(
                    detail or "isolated worker rejected stale training job",
                    preserve_lifecycle=True,
                )
            if error_type == "MemoryError":
                raise MemoryError(
                    detail or "isolated worker exceeded its memory budget"
                )
            raise RuntimeError(
                detail or f"isolated training worker exited with code {return_code}"
            )
        if result.get("job_id") != job["job_id"]:
            raise RuntimeError("isolated training result job id mismatch")

        current_agent = STORE.get_agent_config(agent_id)
        if agent_config_fingerprint(current_agent) != job["agent_fingerprint"]:
            raise StaleTrainingJob(
                "agent configuration changed while isolated training was running",
                preserve_lifecycle=True,
            )
        current_state, current_registry, _ = _snapshot_parent_context(history, agent_id)

        expected_options = job.get("options_fingerprint")
        if expected_options is None:
            expected_options = training_options_fingerprint(
                job.get("options") or dict(OPTIONS)
            )
        current_options = training_options_fingerprint(dict(OPTIONS))
        if current_options != expected_options:
            raise StaleTrainingJob(
                "training-semantic options changed while isolated training was running",
                preserve_lifecycle=False,
            )

        # Long Pi training jobs routinely overlap harmless HA Entity Registry refreshes.
        # Validate only topology that can affect the model actually produced by this
        # worker, plus device siblings used by controllable-context exclusion.
        model_after = STORE.get_model(agent_id) or {}
        schema_item_after = result.get("schema_cache_item") or {}
        neural_artifact = (
            (dict(result.get("neural_training_artifacts") or {})).get(agent_id)
            or {}
        )
        relevant_entities = training_relevant_entities(
            current_agent,
            model=model_after,
            schema_item=schema_item_after,
            neural_artifact=neural_artifact,
        )
        if relevant_entities and "state_map" in job and "entity_registry" in job:
            scope = _expand_topology_scope(
                relevant_entities, job.get("entity_registry"), current_registry
            )
            before_topology = runtime_topology_fingerprint(
                job.get("state_map"), job.get("entity_registry"), scope
            )
            after_topology = runtime_topology_fingerprint(
                current_state, current_registry, scope
            )
            changed_entities = topology_changed_entities(
                job.get("state_map"), job.get("entity_registry"),
                current_state, current_registry, scope,
            )
            history.training_process_status["context_validation"] = {
                "mode": "selected_entities_plus_device_siblings",
                "relevant_entities": len(relevant_entities),
                "scope_entities": len(scope),
                "changed_entities": changed_entities[:32],
                "unrelated_registry_refreshes_tolerated": True,
            }
            if before_topology != after_topology:
                detail = ",".join(changed_entities[:8]) or "unknown"
                raise StaleTrainingJob(
                    "training-relevant topology changed while isolated training was "
                    f"running: {detail}",
                    preserve_lifecycle=False,
                )
        else:
            # Compatibility fallback for hand-built/legacy descriptors that do not carry
            # the snapshot needed for scoped validation.
            current_context = runtime_context_fingerprint(
                current_state, current_registry, dict(OPTIONS)
            )
            if current_context != job["context_fingerprint"]:
                raise StaleTrainingJob(
                    "runtime topology/options changed while isolated training was running",
                    preserve_lifecycle=False,
                )

        # Child writes are durable, but all parent runtime caches are process-local.
        history.engine.models.pop(agent_id, None)
        relevance = result.get("context_relevance")
        if isinstance(relevance, dict):
            history.engine.context_relevance[agent_id] = relevance
        history.temporal_replay_stats = dict(result.get("temporal_replay") or {})
        history.training_long_memory_status = dict(
            result.get("training_long_memory") or {}
        )
        history.training_replay_cache_status = dict(
            result.get("training_replay_cache") or {}
        )
        history.training_home_context_cache_status = dict(
            result.get("training_home_context_cache") or {}
        )
        history.training_ram_replay_index_status = dict(
            result.get("training_ram_replay_index") or {}
        )
        history.training_transition_edge_index_status = dict(
            result.get("training_transition_edge_index") or {}
        )
        history.training_feature_snapshot_cache_status = dict(
            result.get("training_feature_snapshot_cache") or {}
        )
        history.training_phase_timings = dict(
            result.get("training_phase_timings") or {}
        )
        if result.get("sequence_contract"):
            history.training_process_status["sequence_contract"] = result.get(
                "sequence_contract"
            )
            history.training_process_status["sequence_chunks_completed"] = int(
                result.get("sequence_chunks_completed") or 0
            )
            history.training_process_status["sequence_chunk_reports"] = list(
                result.get("sequence_chunk_reports") or ()
            )
            session_profile = dict(
                result.get("training_session_profile") or {}
            )
            history.training_process_status["session_profile"] = session_profile
            if session_profile:
                history.training_job_timings["training_session_profile"] = (
                    session_profile
                )
                history.training_job_timings["worker_elapsed_seconds"] = float(
                    result.get("elapsed_seconds") or 0.0
                )
        schema_item = result.get("schema_cache_item")
        if isinstance(schema_item, dict) and schema_item:
            history.training_schema_cache[agent_id] = schema_item

        neural_artifacts = dict(result.get("neural_training_artifacts") or {})
        history.neural_training_artifacts = neural_artifacts
        if neural_artifacts:
            from tiny_mlp_shadow import publish_training_artifact
            service = getattr(history.engine, "tiny_mlp_shadow", None)
            for neural_agent_id, artifact in neural_artifacts.items():
                try:
                    published = publish_training_artifact(STORE, artifact)
                    if service is not None and callable(getattr(service, "invalidate", None)):
                        service.invalidate(neural_agent_id)
                    STORE.event(
                        neural_agent_id, "info", "tiny_mlp_supervised_tournament",
                        "Offline supervised Tiny MLP tournament completed; physical authority unchanged",
                        {
                            "selected_backend": published.get("selected_backend"),
                            "trained": published.get("trained"),
                            "tournament": artifact.get("tournament"),
                            "trainer": artifact.get("trainer"),
                            "shadow_only": True,
                            "physical_authority": False,
                        },
                    )
                except Exception as exc:
                    STORE.event(
                        neural_agent_id, "warning", "tiny_mlp_supervised_publish_failed",
                        "Ridge training completed but the optional Tiny MLP Shadow artifact was not published",
                        {"error": f"{type(exc).__name__}: {exc}", "physical_authority": False},
                    )
        STORE.touch_agent_index()
        history.engine.agent_index_at = 0.0
        history.engine.agent_index_revision = -1
        history.engine.wake_event.set()

        final_metrics = _proc_metrics(process.pid) if process.poll() is None else latest_metrics
        history.training_process_status = {
            **history.training_process_status,
            "state": "complete",
            "exit_code": int(return_code),
            "wall_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mb": round(peak_rss, 3),
            "cpu_seconds": final_metrics.get("cpu_seconds"),
            "read_bytes": final_metrics.get("read_bytes"),
            "write_bytes": final_metrics.get("write_bytes"),
            "worker_budget": result.get("training_budget"),
        }
        return int(result.get("return_value") or 0)
    except Exception as exc:
        rollback_agent = agent_before
        rollback_model = model_before
        rollback_neural = neural_before
        semantic_stale = bool(
            isinstance(exc, StaleTrainingJob) and not exc.preserve_lifecycle
        )
        if sequence_chunks and not semantic_stale:
            rollback = _read_json(job.get("rollback_path"), {}) or {}
            if isinstance(rollback.get("agent_before"), dict):
                rollback_agent = rollback.get("agent_before")
            if "model_before" in rollback:
                rollback_model = rollback.get("model_before")
            if "neural_before" in rollback:
                rollback_neural = rollback.get("neural_before")
        _restore_rejected_chunk(
            STORE, agent_id, rollback_agent, rollback_model,
            preserve_lifecycle=bool(
                isinstance(exc, StaleTrainingJob) and exc.preserve_lifecycle
            ),
        )
        try:
            from tiny_mlp_shadow import restore_training_record
            restore_training_record(STORE, agent_id, rollback_neural)
            service = getattr(history.engine, "tiny_mlp_shadow", None)
            if service is not None and callable(getattr(service, "invalidate", None)):
                service.invalidate(agent_id)
        except Exception:
            pass
        history.engine.models.pop(agent_id, None)
        history.engine.agent_index_at = 0.0
        history.engine.agent_index_revision = -1
        history.training_process_status = {
            **history.training_process_status,
            "state": "cancelled" if cancelled else "failed",
            "exit_code": process.poll(),
            "wall_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mb": round(peak_rss, 3),
            "memory_exceeded": bool(memory_exceeded),
        }
        raise
    finally:
        history.active_training_process = None


class TrainingWorkerEngine:
    def __init__(self, job, store):
        from context_engine import ContextEngine

        self.lock = threading.RLock()
        self.state_map = dict(job.get("state_map") or {})
        self.entity_registry = dict(job.get("entity_registry") or {})
        self.context_relevance = {
            str(job["agent_id"]): dict(job.get("context_relevance") or {})
        }
        self.models = {}
        self.context = ContextEngine(OPTIONS, store)
        self.context.configure(self.state_map, entities=self.entity_registry)
        self.wake_event = threading.Event()
        self.history_manager = None
        self._hints = {
            str(job["agent_id"]): list(job.get("automation_hints") or ())
        }

    def policy(self, agent):
        from policy import MultiHorizonPolicy
        from storage import STORE

        aid = str(agent["id"])
        cached = self.models.get(aid)
        if cached is not None:
            return cached
        raw_model = STORE.get_model(aid)
        if raw_model is None and self.history_manager is not None:
            raw_model = self.history_manager.training_schema_seed(aid, agent)
        model = MultiHorizonPolicy(
            agent,
            self.state_map,
            self.entity_registry,
            self._hints.get(aid, ()),
            raw_model,
            self.context_relevance.get(aid),
            context_engine=self.context,
        )
        self.models[aid] = model
        return model


def _worker_configure_budget():
    from training_budget import TRAINING_BUDGET

    threading.current_thread().name = "adaptive-ai-index-process"
    TRAINING_BUDGET.configure(
        duty_cycle=float(OPTIONS.get("training_cpu_duty_cycle", 0.65) or 0.65),
        max_slice_seconds=(
            float(OPTIONS.get("training_max_continuous_work_ms", 35) or 35) / 1000.0
        ),
        max_sleep_seconds=float(
            OPTIONS.get("training_throttle_max_sleep_seconds", 0.5) or 0.5
        ),
        thread_prefixes=("adaptive-ai-index-process",),
    )
    return TRAINING_BUDGET


def _worker_boot_status(job, message, *, stage, progress=None):
    """Write a tiny heartbeat before HistoryManager exists.

    This deliberately avoids importing or serializing the runtime status graph so a
    bootstrap RSS failure can be localized even if the child dies during imports,
    Store attachment, ContextEngine setup, or HistoryManager construction.
    """
    payload = {
        "phase": "worker_bootstrap",
        "progress": (
            float(progress)
            if progress is not None
            else float((job.get("train_kwargs") or {}).get("progress_lo") or 0.0)
        ),
        "message": str(message),
        "phase_detail": str(stage),
        "worker_pid": os.getpid(),
        "updated_at": now_ts(),
        "bootstrap_stage": str(stage),
    }
    _atomic_json(job["status_path"], payload)


def _worker_status_writer(history, status_path):
    original = history.set_status

    def set_status(*args, **kwargs):
        result = original(*args, **kwargs)
        payload = history.status()
        payload["worker_pid"] = os.getpid()
        payload["updated_at"] = now_ts()
        _atomic_json(status_path, payload)
        return result

    history.set_status = set_status


def worker_main(job_path):
    job = _read_json(job_path, None)
    if not isinstance(job, dict):
        raise RuntimeError("invalid isolated training descriptor")
    if job.get("format") != JOB_FORMAT or int(job.get("version") or 0) != JOB_VERSION:
        raise RuntimeError("unsupported isolated training job contract")
    if descriptor_checksum(job) != job.get("checksum"):
        raise RuntimeError("isolated training descriptor checksum mismatch")
    if job.get("app_version") != APP_VERSION:
        raise RuntimeError("isolated training app version mismatch")
    if job.get("training_revision") != TRAINING_REVISION:
        raise RuntimeError("isolated training revision mismatch")

    _worker_boot_status(
        job,
        "Training worker descriptor loaded",
        stage="descriptor_validated",
    )

    # Descriptor options are the authoritative semantic job view. The resource profile
    # is scheduling/cache-only and deliberately excluded from runtime_context_fingerprint.
    OPTIONS.clear()
    OPTIONS.update(dict(job.get("options") or {}))
    resource_profile = dict(job.get("resource_profile") or {})
    OPTIONS.update(dict(resource_profile.get("worker_options") or {}))

    nice_value = int(OPTIONS.get("training_worker_nice", 10) or 10)
    nice_applied = False
    try:
        if nice_value > 0:
            os.nice(nice_value)
            nice_applied = True
    except (AttributeError, OSError):
        pass

    _worker_boot_status(
        job,
        "Importing training modules",
        stage="before_training_imports",
    )
    from storage import STORE
    from training_budget import TRAINING_BUDGET
    from observation_contract import install_training_contract
    # The realtime parent installs Observation Contract v12 through runtime composition,
    # but this worker is a fresh process. Align policy/schema/replay globals before
    # HistoryManager constructs any policy or tracker so the durable model it publishes
    # is restart-compatible with the parent.
    worker_contract = install_training_contract()
    restore_training_contract = worker_contract["restore"]
    from history import HistoryManager
    _worker_boot_status(
        job,
        (
            "Training modules loaded; observation contract "
            f"policy v{worker_contract['policy_version']}/schema v{worker_contract['schema_version']}"
        ),
        stage="training_imports_complete",
    )

    STORE.checkpointed_archive_reads = True
    engine = TrainingWorkerEngine(job, STORE)
    _worker_boot_status(
        job,
        "Compact worker context initialized",
        stage="engine_initialized",
    )
    history = HistoryManager(engine, worker_mode=True)
    _worker_boot_status(
        job,
        "Worker history manager initialized without global archive scan",
        stage="history_manager_initialized",
    )
    engine.history_manager = history
    aid = str(job["agent_id"])

    parent_pid = int(job.get("parent_pid") or 0)
    parent_start_token = job.get("parent_start_token")

    def parent_watchdog():
        while not history.stop_event.wait(0.50):
            if not _same_process_alive(parent_pid, parent_start_token):
                history.stop_event.set()
                return

    if parent_pid > 0:
        threading.Thread(
            target=parent_watchdog,
            name="adaptive-ai-training-parent-watchdog",
            daemon=True,
        ).start()
    history.agent_jobs.add(aid)
    if isinstance(job.get("schema_cache_item"), dict) and job["schema_cache_item"]:
        history.training_schema_cache[aid] = dict(job["schema_cache_item"])
    _worker_status_writer(history, job["status_path"])

    expected_agent = str(job["agent_fingerprint"])

    def publication_guard(agent_id, _model):
        if str(agent_id) != aid:
            raise StaleTrainingJob("worker attempted to publish unexpected agent")
        if parent_pid > 0 and not _same_process_alive(
            parent_pid, parent_start_token
        ):
            raise StaleTrainingJob(
                "realtime parent process disappeared before model publication",
                preserve_lifecycle=False,
            )
        current = STORE.get_agent_config(agent_id)
        if agent_config_fingerprint(current) != expected_agent:
            raise StaleTrainingJob(
                "agent configuration changed before model publication"
            )

    STORE.training_publish_guard = publication_guard
    budget = _worker_configure_budget()
    budget.begin(thread_name="adaptive-ai-index-process")
    started = time.monotonic()
    result = {
        "format": RESULT_FORMAT,
        "version": RESULT_VERSION,
        "job_id": job["job_id"],
        "ok": False,
        "worker_pid": os.getpid(),
        "nice_applied": nice_applied,
    }
    try:
        kwargs = dict(job.get("train_kwargs") or {})
        _worker_boot_status(
            job,
            "Worker ready; entering historical training",
            stage="before_train_from_archive",
            progress=kwargs.get("progress_lo"),
        )
        chunks = list(job.get("sequence_chunks") or ())
        if not chunks:
            chunks = [{
                "index": 0,
                "start_ts": float(job["start_ts"]),
                "end_ts": float(job["end_ts"]),
                "train_kwargs": kwargs,
                "checkpoint_cursor_ts": float(job["end_ts"]),
                "checkpoint_meta": {},
            }]
        sequence_mode = bool(job.get("sequence_chunks"))
        total_value = 0
        chunk_reports = []
        from tiny_mlp_shadow import load_training_record, publish_training_artifact

        def write_rollback_snapshot(next_chunk_index):
            path = job.get("rollback_path")
            if not path:
                return
            _atomic_json(path, {
                "chunk_index": int(next_chunk_index),
                "agent_before": STORE.get_agent_config(aid),
                "model_before": STORE.get_model(aid),
                "neural_before": load_training_record(STORE, aid),
                "captured_at": now_ts(),
            })

        if sequence_mode:
            write_rollback_snapshot(0)
        for chunk_position, chunk in enumerate(chunks):
            if history.stop_event.is_set():
                raise InterruptedError("Persistent training worker cancelled")
            chunk_kwargs = dict(chunk.get("train_kwargs") or {})
            history.set_status(
                progress=chunk_kwargs.get("progress_lo"),
                message=(
                    f"Persistent training worker: chunk {chunk_position + 1}/"
                    f"{len(chunks)}"
                ),
                phase_detail=(
                    "Persistent worker keeps Python, NumPy and bounded replay caches "
                    "resident while preserving logical chunk boundaries"
                ),
            )
            chunk_started = time.monotonic()
            value = history.train_from_archive(
                float(chunk["start_ts"]),
                float(chunk["end_ts"]),
                agent_ids={aid},
                **chunk_kwargs,
            )
            total_value += int(value or 0)

            # The old process-per-chunk path publishes the supervised neural artifact
            # before the next child starts. Do the same here so the next logical chunk
            # sees exactly the same persisted TinyMLP parent.
            for neural_agent_id, artifact in dict(
                history.neural_training_artifacts or {}
            ).items():
                publish_training_artifact(STORE, artifact)

            checkpoint_cursor = float(
                chunk.get("checkpoint_cursor_ts", chunk["end_ts"])
            )
            if sequence_mode:
                STORE.set_training_progress(
                    aid,
                    float(job.get("sequence_start_ts", chunks[0]["start_ts"])),
                    checkpoint_cursor,
                    float(job.get("sequence_target_end_ts", chunks[-1]["end_ts"])),
                )
                checkpoint_meta = dict(chunk.get("checkpoint_meta") or {})
                STORE.event(
                    aid,
                    "info",
                    "agent_index_checkpoint",
                    str(checkpoint_meta.pop(
                        "message",
                        f"Historical indexing checkpoint {chunk_position + 1}/{len(chunks)}",
                    )),
                    checkpoint_meta,
                )
                # Immediately advance the rollback point. A kill/cancel between chunks
                # therefore preserves every completed checkpoint.
                write_rollback_snapshot(chunk_position + 1)

            chunk_reports.append({
                "index": int(chunk_position),
                "elapsed_seconds": round(time.monotonic() - chunk_started, 4),
                "return_value": int(value or 0),
                "temporal_replay": dict(history.temporal_replay_stats or {}),
                "training_replay_cache": dict(
                    history.training_replay_cache_status or {}
                ),
                "training_home_context_cache": dict(
                    history.training_home_context_cache_status or {}
                ),
                "training_ram_replay_index": dict(
                    getattr(history, "training_ram_replay_index_status", {}) or {}
                ),
                "training_transition_edge_index": dict(
                    getattr(history, "training_transition_edge_index_status", {}) or {}
                ),
                "training_feature_snapshot_cache": dict(
                    getattr(history, "training_feature_snapshot_cache_status", {}) or {}
                ),
                "training_phase_timings": dict(
                    getattr(history, "training_phase_timings", {}) or {}
                ),
            })
            if history.stop_event.is_set():
                raise InterruptedError("Persistent training worker cancelled")
            if chunk_position + 1 < len(chunks):
                pause_ms = max(
                    0.0,
                    float(OPTIONS.get("agent_training_pause_ms", 0) or 0),
                )
                if pause_ms and history.stop_event.wait(pause_ms / 1000.0):
                    raise InterruptedError("Persistent training worker cancelled")

        training_session_profile = aggregate_training_sequence_profile(
            chunk_reports
        )
        result.update({
            "ok": True,
            "return_value": int(total_value),
            "sequence_contract": (
                "persistent_agent_training_worker_v1" if sequence_mode else None
            ),
            "sequence_chunks_completed": len(chunk_reports),
            "sequence_chunk_reports": chunk_reports,
            "training_session_profile": training_session_profile,
            "context_relevance": dict(engine.context_relevance.get(aid) or {}),
            "temporal_replay": dict(history.temporal_replay_stats or {}),
            "training_long_memory": dict(
                getattr(history, "training_long_memory_status", {}) or {}
            ),
            "training_replay_cache": dict(history.training_replay_cache_status or {}),
            "training_home_context_cache": dict(
                history.training_home_context_cache_status or {}
            ),
            "training_ram_replay_index": dict(
                getattr(history, "training_ram_replay_index_status", {}) or {}
            ),
            "training_transition_edge_index": dict(
                getattr(history, "training_transition_edge_index_status", {}) or {}
            ),
            "training_feature_snapshot_cache": dict(
                getattr(history, "training_feature_snapshot_cache_status", {}) or {}
            ),
            "training_phase_timings": dict(
                getattr(history, "training_phase_timings", {}) or {}
            ),
            "schema_cache_item": dict(
                history.training_schema_cache.get(aid) or {}
            ),
            "neural_training_artifacts": dict(
                history.neural_training_artifacts or {}
            ),
            "training_budget": TRAINING_BUDGET.snapshot(),
            "resource_profile": dict(job.get("resource_profile") or {}),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        _atomic_json(job["result_path"], result)
        return 0
    except Exception as exc:
        result.update({
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
            "trace": traceback.format_exc(limit=12),
            "training_budget": TRAINING_BUDGET.snapshot(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        _atomic_json(job["result_path"], result)
        return 2
    finally:
        try:
            history.close_persistent_training_resources()
        except Exception:
            pass
        try:
            restore_training_contract()
        except Exception:
            pass
        budget.end()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    args = parser.parse_args()
    if not args.worker:
        parser.error("--worker is required")
    # Direct CLI/test invocation must use the same existing-database bootstrap contract
    # as subprocesses created by run_isolated_training_chunk().
    os.environ["ADAPTIVE_AI_TRAINING_WORKER"] = "1"
    raise SystemExit(worker_main(args.worker))


if __name__ == "__main__":
    main()
