"""Bound expensive historical learning on Raspberry-Pi class hosts.

Realtime inference/control is intentionally untouched. Explicit historical indexing runs
in a worker thread and crosses Store.archive_iter for both feature screening and replay.

0.14.17 added an average cooperative duty cycle. A real Pi run then exposed a gap: once
replay reached 100%, model finalization and expensive per-row tracker work could still hold
one core long enough to starve Ingress even though the average replay budget looked safe.

0.14.18 keeps the same model semantics but adds a wall-clock slice budget. Explicit
training workers must yield at cooperative checkpoints after at most a short work slice,
including inside temporal tracking and post-replay finalization.
"""
from __future__ import annotations

import threading
import time

from training_budget import TRAINING_BUDGET


CONTRACT_VERSION = 3
DEFAULT_ARCHIVE_BATCH_ROWS = 16
DEFAULT_TRAINING_DUTY_CYCLE = 0.25
DEFAULT_MAX_THROTTLE_SLEEP_SECONDS = 2.0
DEFAULT_MAX_CONTINUOUS_WORK_MS = 75
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
    # authoritative. 0.14.15/16 shipped 55%; 0.14.17 moved explicit training to 25%.
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
    core.OPTIONS.setdefault(
        "training_max_continuous_work_ms", DEFAULT_MAX_CONTINUOUS_WORK_MS
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
    max_slice_ms = _clamp(
        float(
            core.OPTIONS.get(
                "training_max_continuous_work_ms",
                DEFAULT_MAX_CONTINUOUS_WORK_MS,
            )
        ),
        25.0,
        500.0,
    )

    TRAINING_BUDGET.configure(
        duty_cycle=duty,
        max_slice_seconds=max_slice_ms / 1000.0,
        max_sleep_seconds=max_sleep,
        thread_prefixes=("adaptive-ai-index-",),
    )

    # Store.archive_iter is a common streaming boundary for historical screening and
    # replay. Check after every yielded row. The generator resumes only after downstream
    # processing of that row, so the checkpoint measures the real consumer cost rather
    # than merely SQLite fetch time. More granular checkpoints inside replay.py cover one
    # unusually expensive row before it can monopolize the process for long.
    store = core.STORE
    original_archive_iter = store.archive_iter

    def archive_iter_low_power(*args, **kwargs):
        iterator = original_archive_iter(*args, **kwargs)
        if not threading.current_thread().name.startswith("adaptive-ai-index-"):
            yield from iterator
            return

        for row in iterator:
            yield row
            TRAINING_BUDGET.checkpoint("archive_iter_row")
        TRAINING_BUDGET.checkpoint("archive_iter_end", force=True)

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
        budget = TRAINING_BUDGET.snapshot()
        return {
            "contract_version": CONTRACT_VERSION,
            "training_cpu_duty_cycle": duty,
            "archive_batch_rows": batch_rows,
            "max_throttle_sleep_seconds": max_sleep,
            "max_continuous_work_ms": max_slice_ms,
            "candidate_idle_poll_seconds": DEFAULT_CANDIDATE_IDLE_POLL_SECONDS,
            "maintenance_interval_seconds": MAINTENANCE_INTERVAL_SECONDS,
            **budget,
        }

    core.LOW_POWER_RUNTIME = snapshot
    core._rpi_low_power_runtime_installed = True
    core.STORE.event(
        None,
        "info",
        "rpi_low_power_runtime_ready",
        (
            "Historical training has a Pi-safe CPU duty cycle plus a "
            f"{max_slice_ms:.0f} ms continuous-work slice budget"
        ),
        snapshot(),
    )
    return manager
