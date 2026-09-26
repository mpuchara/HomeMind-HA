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


CONTRACT_VERSION = 7
DEFAULT_ARCHIVE_BATCH_ROWS = 16
DEFAULT_EXPERIENCE_BATCH_ROWS = 128
DEFAULT_TRAINING_DUTY_CYCLE = 0.85
DEFAULT_MAX_THROTTLE_SLEEP_SECONDS = 0.50
DEFAULT_MAX_CONTINUOUS_WORK_MS = 35
DEFAULT_REALTIME_MAX_BURST_SECONDS = 0.45
DEFAULT_REALTIME_COOLDOWN_SECONDS = 0.20
DEFAULT_CANDIDATE_IDLE_POLL_SECONDS = 3.0
MAINTENANCE_INTERVAL_SECONDS = 60.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def _budget_pause(active_seconds, duty_cycle, max_sleep_seconds):
    """Return cooperative sleep needed for active/(active+sleep) ~= duty_cycle."""
    active = max(0.0, float(active_seconds))
    duty = _clamp(float(duty_cycle), 0.15, 0.90)
    max_sleep = _clamp(float(max_sleep_seconds), 0.25, 5.0)
    return _clamp(active * (1.0 - duty) / max(duty, 1e-6), 0.005, max_sleep)


def install(core, manager):
    if getattr(core, "_rpi_low_power_runtime_installed", False):
        return manager

    # Migrate only values that were shipped as defaults. Explicit user tuning remains
    # authoritative. 0.14.15/16 shipped 55%; 0.14.17-0.14.25 shipped 25% / 75 ms.
    if float(core.OPTIONS.get("agent_training_chunk_hours", 24) or 24) == 24.0:
        core.OPTIONS["agent_training_chunk_hours"] = 6
    if int(core.OPTIONS.get("history_background_pause_ms", 500) or 0) == 500:
        core.OPTIONS["history_background_pause_ms"] = 1500
    current_duty = float(
        core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)
        or DEFAULT_TRAINING_DUTY_CYCLE
    )
    if current_duty in (0.20, 0.25, 0.55, 0.65):
        core.OPTIONS["training_cpu_duty_cycle"] = DEFAULT_TRAINING_DUTY_CYCLE
    core.OPTIONS.setdefault("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)
    core.OPTIONS.setdefault("training_archive_batch_rows", DEFAULT_ARCHIVE_BATCH_ROWS)
    if int(core.OPTIONS.get("training_experience_batch_rows", DEFAULT_EXPERIENCE_BATCH_ROWS) or DEFAULT_EXPERIENCE_BATCH_ROWS) == 64:
        core.OPTIONS["training_experience_batch_rows"] = DEFAULT_EXPERIENCE_BATCH_ROWS
    core.OPTIONS.setdefault("training_experience_batch_rows", DEFAULT_EXPERIENCE_BATCH_ROWS)

    # 0.14.96 RAM-first migration. Only values that were shipped defaults are raised;
    # explicit lower user limits remain authoritative.
    if int(core.OPTIONS.get("training_replay_ram_cache_rows", 65536) or 0) == 16384:
        core.OPTIONS["training_replay_ram_cache_rows"] = 65536
    if int(core.OPTIONS.get("training_replay_ram_cache_entry_rows", 2048) or 0) == 1024:
        core.OPTIONS["training_replay_ram_cache_entry_rows"] = 2048
    if int(core.OPTIONS.get("training_home_context_cache_entries", 64) or 0) == 32:
        core.OPTIONS["training_home_context_cache_entries"] = 64
    if int(core.OPTIONS.get("training_home_context_cache_units", 32768) or 0) == 8192:
        core.OPTIONS["training_home_context_cache_units"] = 32768
    if int(core.OPTIONS.get("training_worker_memory_limit_mb", 1024) or 0) == 520:
        core.OPTIONS["training_worker_memory_limit_mb"] = 1024
    core.OPTIONS.setdefault("training_sqlite_cache_mb", 32)
    core.OPTIONS.setdefault("training_worker_memory_floor_mb", 256)
    core.OPTIONS.setdefault("training_worker_memory_total_fraction", 0.30)
    core.OPTIONS.setdefault("training_worker_memory_available_fraction", 0.50)
    core.OPTIONS.setdefault("training_worker_memory_reserve_mb", 512)
    core.OPTIONS.setdefault("training_worker_memory_unknown_fallback_mb", 520)

    core.OPTIONS.setdefault(
        "training_throttle_max_sleep_seconds", DEFAULT_MAX_THROTTLE_SLEEP_SECONDS
    )
    if float(core.OPTIONS.get("training_max_continuous_work_ms", 75) or 75) in (75.0, 50.0):
        core.OPTIONS["training_max_continuous_work_ms"] = DEFAULT_MAX_CONTINUOUS_WORK_MS
    core.OPTIONS.setdefault(
        "training_max_continuous_work_ms", DEFAULT_MAX_CONTINUOUS_WORK_MS
    )
    core.OPTIONS.setdefault(
        "training_realtime_max_burst_seconds", DEFAULT_REALTIME_MAX_BURST_SECONDS
    )
    core.OPTIONS.setdefault(
        "training_realtime_cooldown_seconds", DEFAULT_REALTIME_COOLDOWN_SECONDS
    )

    duty = _clamp(
        float(core.OPTIONS.get("training_cpu_duty_cycle", DEFAULT_TRAINING_DUTY_CYCLE)),
        0.15,
        0.90,
    )
    batch_rows = int(
        _clamp(
            int(core.OPTIONS.get("training_archive_batch_rows", DEFAULT_ARCHIVE_BATCH_ROWS)),
            8,
            128,
        )
    )
    experience_batch_rows = int(
        _clamp(
            int(core.OPTIONS.get("training_experience_batch_rows", DEFAULT_EXPERIENCE_BATCH_ROWS)),
            8,
            512,
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
    realtime_max_burst = _clamp(
        float(core.OPTIONS.get("training_realtime_max_burst_seconds", DEFAULT_REALTIME_MAX_BURST_SECONDS)),
        0.15,
        1.25,
    )
    realtime_cooldown = _clamp(
        float(core.OPTIONS.get("training_realtime_cooldown_seconds", DEFAULT_REALTIME_COOLDOWN_SECONDS)),
        0.05,
        1.0,
    )

    TRAINING_BUDGET.configure(
        duty_cycle=duty,
        max_slice_seconds=max_slice_ms / 1000.0,
        max_sleep_seconds=max_sleep,
        thread_prefixes=("adaptive-ai-index-",),
        clock=lambda: time.perf_counter(),
        sleeper=lambda seconds: time.sleep(seconds),
        realtime_max_burst_seconds=realtime_max_burst,
        realtime_cooldown_seconds=realtime_cooldown,
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

        rows = 0
        for row in iterator:
            yield row
            rows += 1
            # A cheap-row batch keeps the historical 0.14.17 average-duty behavior,
            # while per-row checkpoints still catch an expensive row as soon as it
            # returns from downstream processing.
            if rows % batch_rows == 0:
                TRAINING_BUDGET.checkpoint("archive_iter_batch", force=True)
            else:
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
            # Compatibility key: this setting name is already persisted in options.
            # Semantically it is a cooperative wall-clock work/sleep target, not measured
            # Linux process CPU utilisation.
            "training_cpu_duty_cycle": duty,
            "training_wall_duty_cycle_target": duty,
            "budget_semantics": "cooperative_wall_clock_not_process_cpu",
            "archive_batch_rows": batch_rows,
            "experience_batch_rows": experience_batch_rows,
            "max_throttle_sleep_seconds": max_sleep,
            "max_continuous_work_ms": max_slice_ms,
            "realtime_max_burst_seconds": realtime_max_burst,
            "realtime_cooldown_seconds": realtime_cooldown,
            "candidate_idle_poll_seconds": DEFAULT_CANDIDATE_IDLE_POLL_SECONDS,
            "maintenance_interval_seconds": MAINTENANCE_INTERVAL_SECONDS,
            "training_memory_strategy": "adaptive_ram_first_worker_profile_v1",
            "training_memory_ceiling_mb": int(
                core.OPTIONS.get("training_worker_memory_limit_mb", 1024) or 1024
            ),
            "training_replay_ram_cache_rows": int(
                core.OPTIONS.get("training_replay_ram_cache_rows", 65536) or 0
            ),
            "training_home_context_cache_entries": int(
                core.OPTIONS.get("training_home_context_cache_entries", 64) or 0
            ),
            "training_sqlite_cache_mb": int(
                core.OPTIONS.get("training_sqlite_cache_mb", 32) or 32
            ),
            **budget,
        }

    core.LOW_POWER_RUNTIME = snapshot
    core._rpi_low_power_runtime_installed = True
    core.STORE.event(
        None,
        "info",
        "rpi_low_power_runtime_ready",
        (
            "Historical training has a Pi-safe cooperative wall-clock duty target plus a "
            f"{max_slice_ms:.0f} ms continuous-work slice budget, bounded realtime preemption and batched replay persistence"
        ),
        snapshot(),
    )
    return manager
