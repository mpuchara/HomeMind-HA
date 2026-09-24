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

AGENT_CONFIG_KEYS = (
    "id", "enabled", "input_entities", "target_entity", "target_property",
    "min_value", "max_value", "confidence_threshold", "deadband",
    "action_interval", "exploration_step", "exploration_interval",
    "micro_exploration", "ack_timeout", "settling_seconds",
    "manual_hold_seconds",
)
STATE_METADATA_KEYS = (
    "device_class", "unit_of_measurement", "supported_features",
    "supported_color_modes", "options", "min", "max", "step",
)


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
        "registry": registry or {},
        # A changed option set is conservative invalidation. Training is rare and a
        # versioned job must never silently publish under a different configuration.
        "options": options or {},
        "training_revision": TRAINING_REVISION,
    })


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


def _proc_metrics(pid):
    result = {
        "rss_mb": None, "cpu_seconds": None,
        "read_bytes": None, "write_bytes": None,
    }
    try:
        status = Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                result["rss_mb"] = float(line.split()[1]) / 1024.0
                break
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
        state_map = dict(history.engine.state_map)
        registry = dict(history.engine.entity_registry)
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
        "state_map": state_map,
        "entity_registry": registry,
        "context_relevance": relevance,
        "automation_hints": list(hints or ()),
        "schema_cache_item": schema_item,
        "options": dict(OPTIONS),
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
        },
        "status_path": str(root / f"{job_id}.status.json"),
        "result_path": str(root / f"{job_id}.result.json"),
        "log_path": str(root / f"{job_id}.log"),
    }
    job["checksum"] = descriptor_checksum(job)
    job["job_path"] = str(root / f"{job_id}.job.json")
    # job_path is transport metadata and is intentionally outside the checksum contract.
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


def run_isolated_training_chunk(history, start_ts, end_ts, **kwargs):
    """Supervise one clean worker process while realtime remains in the parent."""
    from storage import STORE

    job = _build_job(history, start_ts, end_ts, kwargs)
    job_path = Path(job.pop("job_path"))
    _atomic_json(job_path, job)
    _prune_job_files()

    agent_id = job["agent_id"]
    agent_before = STORE.get_agent_config(agent_id)
    model_before = STORE.get_model(agent_id)
    poll_seconds = max(
        0.05, float(OPTIONS.get("training_worker_poll_ms", 200) or 200) / 1000.0
    )
    memory_limit = max(
        128.0, float(OPTIONS.get("training_worker_memory_limit_mb", 520) or 520)
    )
    grace = max(
        0.2, float(OPTIONS.get("training_worker_terminate_grace_seconds", 2.0) or 2.0)
    )
    env = dict(os.environ)
    env["ADAPTIVE_AI_DATA"] = str(DATA_DIR)
    env["PYTHONUNBUFFERED"] = "1"

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
    cancelled = False
    memory_exceeded = False
    history.training_process_status = {
        "enabled": True,
        "state": "running",
        "contract": "isolated_training_process_v1",
        "job_id": job["job_id"],
        "pid": process.pid,
        "agent_id": agent_id,
        "started_at": now_ts(),
        "worker_nice": int(OPTIONS.get("training_worker_nice", 10) or 10),
        "memory_limit_mb": memory_limit,
        "checkpointed_wal_reads": True,
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
            raise MemoryError(
                f"Isolated training worker exceeded {memory_limit:.0f} MB RSS"
            )
        if return_code != 0 or not isinstance(result, dict) or not result.get("ok"):
            detail = (result or {}).get("error") if isinstance(result, dict) else None
            error_type = (result or {}).get("error_type") if isinstance(result, dict) else None
            if error_type == "StaleTrainingJob":
                raise StaleTrainingJob(
                    detail or "isolated worker rejected stale training job",
                    preserve_lifecycle=True,
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
        history.training_replay_cache_status = dict(
            result.get("training_replay_cache") or {}
        )
        history.training_home_context_cache_status = dict(
            result.get("training_home_context_cache") or {}
        )
        schema_item = result.get("schema_cache_item")
        if isinstance(schema_item, dict) and schema_item:
            history.training_schema_cache[agent_id] = schema_item
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
        _restore_rejected_chunk(
            STORE, agent_id, agent_before, model_before,
            preserve_lifecycle=bool(
                isinstance(exc, StaleTrainingJob) and exc.preserve_lifecycle
            ),
        )
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

    # Descriptor options are the authoritative versioned job view.
    OPTIONS.clear()
    OPTIONS.update(dict(job.get("options") or {}))

    nice_value = int(OPTIONS.get("training_worker_nice", 10) or 10)
    nice_applied = False
    try:
        if nice_value > 0:
            os.nice(nice_value)
            nice_applied = True
    except (AttributeError, OSError):
        pass

    from history import HistoryManager
    from storage import STORE
    from training_budget import TRAINING_BUDGET

    STORE.checkpointed_archive_reads = True
    engine = TrainingWorkerEngine(job, STORE)
    history = HistoryManager(engine, worker_mode=True)
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
        value = history.train_from_archive(
            float(job["start_ts"]),
            float(job["end_ts"]),
            agent_ids={aid},
            **kwargs,
        )
        result.update({
            "ok": True,
            "return_value": int(value or 0),
            "context_relevance": dict(engine.context_relevance.get(aid) or {}),
            "temporal_replay": dict(history.temporal_replay_stats or {}),
            "training_replay_cache": dict(history.training_replay_cache_status or {}),
            "training_home_context_cache": dict(
                history.training_home_context_cache_status or {}
            ),
            "schema_cache_item": dict(
                history.training_schema_cache.get(aid) or {}
            ),
            "training_budget": TRAINING_BUDGET.snapshot(),
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
        budget.end()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    args = parser.parse_args()
    if not args.worker:
        parser.error("--worker is required")
    raise SystemExit(worker_main(args.worker))


if __name__ == "__main__":
    main()
