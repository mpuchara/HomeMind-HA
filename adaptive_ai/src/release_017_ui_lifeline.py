"""0.14.17 UI/status lifeline while historical training is active.

Heavy replay must never make Ingress disappear. This release leaves learning, control,
models and persistence untouched; it only replaces expensive read-side summaries while a
shared heavy job is running. Rich cards/status return automatically when the heavy slot is
idle again.
"""
from __future__ import annotations

import threading
import time


CONTRACT_VERSION = 1


def install(runtime):
    core = runtime.core
    if getattr(core, "_release_017_ui_lifeline_installed", False):
        return getattr(core, "RELEASE_017_UI_LIFELINE", None)

    import queue_main as queue_runtime
    from telemetry import HEAVY_JOBS, TELEMETRY
    from training_queue import TrainingQueue

    cache_lock = threading.RLock()
    state = {
        "contract_version": CONTRACT_VERSION,
        "rich_agent_cache_at": 0.0,
        "rich_status_cache_at": 0.0,
        "agent_lifeline_reads": 0,
        "status_lifeline_reads": 0,
        "queue_label_reads": 0,
        "home_diag_cache_at": 0.0,
        "home_diag_refreshes": 0,
    }
    rich_agents = {}
    rich_status = {}

    original_agent_payloads = queue_runtime._agent_payloads
    previous_status_payload = core.Handler.status_payload

    def queue_object():
        return getattr(core, "TRAINING_QUEUE", None) or getattr(
            queue_runtime, "TRAINING_QUEUE", None
        )

    def heavy_active():
        if HEAVY_JOBS.owner is not None:
            return True
        queue = queue_object()
        if queue is None:
            return False
        cv = getattr(queue, "cv", None)
        if cv is None:
            return bool(getattr(queue, "active", None))
        with cv:
            return bool(getattr(queue, "active", None))

    # Queue status previously performed COUNT/AVG history scans merely to resolve every
    # queued agent's display name. The config-only lookup is sufficient and O(1)-ish.
    def cheap_agent_label(queue_self, agent_id):
        core.ENGINE._refresh_agent_index()
        with core.ENGINE.lock:
            agent = dict(getattr(core.ENGINE, "all_agent_configs", {}).get(str(agent_id)) or {})
        if not agent:
            agent = queue_self.store.get_agent_config(agent_id)
        with cache_lock:
            state["queue_label_reads"] += 1
        return (agent or {}).get("name") or agent_id

    TrainingQueue._agent_label = cheap_agent_label

    def hot_configs():
        # Revision-driven refresh performs no SQLite read while agent config is unchanged.
        # The complete config cache is intentionally distinct from the inference-routing
        # subset so PAUSED / WAITING / NEEDS_RETRAIN cards never disappear from the UI.
        core.ENGINE._refresh_agent_index()
        with core.ENGINE.lock:
            return [dict(row) for row in core.ENGINE.all_agent_configs.values()]

    home_cache = {"home_intelligence": {}, "home_bootstrap": {}}

    def hot_home_diagnostics():
        # Home Intelligence diagnostics are in-memory but not free: they summarize areas,
        # sources and adaptive-presence capability. Refresh them at most once per 5 s so
        # the panel stays truthful without competing with event -> intent on Raspberry Pi.
        now = time.monotonic()
        with cache_lock:
            cached_at = float(state.get("home_diag_cache_at") or 0.0)
            if cached_at > 0.0 and now - cached_at < 5.0:
                return (
                    dict(home_cache.get("home_intelligence") or {}),
                    dict(home_cache.get("home_bootstrap") or {}),
                )
        try:
            intelligence = core.ENGINE.context.diagnostics()
        except Exception as exc:
            intelligence = {"diagnostics_error": f"{type(exc).__name__}: {exc}"}
        bootstrap = (
            dict(core.ENGINE.home_bootstrap.status)
            if getattr(core.ENGINE, "home_bootstrap", None) is not None
            else {}
        )
        with cache_lock:
            home_cache["home_intelligence"] = dict(intelligence or {})
            home_cache["home_bootstrap"] = dict(bootstrap or {})
            state["home_diag_cache_at"] = now
            state["home_diag_refreshes"] = int(state.get("home_diag_refreshes") or 0) + 1
        return dict(intelligence or {}), dict(bootstrap or {})

    def snapshot():
        with cache_lock:
            result = dict(state)
        result["heavy"] = bool(heavy_active())
        result["heavy_job"] = HEAVY_JOBS.owner
        return result

    def hot_agent_payloads(handler_self):
        nonlocal rich_agents
        # Operational UI path is always lightweight. Rich COUNT/AVG history aggregates
        # are not allowed on the 4 s polling path, even when no heavy job is active.
        # Detailed historical diagnostics remain available through explicit endpoints.
        # Avoid rich history aggregates and runtime diagnostics.
        # diagnostics. Start from the last rich card and overlay only current cheap data.
        from context import target_value

        configs = hot_configs()
        with core.ENGINE.lock:
            states = dict(core.ENGINE.state_map)
            hot_runtime = {
                aid: dict(value) for aid, value in core.ENGINE.runtime.items()
            }
        with cache_lock:
            cached = {aid: dict(value) for aid, value in rich_agents.items()}
            state["agent_lifeline_reads"] += 1

        queue = queue_object()
        out = []
        runtime_keys = (
            "last_prediction",
            "last_confidence",
            "last_uncertainty",
            "last_expected_reward",
            "last_ai_ts",
            "last_reward",
            "last_reward_reason",
            "decision_state",
            "decision_reason",
            "last_service_ts",
            "last_service",
            "last_service_ok",
            "last_service_error",
            "last_service_latency_ms",
            "event_to_service_ms",
            "intent_to_service_ms",
            "context_meta",
            "teaching_id",
            "decision_source",
        )
        for config in configs:
            aid = config["id"]
            prior = cached.get(aid) or {}
            agent = dict(prior)
            agent.update(config)
            runtime_payload = dict(prior.get("runtime") or {})
            current_runtime = hot_runtime.get(aid) or {}
            for key in runtime_keys:
                if key in current_runtime:
                    runtime_payload[key] = current_runtime.get(key)
            target_state = states.get(config["target_entity"])
            runtime_payload["current_value"] = (
                target_value(target_state, config["target_property"])
                if target_state
                else None
            )
            runtime_payload["training_state"] = config.get("training_state") or "training"
            runtime_payload["benchmark_score"] = config.get("benchmark_score")
            runtime_payload["benchmark_samples"] = int(
                config.get("benchmark_samples") or 0
            )
            agent.setdefault("feedback_count", 0)
            agent.setdefault("positive_count", 0)
            agent.setdefault("negative_count", 0)
            agent.setdefault("historical_count", 0)
            agent.setdefault("control_qualification", {})
            agent.setdefault("control_review", {})
            agent.setdefault("control_lease", None)
            agent["training_queue"] = queue.status_for(aid) if queue else None
            agent["runtime"] = runtime_payload
            agent["_ui_read_mode"] = "operational_hot"
            out.append(agent)
        return out

    queue_runtime._agent_payloads = hot_agent_payloads

    def status_payload(handler_self):
        nonlocal rich_status
        startup = core.startup_snapshot()
        if not startup.get("ready") or not core.runtime_available():
            # Keep the earlier startup guard authoritative until Engine and its locks are
            # fully composed. Never touch a partial Engine from the hot operational path.
            payload = previous_status_payload(handler_self)
            payload["status_read_mode"] = "startup"
            payload["ui_lifeline"] = snapshot()
            return payload

        # Operational status must remain O(number of agents + in-memory runtime).
        # Engine.status()/STORE.list_agents() perform history aggregates and are never
        # called from the periodic UI path.
        with cache_lock:
            payload = dict(rich_status)
            state["status_lifeline_reads"] += 1
        with core.ENGINE.lock:
            confidences = [
                rt.get("last_confidence") for rt in core.ENGINE.runtime.values()
                if rt.get("last_confidence") is not None
            ]
            engine_error = core.ENGINE.error
            state_count = core.ENGINE.last_state_count
            last_poll = core.ENGINE.last_poll
            ws_connected = core.ENGINE.ws_connected
            ws_error = core.ENGINE.ws_error
            registry_count = len(core.ENGINE.entity_registry)
            last_ws_event = core.ENGINE.last_ws_event
            active_inference_agent_count = len(core.ENGINE.agent_configs)
        configs = hot_configs()
        home_intelligence, home_bootstrap = hot_home_diagnostics()
        queue = queue_object()
        startup = core.startup_snapshot()

        payload.update(
            {
                "version": core.APP_VERSION,
                "engine_error": engine_error,
                "last_poll": last_poll,
                "state_count": state_count,
                "agent_count": len(configs),
                "active_inference_agent_count": active_inference_agent_count,
                "average_confidence": (
                    sum(float(x) for x in confidences) / len(confidences)
                    if confidences
                    else 0.0
                ),
                "realtime": {
                    "connected": bool(ws_connected),
                    "error": ws_error,
                    "registry_entries": registry_count,
                    "last_event": last_ws_event,
                },
                "history": (
                    core.HISTORY.status()
                    if core.HISTORY is not None
                    else {"phase": "starting", "archive": {}}
                ),
                "heavy_job": HEAVY_JOBS.owner,
                "training_queue": (
                    queue.snapshot()
                    if queue
                    else {"active": None, "queued": [], "queued_count": 0}
                ),
                "startup": startup,
                "options": core.OPTIONS,
                "telemetry": TELEMETRY.snapshot(),
                "status_read_mode": "operational_hot",
            }
        )
        payload.setdefault("ha_connected", bool(ws_connected))
        payload.setdefault("ha_error", ws_error)
        payload.setdefault("feedback_count", 0)
        payload.setdefault("historical_experience_count", 0)
        payload.setdefault("automation_knowledge", {})
        payload["home_intelligence"] = home_intelligence
        payload["home_bootstrap"] = home_bootstrap

        low_power = getattr(core, "LOW_POWER_RUNTIME", None)
        if callable(low_power):
            payload["low_power_runtime"] = low_power()
        release_016 = getattr(core, "RELEASE_016_RESOURCE_GUARD", None)
        if callable(release_016):
            payload["resource_guard"] = release_016()
        feature_journal = getattr(core.ENGINE, "feature_observation_deferred_snapshot", None)
        if callable(feature_journal):
            payload["feature_journal"] = feature_journal()
        provenance_queue = getattr(core.ENGINE, "provenance_deferred_snapshot", None)
        if callable(provenance_queue):
            payload["provenance_queue"] = provenance_queue()
        with core.ENGINE.lock:
            archive_pending = len(getattr(core.ENGINE, "pending_archive", ()) or ())
        teaching = getattr(core.ENGINE, "teaching", None)
        teaching_lock = getattr(teaching, "lock", None)
        if teaching is not None and teaching_lock is not None:
            with teaching_lock:
                decision_history_pending = len(getattr(teaching, "buffer", ()) or ())
        else:
            decision_history_pending = 0
        with core.STORE.lock:
            diagnostic_events_pending = len(getattr(core.STORE, "_event_buffer", ()) or ())
            diagnostic_events_dropped = int(getattr(core.STORE, "_event_buffer_dropped", 0) or 0)
        tournament = getattr(core.ENGINE, "context_tournament", None)
        shadow_snapshot = getattr(tournament, "shadow_persistence_snapshot", None)
        fast_snapshot = getattr(tournament, "fast_light_persistence_snapshot", None)
        payload["ram_persistence_buffers"] = {
            "archive_pending": archive_pending,
            "decision_history_pending": decision_history_pending,
            "diagnostic_events_pending": diagnostic_events_pending,
            "diagnostic_events_dropped": diagnostic_events_dropped,
            "context_shadow": shadow_snapshot() if callable(shadow_snapshot) else {},
            "fast_light": fast_snapshot() if callable(fast_snapshot) else {},
        }
        payload["ui_lifeline"] = snapshot()
        return payload

    core.Handler.status_payload = status_payload
    core.RELEASE_017_UI_LIFELINE = snapshot
    core.release_017_ui_lifeline_contract = {
        "status_periodic": "always_hot_state_without_engine_status_or_history_aggregates",
        "agents_periodic": "config_plus_hot_runtime_without_history_aggregates",
        "queue_labels": "config_only",
        "mutations": "unchanged",
        "learning": "unchanged",
        "physical_control": "unchanged",
    }
    core._release_017_ui_lifeline_installed = True
    return snapshot
