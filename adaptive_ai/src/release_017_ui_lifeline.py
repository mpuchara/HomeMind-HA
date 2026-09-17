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
    from telemetry import HEAVY_JOBS
    from training_queue import TrainingQueue

    cache_lock = threading.RLock()
    state = {
        "contract_version": CONTRACT_VERSION,
        "rich_agent_cache_at": 0.0,
        "rich_status_cache_at": 0.0,
        "agent_lifeline_reads": 0,
        "status_lifeline_reads": 0,
        "queue_label_reads": 0,
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
        agent = queue_self.store.get_agent_config(agent_id)
        with cache_lock:
            state["queue_label_reads"] += 1
        return (agent or {}).get("name") or agent_id

    TrainingQueue._agent_label = cheap_agent_label

    def snapshot():
        with cache_lock:
            result = dict(state)
        result["heavy"] = bool(heavy_active())
        result["heavy_job"] = HEAVY_JOBS.owner
        return result

    def hot_agent_payloads(handler_self):
        nonlocal rich_agents
        if not heavy_active():
            agents = original_agent_payloads(handler_self)
            with cache_lock:
                rich_agents = {a["id"]: dict(a) for a in agents}
                state["rich_agent_cache_at"] = time.time()
            return agents

        # Training lifeline: avoid rich history aggregates and runtime diagnostics.
        # diagnostics. Start from the last rich card and overlay only current cheap data.
        from context import target_value

        configs = core.STORE.list_agent_configs()
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
            agent["_ui_read_mode"] = "training_lifeline"
            out.append(agent)
        return out

    queue_runtime._agent_payloads = hot_agent_payloads

    def status_payload(handler_self):
        nonlocal rich_status
        if not core.runtime_available() or not heavy_active():
            payload = previous_status_payload(handler_self)
            if core.runtime_available():
                with cache_lock:
                    rich_status = dict(payload)
                    state["rich_status_cache_at"] = time.time()
            payload["status_read_mode"] = (
                "normal" if core.runtime_available() else "startup"
            )
            low_power = getattr(core, "LOW_POWER_RUNTIME", None)
            if callable(low_power):
                payload["low_power_runtime"] = low_power()
            payload["ui_lifeline"] = snapshot()
            return payload

        # Do not call Engine.status() here: it performs list_agents() aggregate history
        # queries. The lifeline intentionally exposes only cheap current state plus the
        # most recent rich snapshot until historical work yields the heavy slot.
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
        configs = core.STORE.list_agent_configs()
        queue = queue_object()
        startup = core.startup_snapshot()

        payload.update(
            {
                "version": core.APP_VERSION,
                "engine_error": engine_error,
                "last_poll": last_poll,
                "state_count": state_count,
                "agent_count": len(configs),
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
                "status_read_mode": "training_lifeline",
            }
        )
        payload.setdefault("ha_connected", bool(ws_connected))
        payload.setdefault("ha_error", ws_error)
        payload.setdefault("feedback_count", 0)
        payload.setdefault("historical_experience_count", 0)
        payload.setdefault("automation_knowledge", {})
        payload.setdefault("home_intelligence", {})
        payload.setdefault("home_bootstrap", {})
        payload.setdefault("telemetry", {})

        low_power = getattr(core, "LOW_POWER_RUNTIME", None)
        if callable(low_power):
            payload["low_power_runtime"] = low_power()
        release_016 = getattr(core, "RELEASE_016_RESOURCE_GUARD", None)
        if callable(release_016):
            payload["resource_guard"] = release_016()
        payload["ui_lifeline"] = snapshot()
        return payload

    core.Handler.status_payload = status_payload
    core.RELEASE_017_UI_LIFELINE = snapshot
    core.release_017_ui_lifeline_contract = {
        "status_during_training": "cached_rich_plus_hot_state_without_engine_status",
        "agents_during_training": "config_plus_cached_rich_without_history_aggregates",
        "queue_labels": "config_only",
        "mutations": "unchanged",
        "learning": "unchanged",
        "physical_control": "unchanged",
    }
    core._release_017_ui_lifeline_installed = True
    return snapshot
