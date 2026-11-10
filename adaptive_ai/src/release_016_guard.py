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
from urllib.error import HTTPError, URLError

def recorder_failure_diagnostics(exc):
    """Classify a failed HA Recorder request without recording a URL, token or entity ID.

    Nonretryable HTTP errors need an explicit operator intervention.  Local
    8 MiB response-limit failures and HTTP 413/414 are requests for the existing
    resilient history splitter, not evidence of HA overload.
    """
    error_type = type(exc).__name__
    status = None
    if isinstance(exc, HTTPError):
        status = int(exc.code)
        if status in (400, 401, 403, 404):
            category = "authentication" if status in (401, 403) else "unsupported_request"
            return {
                "category": category, "exception_type": error_type,
                "http_status": status, "retryable": False, "backoff": False,
                "action": "Check Home Assistant Recorder API, permissions and integration configuration; then run manual Rescan",
            }
        if status in (413, 414):
            return {
                "category": "request_too_large", "exception_type": error_type,
                "http_status": status, "retryable": True, "backoff": False,
                "action": "Retry using bounded entity/time slices",
            }
        return {
            "category": "ha_server_or_rate_limit", "exception_type": error_type,
            "http_status": status, "retryable": True, "backoff": True,
            "action": "Allow Home Assistant Recorder to recover before retry",
        }
    if isinstance(exc, OSError) and type(exc) is OSError and str(exc) == "HA response too large; split the request":
        return {
            "category": "response_too_large", "exception_type": error_type,
            "http_status": None, "retryable": True, "backoff": False,
            "action": "Retry using bounded entity/time slices",
        }
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        category = "timeout" if isinstance(reason, TimeoutError) else "network_error"
        errno = getattr(reason, "errno", None)
    elif isinstance(exc, TimeoutError):
        category = "timeout"
        errno = getattr(exc, "errno", None)
    elif isinstance(exc, OSError):
        category = "network_error"
        errno = getattr(exc, "errno", None)
    else:
        category = "unexpected_recorder_error"
        errno = None
    retryable = category != "unexpected_recorder_error"
    result = {
        "category": category, "exception_type": error_type,
        "http_status": None, "retryable": retryable, "backoff": retryable,
        "action": ("Check HA connectivity and Recorder performance"
                   if retryable else "Inspect add-on logs and fix the Recorder response before manual Rescan"),
    }
    if isinstance(errno, int):
        result["errno"] = errno
    return result



CONTRACT_VERSION = 1
BACKGROUND_DUTY_CYCLE = 0.20
BACKGROUND_BATCH_ROWS = 128
BACKGROUND_MIN_GRACE_SECONDS = 60.0
RECORDER_BACKOFF_SECONDS = 120.0
INITIAL_DISCOVERY_META_KEY = "initial_discovery_complete"
DEEP_DISCOVERY_META_KEY = "discovery_deep_history_complete"


def _initial_discovery_needed(store):
    """True only before the first completed classification on an empty install."""
    if store.meta_get(INITIAL_DISCOVERY_META_KEY, ""):
        return False
    # An interrupted initial discovery may have created some agents before the
    # final marker. Its persisted Recorder coverage proves this is unfinished,
    # NOT a legacy upgrade to mark complete. Resume only the uncovered chunks.
    if store.meta_get("discovery_recorder_coverage_v1"):
        return True
    # Legacy existing-agent installs without progress metadata keep their
    # original quiet-start contract.
    return not bool(store.list_agent_configs())


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
        "recorder_failure_count": 0,
        "recorder_last_failure": None,
        "recorder_terminal_error": None,
        "automation_scan_workers": 1,
        "initial_discovery_scheduled": False,
        "initial_discovery_completed": False,
        "deep_discovery_reconciliation_scheduled": False,
        "deep_discovery_reconciliation_completed": False,
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
            all_existing = store.list_agent_configs()
            existing = [a for a in all_existing if a.get("enabled")]
            # Upgrades that already contain agent configuration are not fresh installs.
            # Persist the boundary once so future restarts do not need to infer it.
            initial_marker = store.meta_get(INITIAL_DISCOVERY_META_KEY, "")
            if (not initial_marker and all_existing
                    and not store.meta_get("discovery_recorder_coverage_v1")):
                # Old upgrades are considered classified, but an interrupted
                # first scan with persisted partial coverage must resume instead.
                initial_marker = history_module.iso_from_ts(history_module.now_ts())
                store.meta_set(INITIAL_DISCOVERY_META_KEY, initial_marker)
            with state_lock:
                state["initial_discovery_completed"] = bool(initial_marker)
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
                    "initial_discovery_completed": bool(initial_marker),
                },
            )
            return True

        def quiet_run(history_self):
            # Operational-first runtime remains the steady state.  The sole exception is
            # a genuinely empty installation: once HTTP/realtime startup is ready, launch
            # one bounded discovery pass through the same async/single-flight path as the
            # Rescan button.  A durable completion marker keeps all later restarts quiet.
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

                marker = store.meta_get(INITIAL_DISCOVERY_META_KEY, "")
                deep_marker = store.meta_get(DEEP_DISCOVERY_META_KEY, "")
                with state_lock:
                    state["initial_discovery_completed"] = bool(marker)
                    state["deep_discovery_reconciliation_completed"] = bool(deep_marker)
                initial_needed = not marker and _initial_discovery_needed(store)
                # 0.14.41 and older low-memory installs can already have agents and an
                # initial-discovery marker while their local archive only covers the last
                # 24 h of the 10-day classifier window. Reconcile that mismatch exactly
                # once after runtime readiness; the discovery cycle persists deep_marker.
                deep_reconcile_needed = bool(marker) and not bool(deep_marker)
                discovery_needed = initial_needed or deep_reconcile_needed
                if discovery_needed and not core.startup_snapshot().get("ready"):
                    # History starts during runtime construction, just before the public
                    # ready flag flips. Do not turn that tiny ordering gap into a 60 s
                    # apparent cold-start stall.
                    history_self.stop_event.wait(0.25)
                    continue
                with state_lock:
                    recorder_terminal = bool(state["recorder_terminal_error"])
                if discovery_needed and not recorder_terminal and not history_self.discovery_job_active:
                    request = getattr(history_self, "request_discovery_rescan", None)
                    reason = "fresh_install" if initial_needed else "deep_history_reconcile"
                    if callable(request) and request(
                        threshold_override=1, reason=reason
                    ):
                        with state_lock:
                            if initial_needed:
                                state["initial_discovery_scheduled"] = True
                            else:
                                state["deep_discovery_reconciliation_scheduled"] = True
                        store.event(
                            None,
                            "info",
                            "initial_discovery_started" if initial_needed else "deep_discovery_reconciliation_started",
                            (
                                "Fresh install: started one-time controllable-device discovery"
                                if initial_needed
                                else "Upgrade: reconciling the full controllable-device discovery window once"
                            ),
                            {"threshold_override": 1, "reason": reason},
                        )

                # Realtime ingestion keeps the local archive current. There is no
                # periodic Recorder maintenance here. A failed one-time discovery or
                # deep-window reconciliation may retry after the bounded delay until its
                # durable marker is written; later restarts remain quiet.
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
                    failure = recorder_failure_diagnostics(exc)
                    # Request shape is safe to export: no raw entity IDs, dates,
                    # URLs, access tokens or returned state attributes.
                    ids = args[0] if args else kwargs.get("entity_ids", ())
                    start = args[1] if len(args) > 1 else kwargs.get("start_dt")
                    end = args[2] if len(args) > 2 else kwargs.get("end_dt")
                    try:
                        window_s = max(0, int(
                            history_module.parse_ts(end) - history_module.parse_ts(start)
                        ))
                    except (TypeError, ValueError, OverflowError):
                        window_s = None
                    failure["request_shape"] = {
                        "entity_count": len(ids or ()),
                        "window_seconds": window_s,
                        "minimal": bool(kwargs.get("minimal", True)),
                        "no_attributes": bool(kwargs.get("no_attributes", True)),
                        "significant": bool(kwargs.get("significant", True)),
                    }
                    now = time.monotonic()
                    with state_lock:
                        state["recorder_failure_count"] += 1
                        state["recorder_last_failure"] = dict(failure)
                        if not failure["retryable"]:
                            first = state["recorder_terminal_error"] != failure
                            state["recorder_terminal_error"] = dict(failure)
                            state["recorder_backoff_until_monotonic"] = 0.0
                        elif failure["backoff"]:
                            first = now >= state["recorder_backoff_until_monotonic"]
                            # Do not extend a pending cooldown on each recursive
                            # split failure; preserve the existing 120 s ceiling.
                            if first:
                                state["recorder_backoff_until_monotonic"] = (
                                    now + RECORDER_BACKOFF_SECONDS
                                )
                            state["recorder_timeout_count"] += 1
                        else:
                            first = False  # Existing splitter retries immediately.
                    if not failure["retryable"] and first:
                        store.event(
                            None, "error", "history_background_unavailable",
                            "Recorder request rejected; automatic discovery suspended until manual Rescan",
                            dict(failure),
                        )
                    elif failure["backoff"] and first:
                        store.event(
                            None, "warning", "history_background_backoff",
                            (
                                "Recorder background request failed "
                                f"({failure['category']}"
                                + (f", HTTP {failure['http_status']}" if failure["http_status"] else "")
                                + f"); pausing discovery for {int(RECORDER_BACKOFF_SECONDS)} s"
                            ),
                            dict(failure, backoff_seconds=RECORDER_BACKOFF_SECONDS),
                        )
                raise

        ha_module.HA.history = guarded_ha_history
        original_fetch = history_module.HistoryManager._fetch_history_resilient

        def fetch_with_circuit_breaker(history_self, *args, **kwargs):
            if HEAVY_JOBS.owner == "discovery":
                # Backoff must throttle Recorder, not silently drop discovery coverage.
                # Returning 0 here made the caller count a skipped chunk as completed,
                # so one timeout could erase roughly two minutes of target history from
                # classification. Wait cooperatively, then retry the same resilient
                # request/split path. Realtime HA processing stays independent.
                while not history_self.stop_event.is_set():
                    with state_lock:
                        remaining = (
                            state["recorder_backoff_until_monotonic"]
                            - time.monotonic()
                        )
                    if remaining <= 0:
                        break
                    cancel = getattr(history_self, "job_cancel_event", None)
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("History discovery cancelled during Recorder backoff")
                    history_self.set_status(
                        message=f"Recorder cooling down · retry in {max(1, int(remaining))} s",
                        phase_detail="Discovery coverage is paused, not skipped",
                        eta_source="Recorder circuit breaker",
                    )
                    history_self.stop_event.wait(min(1.0, max(0.05, remaining)))
                if history_self.stop_event.is_set():
                    raise InterruptedError("History discovery stopped during Recorder backoff")
            return original_fetch(history_self, *args, **kwargs)

        history_module.HistoryManager._fetch_history_resilient = fetch_with_circuit_breaker

        # A manual Rescan is an explicit operator retry after a permanent API
        # response (e.g. 401). Keep the automatic job suspended until then.
        original_request_discovery = history_module.HistoryManager.request_discovery_rescan

        def request_discovery_with_recovery(history_self, *args, **kwargs):
            if kwargs.get("reason") != "manual":
                return original_request_discovery(history_self, *args, **kwargs)
            with state_lock:
                prior = state["recorder_terminal_error"]
                state["recorder_terminal_error"] = None
            try:
                accepted = original_request_discovery(history_self, *args, **kwargs)
            except BaseException:
                with state_lock:
                    state["recorder_terminal_error"] = prior
                raise
            if not accepted:
                with state_lock:
                    state["recorder_terminal_error"] = prior
            return accepted

        history_module.HistoryManager.request_discovery_rescan = request_discovery_with_recovery

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
        "startup": "saved_agents_and_realtime_then_fresh_install_discovery",
        "automatic_background_discovery": "fresh_install_once_plus_one_time_deep_reconciliation_then_explicit_rescan",
        "background_archive_cpu": "20pct_default_duty_cycle_when_explicit",
        "recorder_timeout": "120s_circuit_breaker_no_recursive_burst",
        "recorder_diagnostics": "sanitized_request_shape_and_bounded_5xx_split_v2",
        "automation_config_reads": "serialized_and_last_scan_persisted",
        "entrypoint_import": "no_runtime_or_database_imports",
        "physical_control": "unchanged",
    }
    core._release_016_guard_installed = True
    return snapshot
