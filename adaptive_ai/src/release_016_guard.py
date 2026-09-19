"""0.14.16 Raspberry-Pi startup and background-load guard.

This release changes scheduling and transport pressure only. It deliberately leaves
models, labels, generation state, Executor dispatch and persisted history semantics
unchanged.

The important boundary is that importing the shipped entrypoint must remain cheap. The
actual History/HA patches are therefore installed from ``core.initialize_runtime`` in the
background initializer, never while the HTTP entrypoint is being imported.
"""
from __future__ import annotations

import threading
import time
import traceback


CONTRACT_VERSION = 1
BACKGROUND_DUTY_CYCLE = 0.20
BACKGROUND_BATCH_ROWS = 128
BACKGROUND_MIN_GRACE_SECONDS = 60.0
RECORDER_BACKOFF_SECONDS = 120.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def install(runtime):
    core = runtime.core
    if getattr(core, "_release_016_guard_installed", False):
        return getattr(core, "RELEASE_016_RESOURCE_GUARD", None)

    state = {
        "contract_version": CONTRACT_VERSION,
        "quiet_start_completed": False,
        "runtime_patches_installed": False,
        "background_grace_seconds": max(
            BACKGROUND_MIN_GRACE_SECONDS,
            float(core.OPTIONS.get("history_background_start_delay_seconds", 10) or 0),
        ),
        "background_cpu_duty_cycle": BACKGROUND_DUTY_CYCLE,
        "background_archive_batch_rows": BACKGROUND_BATCH_ROWS,
        "background_throttle_batches": 0,
        "background_throttle_sleep_seconds": 0.0,
        "recorder_backoff_seconds": RECORDER_BACKOFF_SECONDS,
        "recorder_backoff_until_monotonic": 0.0,
        "recorder_timeout_count": 0,
        "automation_scan_workers": 1,
    }
    state_lock = threading.RLock()
    core.OPTIONS["history_background_start_delay_seconds"] = state["background_grace_seconds"]
    core.OPTIONS.setdefault("background_cpu_duty_cycle", BACKGROUND_DUTY_CYCLE)

    def snapshot():
        with state_lock:
            now = time.monotonic()
            return {
                **state,
                "background_throttle_sleep_seconds": round(
                    state["background_throttle_sleep_seconds"], 3
                ),
                "recorder_backoff_remaining_seconds": round(
                    max(0.0, state["recorder_backoff_until_monotonic"] - now), 1
                ),
            }

    # Diagnostics can be installed without importing runtime/database modules.
    previous_status_payload = core.Handler.status_payload

    def status_payload(handler_self):
        payload = previous_status_payload(handler_self)
        if isinstance(payload, dict):
            payload["resource_guard"] = snapshot()
        return payload

    core.Handler.status_payload = status_payload

    def install_runtime_patches():
        with state_lock:
            if state["runtime_patches_installed"]:
                return
            # Claim installation before patching so nested initialization cannot double-wrap.
            state["runtime_patches_installed"] = True

        import history as history_module
        import ha as ha_module
        from telemetry import HEAVY_JOBS

        store = history_module.STORE
        original_bootstrap = history_module.HistoryManager.bootstrap_and_train

        # ----- Local-only first History cycle -----------------------------
        def quiet_start(history_self):
            with history_self.engine.lock:
                current = dict(history_self.engine.state_map)
            if not current:
                return False

            controllable = [
                eid
                for eid, st in current.items()
                if history_module.target_options_for_state(st)
            ]
            history_self.discovered_controllable = len(controllable)
            existing = [a for a in store.list_agent_configs() if a.get("enabled")]
            history_self.discovered_active = len(
                [a for a in existing if a.get("target_entity") in current]
            )
            history_self.discovery_eligible = len(controllable)

            # Purely local diagnostics: this only reads the in-memory HA state/registry.
            try:
                history_self._eligible_rebuild_context()
            except Exception as exc:
                store.event(
                    None,
                    "warning",
                    "startup_context_diagnostics_partial",
                    str(exc),
                    None,
                )

            q = len(store.qualified_agents())
            waiting = len(
                [
                    a
                    for a in existing
                    if a.get("training_state")
                    in ("paused", "waiting", "needs_retrain")
                ]
            )
            history_self.last_run = history_module.now_ts()
            history_self.set_status(
                "ready",
                1.0,
                f"Fast startup ready - {q} trained / {waiting} waiting - Recorder idle",
                stage_eta_seconds=0,
                work_done=0,
                work_total=0,
                work_unit="startup I/O",
                eta_source="idle",
                phase_detail=(
                    "Saved agents + realtime only; no Recorder/API backfill during startup"
                ),
            )
            with state_lock:
                state["quiet_start_completed"] = True
            store.event(
                None,
                "info",
                "startup_io_quiet",
                (
                    "Startup completed from saved agents/current state without Recorder "
                    "or automation-config backfill"
                ),
                {
                    "existing_agents": len(existing),
                    "controllable_now": len(controllable),
                },
            )
            return True

        def quiet_run(history_self):
            # Operational-first runtime: saved agents + realtime are the normal steady
            # state. Recorder/discovery is intentionally *not* a periodic background task.
            # Heavy discovery runs only through the explicit Rescan API, which uses
            # HistoryManager.request_discovery_rescan().
            quiet_done = False
            while not history_self.stop_event.is_set():
                if not history_self.engine.state_map:
                    history_self.stop_event.wait(1.0)
                    continue
                if not quiet_done:
                    try:
                        quiet_done = quiet_start(history_self)
                        history_self.error = None
                    except Exception as exc:
                        history_self.error = f"{type(exc).__name__}: {exc}"
                        history_self.set_status("error", message=history_self.error)
                        store.event(
                            None,
                            "error",
                            "history_quiet_start_error",
                            history_self.error,
                            {"trace": traceback.format_exc(limit=6)},
                        )
                    if not quiet_done:
                        history_self.stop_event.wait(1.0)
                        continue
                # Realtime ingestion keeps the local archive current. Missing Recorder
                # backfill and auto-discovery are user-requested maintenance, never an
                # automatic cost paid by every restart.
                history_self.stop_event.wait(60.0)

        history_module.HistoryManager.run = quiet_run

        # ----- Background local-archive CPU budget ------------------------
        original_archive_iter = store.archive_iter

        def archive_iter_guard(*args, **kwargs):
            iterator = original_archive_iter(*args, **kwargs)
            if threading.current_thread().name != "adaptive-ai-history":
                yield from iterator
                return

            duty = _clamp(
                float(
                    core.OPTIONS.get(
                        "background_cpu_duty_cycle", BACKGROUND_DUTY_CYCLE
                    )
                ),
                0.10,
                0.60,
            )
            batch_started = time.perf_counter()
            rows = 0
            for row in iterator:
                yield row
                rows += 1
                if rows % BACKGROUND_BATCH_ROWS:
                    continue
                active = max(0.0, time.perf_counter() - batch_started)
                pause = _clamp(
                    active * (1.0 - duty) / max(duty, 1e-6), 0.005, 0.500
                )
                time.sleep(pause)
                with state_lock:
                    state["background_cpu_duty_cycle"] = duty
                    state["background_throttle_batches"] += 1
                    state["background_throttle_sleep_seconds"] += pause
                batch_started = time.perf_counter()

        store.archive_iter = archive_iter_guard

        # ----- Recorder overload circuit breaker --------------------------
        original_ha_history = ha_module.HA.history

        def guarded_ha_history(*args, **kwargs):
            try:
                return original_ha_history(*args, **kwargs)
            except Exception as exc:
                if HEAVY_JOBS.owner == "discovery":
                    now = time.monotonic()
                    with state_lock:
                        first = now >= state["recorder_backoff_until_monotonic"]
                        state["recorder_backoff_until_monotonic"] = (
                            now + RECORDER_BACKOFF_SECONDS
                        )
                        state["recorder_timeout_count"] += 1
                    if first:
                        store.event(
                            None,
                            "warning",
                            "history_background_backoff",
                            (
                                "Recorder background request failed; pausing discovery "
                                f"for {int(RECORDER_BACKOFF_SECONDS)} s"
                            ),
                            {
                                "error": f"{type(exc).__name__}: {exc}",
                                "backoff_seconds": RECORDER_BACKOFF_SECONDS,
                            },
                        )
                raise

        ha_module.HA.history = guarded_ha_history
        original_fetch = history_module.HistoryManager._fetch_history_resilient

        def fetch_with_circuit_breaker(history_self, *args, **kwargs):
            if HEAVY_JOBS.owner == "discovery":
                with state_lock:
                    if (
                        time.monotonic()
                        < state["recorder_backoff_until_monotonic"]
                    ):
                        return 0
            return original_fetch(history_self, *args, **kwargs)

        history_module.HistoryManager._fetch_history_resilient = fetch_with_circuit_breaker

        # ----- Automation API pressure ------------------------------------
        # ha.py uses this imported executor for config reads. Serialize those reads on
        # Raspberry-Pi class hardware rather than starting up to eight simultaneous calls.
        original_executor = ha_module.ThreadPoolExecutor

        def serial_executor(*args, **kwargs):
            if args:
                args = (1, *args[1:])
            else:
                kwargs["max_workers"] = 1
            return original_executor(*args, **kwargs)

        ha_module.ThreadPoolExecutor = serial_executor
        original_scan = ha_module.AUTOMATION_KNOWLEDGE.scan
        try:
            saved_scan = float(store.meta_get("automation_scan_last_ts", "0") or 0)
            if saved_scan > 0:
                ha_module.AUTOMATION_KNOWLEDGE.last_scan = saved_scan
        except (TypeError, ValueError):
            pass

        def persistent_scan(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            last = ha_module.AUTOMATION_KNOWLEDGE.status().get("last_scan")
            if last:
                store.meta_set("automation_scan_last_ts", str(last))
            return result

        ha_module.AUTOMATION_KNOWLEDGE.scan = persistent_scan

    # Defer all history/HA imports to the existing background initializer.
    # This keeps the proven HTTP-first startup contract from 0.14.14/0.14.15 intact.
    original_initialize_runtime = core.initialize_runtime

    def initialize_runtime():
        install_runtime_patches()
        return original_initialize_runtime()

    core.initialize_runtime = initialize_runtime
    core.RELEASE_016_RESOURCE_GUARD = snapshot
    core.release_016_resource_guard_contract = {
        "startup": "saved_agents_and_realtime_before_recorder",
        "first_background_pass": "automation_scan_suppressed",
        "background_archive_cpu": "20pct_default_duty_cycle",
        "recorder_timeout": "120s_circuit_breaker_no_recursive_burst",
        "automation_config_reads": "serialized_and_last_scan_persisted",
        "entrypoint_import": "no_runtime_or_database_imports",
        "physical_control": "unchanged",
    }
    core._release_016_guard_installed = True
    return snapshot
