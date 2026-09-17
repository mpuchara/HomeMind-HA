"""Bound background Adaptive AI work for Raspberry Pi class hosts.

The control/event path stays realtime.  Only historical archive scans performed by the
explicit per-agent indexing worker are duty-cycled.  The wrapper measures wall time spent
between batches of yielded archive rows, which includes the downstream feature/replay work,
then sleeps long enough to meet the configured duty cycle.  This avoids the old behaviour
where a Train/Candidate replay could consume a full CPU core for long periods.

Candidate orchestration is event-driven, so its idle database poll can also be relaxed
without adding user-visible action latency: enqueue/discard/promote paths set wake_event.
No model/evidence/data semantics are changed by this module.
"""
from __future__ import annotations

import threading
import time


CONTRACT_VERSION = 1
ARCHIVE_BATCH_ROWS = 256
DEFAULT_TRAINING_DUTY_CYCLE = 0.55
DEFAULT_CANDIDATE_IDLE_POLL_SECONDS = 3.0
MAINTENANCE_INTERVAL_SECONDS = 60.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def install(core, manager):
    if getattr(core, "_rpi_low_power_runtime_installed", False):
        return manager

    # New low-power defaults apply to the old shipped defaults only.  Explicit user
    # tuning remains authoritative when it differs from those legacy values.
    if float(core.OPTIONS.get("agent_training_chunk_hours", 24) or 24) == 24.0:
        core.OPTIONS["agent_training_chunk_hours"] = 6
    if int(core.OPTIONS.get("history_background_pause_ms", 500) or 0) == 500:
        core.OPTIONS["history_background_pause_ms"] = 1500
    core.OPTIONS.setdefault("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)

    diagnostics = {
        "contract_version": CONTRACT_VERSION,
        "training_cpu_duty_cycle": _clamp(
            float(core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)),
            0.20, 0.90,
        ),
        "archive_batch_rows": ARCHIVE_BATCH_ROWS,
        "throttle_batches": 0,
        "throttle_sleep_seconds": 0.0,
        "candidate_idle_poll_seconds": DEFAULT_CANDIDATE_IDLE_POLL_SECONDS,
        "maintenance_interval_seconds": MAINTENANCE_INTERVAL_SECONDS,
    }
    diag_lock = threading.RLock()

    # Store.archive_iter is the common streaming boundary used by historical screening
    # and chronological replay.  A generator wrapper lets us measure downstream work too:
    # execution resumes after each yield only once the consumer asks for the next row.
    store = core.STORE
    original_archive_iter = store.archive_iter

    def archive_iter_low_power(*args, **kwargs):
        iterator = original_archive_iter(*args, **kwargs)
        if not threading.current_thread().name.startswith("adaptive-ai-index-"):
            yield from iterator
            return

        duty = _clamp(
            float(core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)),
            0.20, 0.90,
        )
        batch_started = time.perf_counter()
        rows = 0
        for row in iterator:
            yield row
            rows += 1
            if rows % ARCHIVE_BATCH_ROWS:
                continue
            active_seconds = max(0.0, time.perf_counter() - batch_started)
            # duty = active/(active+sleep) -> sleep = active*(1-duty)/duty.
            pause = active_seconds * (1.0 - duty) / max(duty, 1e-6)
            # Keep yields observable even on very fast batches, but never make a single
            # sleep so long that cancellation/status feels unresponsive.
            pause = _clamp(pause, 0.003, 0.250)
            time.sleep(pause)
            with diag_lock:
                diagnostics["throttle_batches"] += 1
                diagnostics["throttle_sleep_seconds"] += pause
                diagnostics["training_cpu_duty_cycle"] = duty
            batch_started = time.perf_counter()

    store.archive_iter = archive_iter_low_power

    # Candidate work is awakened explicitly whenever its state changes.  Polling every
    # 500 ms while idle only burns SQLite/Python cycles on small hosts.
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
            return {
                **diagnostics,
                "throttle_sleep_seconds": round(float(diagnostics["throttle_sleep_seconds"]), 3),
            }

    core.LOW_POWER_RUNTIME = snapshot
    core._rpi_low_power_runtime_installed = True
    core.STORE.event(
        None,
        "info",
        "rpi_low_power_runtime_ready",
        "Historical training is duty-cycled and idle Candidate polling is reduced",
        snapshot(),
    )
    return manager
