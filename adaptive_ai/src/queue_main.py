"""Runtime wrapper that adds prioritized training admission without bloating main.py.

`main.py` remains the core HTTP/runtime implementation.  This module installs the
training queue hooks before calling it, so a busy low-memory training slot becomes
"queued" instead of an HTTP 409.
"""
import traceback
from urllib.parse import parse_qs, urlsplit

import main as core
from training_queue import TrainingQueue
from training_request_semantics import train_request_decision


TRAINING_QUEUE = None


_original_initialize_runtime = core.initialize_runtime
_original_shutdown_runtime = core.shutdown_runtime
_original_status_payload = core.Handler.status_payload
_original_do_post = core.Handler.do_POST
_original_do_patch = core.Handler.do_PATCH
_original_do_delete = core.Handler.do_DELETE


def initialize_runtime():
    global TRAINING_QUEUE
    _original_initialize_runtime()
    if not core.runtime_available() or core.HISTORY is None:
        return
    TRAINING_QUEUE = TrainingQueue(core.HISTORY, core.STORE, core.ENGINE)
    core.TRAINING_QUEUE = TRAINING_QUEUE
    TRAINING_QUEUE.start()

    # Runtime HTTP dispatch is created inside the base initialize path before the queue
    # becomes authoritative. Register queue-owned GET routes only after TRAINING_QUEUE
    # exists, preserving the historical early-start fallback behavior.
    registry = getattr(core, "EXPLICIT_HTTP_ROUTES", None)
    if registry is not None:
        register_read_routes(registry)

    core.STORE.event(None, "info", "training_queue_ready",
                     "Priority training queue ready; heavy jobs will run one at a time", None)


def status_payload(self):
    payload = _original_status_payload(self)
    metrics = getattr(core.ENGINE, "inference_hot_path_metrics", None) if core.ENGINE is not None else None
    payload["inference_hot_path"] = metrics.snapshot() if metrics is not None else None
    candidate_manager = getattr(core.ENGINE, "agent_candidates", None) if core.ENGINE is not None else None
    candidate_diag = getattr(candidate_manager, "candidate_shadow_async_diagnostics", None)
    payload["candidate_shadow_async"] = candidate_diag() if callable(candidate_diag) else None
    payload["training_queue"] = (TRAINING_QUEUE.snapshot() if TRAINING_QUEUE else
                                 {"active": None, "queued": [], "queued_count": 0})
    return payload


def _agent_payloads(self):
    agents = core.STORE.list_agents()
    for agent in agents:
        runtime = core.ENGINE.runtime_for(agent)
        internal = core.ENGINE.runtime.get(agent["id"]) or {}
        runtime["event_to_service_ms"] = internal.get("event_to_service_ms")
        runtime["intent_to_service_ms"] = internal.get("intent_to_service_ms")
        ack = runtime.get("ack_latency_seconds")
        runtime["service_to_ack_ms"] = None if ack is None else float(ack) * 1000.0
        if runtime.get("event_to_service_ms") is not None and runtime.get("service_to_ack_ms") is not None:
            runtime["event_to_ack_ms"] = float(runtime["event_to_service_ms"]) + float(runtime["service_to_ack_ms"])
        else:
            runtime["event_to_ack_ms"] = None
        with core.ENGINE.lock:
            target_state = core.ENGINE.state_map.get(agent["target_entity"])
        agent["control_qualification"] = core.assess_control_qualification(agent)
        agent["control_review"] = core.review_status(core.STORE, agent, target_state)
        agent["control_lease"] = core.ENGINE.executor.handoff.journal.get(agent["target_entity"])
        agent["training_queue"] = TRAINING_QUEUE.status_for(agent["id"]) if TRAINING_QUEUE else None
        agent["runtime"] = runtime
    return agents


def _rl_teaching():
    service = getattr(core.ENGINE, "rl_teaching", None) if core.ENGINE is not None else None
    if service is None:
        raise RuntimeError("Teach RL service is not ready")
    return service


def register_read_routes(registry):
    """Move queue-owned GET/static endpoints out of the Handler wrapper chain."""

    def queue_js(http, _params):
        return http.static("queue.js", "application/javascript; charset=utf-8")

    def teach_rl(http, params, action):
        agent_id = params["agent_id"]
        agent = core.STORE.get_agent_config(agent_id)
        if not agent:
            return http.send_json(404, {"error": "agent not found"})
        try:
            service = _rl_teaching()
            query = parse_qs(urlsplit(http.path).query)
            if action == "status":
                result = service.status(agent_id)
                result["training_queue"] = (
                    TRAINING_QUEUE.status_for(agent_id) if TRAINING_QUEUE else None
                )
                if core.HISTORY is not None:
                    result["history"] = core.HISTORY.status()
                return http.send_json(200, result)
            if action == "point":
                return http.send_json(
                    200, service.point(agent, (query.get("ts") or [None])[0])
                )
            return http.send_json(
                200,
                service.history(
                    agent,
                    (query.get("start") or [None])[0],
                    (query.get("end") or [None])[0],
                ),
            )
        except (ValueError, TypeError) as exc:
            return http.send_json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return http.send_json(500, {"error": str(exc)})

    def agents(http, _params):
        try:
            return http.send_json(200, _agent_payloads(http))
        except Exception as exc:
            traceback.print_exc()
            return http.send_json(500, {"error": str(exc)})

    registry.register(
        "GET",
        "queue.static",
        r"^/queue\.js$",
        queue_js,
        require_trusted=True,
        require_runtime=False,
        priority=180,
    )
    for action in ("history", "point", "status"):
        registry.register(
            "GET",
            f"queue.teach_rl.{action}",
            rf"^/api/agents/(?P<agent_id>[^/]+)/teach-rl-{action}$",
            lambda http, params, action=action: teach_rl(http, params, action),
            require_trusted=True,
            require_runtime=True,
            priority=180,
        )
    registry.register(
        "GET",
        "queue.agents",
        r"^/api/agents$",
        agents,
        require_trusted=True,
        require_runtime=True,
        priority=180,
    )
    return registry

def _queue_agent(self, agent_id, *, rebuild, reason, resumed=False,
                 rebuild_reason=None, learning_path=None):
    if TRAINING_QUEUE is None:
        return self.send_json(409, {"error": "Training queue is not ready"})
    agent = core.STORE.get_agent(agent_id)
    if not agent or core.HISTORY is None:
        return self.send_json(404, {"error": "agent/history engine not found"})
    try:
        queued = TRAINING_QUEUE.enqueue(
            agent_id, rebuild=rebuild, reason=reason,
            rebuild_reason=rebuild_reason,
        )
    except ValueError as exc:
        return self.send_json(404, {"error": str(exc)})
    except Exception as exc:
        return self.send_json(502, {"error": f"Could not queue training safely: {exc}"})
    state = queued.get("state") or "queued"
    position = int(queued.get("position") or 0)
    message = ("Training is running now" if state == "active" else
               f"Training queued at position {position}; it will start automatically")
    return self.send_json(202, {
        "ok": True,
        "state": state,
        "queue_position": position,
        "ahead": int(queued.get("ahead") or 0),
        "rebuild": bool(queued.get("rebuild")),
        "rebuild_reason": queued.get("rebuild_reason"),
        "learning_path": learning_path or (
            "rebuild" if queued.get("rebuild") else "incremental_replay"
        ),
        "resumed": bool(resumed),
        "message": message,
    })


def _teach_queue_busy(agent_id):
    queued = TRAINING_QUEUE.status_for(agent_id) if TRAINING_QUEUE else None
    return queued if queued and queued.get("state") in ("queued", "active") else None


def do_post(self):
    path, _, _ = self.path.partition("?")

    if path.startswith("/api/agents/") and path.endswith(("/teach-rl", "/undo-teach-rl", "/teach-rl-train")):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        agent = core.STORE.get_agent_config(agent_id)
        if not agent:
            return self.send_json(404, {"error": "agent not found"})
        try:
            service = _rl_teaching()
            busy = _teach_queue_busy(agent_id)
            if path.endswith("/teach-rl-train"):
                if busy:
                    status = service.status(agent_id)
                    status["training_queue"] = busy
                    return self.send_json(202, status)
                report = service.prepare_retrain(agent)
                if TRAINING_QUEUE is None:
                    raise RuntimeError("Training queue is not ready")
                queued = TRAINING_QUEUE.enqueue(
                    agent_id, rebuild=True, reason="teach_rl",
                    rebuild_reason="feature_mask_change",
                )
                return self.send_json(202, {"ok": True, "report": report, "training_queue": queued})
            if busy:
                return self.send_json(409, {"error": "Poczekaj na zakończenie Teach RL przed zmianą punktów", "training_queue": busy})
            if path.endswith("/undo-teach-rl"):
                return self.send_json(200, service.undo(agent))
            payload = self.read_json()
            payload = payload if isinstance(payload, dict) else {}
            if payload.get("sample_ts") is None or payload.get("desired_value") is None:
                return self.send_json(400, {"error": "sample_ts and desired_value are required"})
            return self.send_json(200, service.add_label(agent, payload["desired_value"], payload["sample_ts"]))
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(502, {"error": f"Teach RL failed: {type(exc).__name__}: {exc}"})

    if TRAINING_QUEUE is not None and path.startswith("/api/agents/") and path.endswith("/train"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        agent = core.STORE.get_agent(agent_id)
        if not agent:
            return self.send_json(404, {"error": "agent/history engine not found"})
        decision = train_request_decision(
            agent, core.STORE.get_model(agent_id) is not None
        )
        return _queue_agent(
            self, agent_id,
            rebuild=bool(decision["rebuild"]),
            reason="training",
            resumed=bool(decision["resumed"]),
            rebuild_reason=decision.get("rebuild_reason"),
            learning_path=decision.get("learning_path"),
        )

    if TRAINING_QUEUE is not None and path.startswith("/api/agents/") and path.endswith("/resume"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        agent = core.STORE.get_agent(agent_id)
        if not agent:
            return self.send_json(404, {"error": "agent/history engine not found"})
        existing = TRAINING_QUEUE.status_for(agent_id)
        if existing:
            return _queue_agent(
                self, agent_id, rebuild=bool(existing.get("rebuild")),
                reason="resume_training", resumed=True,
                rebuild_reason=existing.get("rebuild_reason"),
                learning_path=(
                    "rebuild" if existing.get("rebuild")
                    else "incremental_replay"
                ),
            )
        if agent.get("training_state") not in ("paused", "waiting", "training"):
            return self.send_json(409, {"error": "Resume is available only for paused/waiting training"})
        return _queue_agent(self, agent_id, rebuild=False,
                            reason="resume_training", resumed=True)

    return _original_do_post(self)


def do_patch(self):
    path, _, _ = self.path.partition("?")
    if TRAINING_QUEUE is not None and path.startswith("/api/agents/"):
        agent_id = path.split("/")[3]
        queued = TRAINING_QUEUE.status_for(agent_id)
        if queued:
            return self.send_json(409, {
                "error": "Wait for queued/active training before editing this agent",
                "training_queue": queued,
            })
    return _original_do_patch(self)


def do_delete(self):
    path, _, _ = self.path.partition("?")
    if TRAINING_QUEUE is not None and path.startswith("/api/agents/") and path.endswith("/learning"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        return _queue_agent(
            self, agent_id, rebuild=True, reason="full_rebuild", resumed=False,
            rebuild_reason="explicit_manual_rebuild", learning_path="rebuild",
        )

    if TRAINING_QUEUE is not None and path.startswith("/api/agents/"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        queued = TRAINING_QUEUE.status_for(agent_id)
        if queued and queued.get("state") == "active":
            return self.send_json(409, {"error": "Wait for active training before deleting this agent"})
        TRAINING_QUEUE.cancel(agent_id)
    return _original_do_delete(self)


def shutdown_runtime():
    if TRAINING_QUEUE is not None:
        TRAINING_QUEUE.stop()
    _original_shutdown_runtime()


core.initialize_runtime = initialize_runtime
core.shutdown_runtime = shutdown_runtime
core.Handler.status_payload = status_payload
core.Handler.do_POST = do_post
core.Handler.do_PATCH = do_patch
core.Handler.do_DELETE = do_delete


if __name__ == "__main__":
    core.main()
