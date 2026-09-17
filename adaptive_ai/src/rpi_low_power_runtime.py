"""Bound expensive historical learning on Raspberry-Pi class hosts.

Realtime inference/control is intentionally untouched. Explicit historical indexing runs
in a worker thread and crosses Store.archive_iter for both feature screening and replay.
This wrapper uses that generator boundary as a cooperative CPU budget: after a small batch
of rows it measures the *downstream* wall time spent by the consumer and sleeps long
enough to reserve CPU for Home Assistant, Ingress and the HTTP/UI threads.

0.14.17 tightens the contract after a real Pi 4 run showed that the previous 250 ms sleep
cap could defeat the nominal 55% duty cycle on expensive batches. The default is now 25%,
checkpoints are smaller, and compensating sleeps may extend to 2 seconds. This is a
cooperative application budget, not a Linux cgroup hard quota.
"""
from __future__ import annotations

import threading
import time


CONTRACT_VERSION = 2
DEFAULT_ARCHIVE_BATCH_ROWS = 16
DEFAULT_TRAINING_DUTY_CYCLE = 0.25
DEFAULT_MAX_THROTTLE_SLEEP_SECONDS = 2.0
DEFAULT_CANDIDATE_IDLE_POLL_SECONDS = 3.0
MAINTENANCE_INTERVAL_SECONDS = 60.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def _budget_pause(active_seconds, duty_cycle, max_sleep_seconds):
    """Return cooperative sleep needed for active/(active+sleep) ~= duty_cycle."""
    active = max(0.0, float(active_seconds))
    duty = _clamp(float(duty_cycle), 0.15, 0.70)
    max_sleep = _clamp(float(max_sleep_seconds), 0.25, 5.0)
    return _clamp(active * (1.0 - duty) / max(duty, 1e-6), 0.005, max_sleep)


def install(core, manager):
    if getattr(core, "_rpi_low_power_runtime_installed", False):
        return manager

    # Migrate only values that were shipped as defaults. Explicit user tuning remains
    # authoritative. 0.14.15/16 shipped 55%; 0.14.17 reserves substantially more CPU
    # for HA/Ingress while historical replay is active.
    if float(core.OPTIONS.get("agent_training_chunk_hours", 24) or 24) == 24.0:
        core.OPTIONS["agent_training_chunk_hours"] = 6
    if int(core.OPTIONS.get("history_background_pause_ms", 500) or 0) == 500:
        core.OPTIONS["history_background_pause_ms"] = 1500
    if float(core.OPTIONS.get("training_cpu_duty_cycle", 0.55) or 0.55) == 0.55:
        core.OPTIONS["training_cpu_duty_cycle"] = DEFAULT_TRAINING_DUTY_CYCLE
    core.OPTIONS.setdefault("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)
    core.OPTIONS.setdefault("training_archive_batch_rows", DEFAULT_ARCHIVE_BATCH_ROWS)
    core.OPTIONS.setdefault(
        "training_throttle_max_sleep_seconds", DEFAULT_MAX_THROTTLE_SLEEP_SECONDS
    )

    duty = _clamp(
        float(core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)),
        0.15,
        0.70,
    )
    batch_rows = int(
        _clamp(
            int(core.OPTIONS.get("training_archive_batch_rows", DEFAULT_ARCHIVE_BATCH_ROWS)),
            8,
            128,
        )
    )
    max_sleep = _clamp(
        float(
            core.OPTIONS.get(
                "training_throttle_max_sleep_seconds",
                DEFAULT_MAX_THROTTLE_SLEEP_SECONDS,
            )
        ),
        0.25,
        5.0,
    )
    diagnostics = {
        "contract_version": CONTRACT_VERSION,
        "training_cpu_duty_cycle": duty,
        "archive_batch_rows": batch_rows,
        "max_throttle_sleep_seconds": max_sleep,
        "throttle_batches": 0,
        "throttle_active_seconds": 0.0,
        "throttle_sleep_seconds": 0.0,
        "effective_training_duty_cycle": None,
        "candidate_idle_poll_seconds": DEFAULT_CANDIDATE_IDLE_POLL_SECONDS,
        "maintenance_interval_seconds": MAINTENANCE_INTERVAL_SECONDS,
    }
    diag_lock = threading.RLock()

    # Store.archive_iter is the common streaming boundary used by historical screening
    # and chronological replay. Execution resumes after each yield only once the consumer
    # requests the next row, so elapsed wall time includes policy/features/replay work.
    store = core.STORE
    original_archive_iter = store.archive_iter

    def archive_iter_low_power(*args, **kwargs):
        iterator = original_archive_iter(*args, **kwargs)
        if not threading.current_thread().name.startswith("adaptive-ai-index-"):
            yield from iterator
            return

        current_duty = _clamp(
            float(core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)),
            0.15,
            0.70,
        )
        current_batch = int(
            _clamp(
                int(core.OPTIONS.get("training_archive_batch_rows", DEFAULT_ARCHIVE_BATCH_ROWS)),
                8,
                128,
            )
        )
        current_max_sleep = _clamp(
            float(
                core.OPTIONS.get(
                    "training_throttle_max_sleep_seconds",
                    DEFAULT_MAX_THROTTLE_SLEEP_SECONDS,
                )
            ),
            0.25,
            5.0,
        )
        batch_started = time.perf_counter()
        rows = 0
        for row in iterator:
            yield row
            rows += 1
            if rows % current_batch:
                continue
            active_seconds = max(0.0, time.perf_counter() - batch_started)
            pause = _budget_pause(active_seconds, current_duty, current_max_sleep)
            time.sleep(pause)
            with diag_lock:
                diagnostics["training_cpu_duty_cycle"] = current_duty
                diagnostics["archive_batch_rows"] = current_batch
                diagnostics["max_throttle_sleep_seconds"] = current_max_sleep
                diagnostics["throttle_batches"] += 1
                diagnostics["throttle_active_seconds"] += active_seconds
                diagnostics["throttle_sleep_seconds"] += pause
                total = (
                    diagnostics["throttle_active_seconds"]
                    + diagnostics["throttle_sleep_seconds"]
                )
                diagnostics["effective_training_duty_cycle"] = (
                    diagnostics["throttle_active_seconds"] / total if total > 0 else None
                )
            batch_started = time.perf_counter()

    store.archive_iter = archive_iter_low_power

    # Candidate orchestration is event-driven. Polling SQLite every 500 ms while idle is
    # unnecessary on small hosts; enqueue/discard/promote paths explicitly wake it.
    manager.poll_seconds = max(
        float(getattr(manager, "poll_seconds", 0.5) or 0.5),
        DEFAULT_CANDIDATE_IDLE_POLL_SECONDS,
    )

    original_maintenance = manager._maintenance
    maintenance_state = {"next_at": 0.0}

    def bounded_maintenance():
        now = time.monotonic()
        if now < maintenance_state["next_at"]:
            return None
        maintenance_state["next_at"] = now + MAINTENANCE_INTERVAL_SECONDS
        return original_maintenance()

    manager._maintenance = bounded_maintenance

    def snapshot():
        with diag_lock:
            result = dict(diagnostics)
        result["throttle_active_seconds"] = round(
            float(result["throttle_active_seconds"]), 3
        )
        result["throttle_sleep_seconds"] = round(
            float(result["throttle_sleep_seconds"]), 3
        )
        effective = result.get("effective_training_duty_cycle")
        result["effective_training_duty_cycle"] = (
            None if effective is None else round(float(effective), 4)
        )
        return result

    core.LOW_POWER_RUNTIME = snapshot
    core._rpi_low_power_runtime_installed = True
    core.STORE.event(
        None,
        "info",
        "rpi_low_power_runtime_ready",
        "Historical training has a Pi-safe CPU budget and idle Candidate polling is reduced",
        snapshot(),
    )
    return manager
