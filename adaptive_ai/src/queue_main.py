"""Runtime wrapper that adds FIFO training admission without bloating main.py.

`main.py` remains the core HTTP/runtime implementation.  This module installs the
training queue hooks before calling it, so a busy low-memory training slot becomes
"queued" instead of an HTTP 409.
"""
import traceback

import main as core
from training_queue import TrainingQueue


TRAINING_QUEUE = None


_original_initialize_runtime = core.initialize_runtime
_original_shutdown_runtime = core.shutdown_runtime
_original_status_payload = core.Handler.status_payload
_original_do_get = core.Handler.do_GET
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
    core.STORE.event(None, "info", "training_queue_ready",
                     "FIFO training queue ready; heavy jobs will run one at a time", None)


def status_payload(self):
    payload = _original_status_payload(self)
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


def do_get(self):
    path, _, _ = self.path.partition("?")
    if path == "/queue.js":
        if not self.require_trusted_client():
            return
        return self.static("queue.js", "application/javascript; charset=utf-8")
    if path == "/api/agents" and TRAINING_QUEUE is not None:
        if not self.require_trusted_client():
            return
        try:
            if not self.require_runtime():
                return
            return self.send_json(200, _agent_payloads(self))
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})
    return _original_do_get(self)


def _queue_agent(self, agent_id, *, rebuild, reason, resumed=False):
    if TRAINING_QUEUE is None:
        return self.send_json(409, {"error": "Training queue is not ready"})
    agent = core.STORE.get_agent(agent_id)
    if not agent or core.HISTORY is None:
        return self.send_json(404, {"error": "agent/history engine not found"})
    try:
        queued = TRAINING_QUEUE.enqueue(agent_id, rebuild=rebuild, reason=reason)
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
        "resumed": bool(resumed),
        "message": message,
    })


def do_post(self):
    path, _, _ = self.path.partition("?")
    if TRAINING_QUEUE is not None and path.startswith("/api/agents/") and path.endswith("/train"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        agent = core.STORE.get_agent(agent_id)
        if not agent:
            return self.send_json(404, {"error": "agent/history engine not found"})
        partial = (agent.get("training_state") != "needs_retrain" and
                   agent.get("training_cursor_ts") is not None and
                   float(agent.get("training_progress") or 0.0) < 0.999)
        return _queue_agent(self, agent_id, rebuild=not partial,
                            reason="training", resumed=bool(partial))

    if TRAINING_QUEUE is not None and path.startswith("/api/agents/") and path.endswith("/resume"):
        if not self.require_trusted_client() or not self.require_runtime():
            return
        agent_id = path.split("/")[3]
        agent = core.STORE.get_agent(agent_id)
        if not agent:
            return self.send_json(404, {"error": "agent/history engine not found"})
        existing = TRAINING_QUEUE.status_for(agent_id)
        if existing:
            return _queue_agent(self, agent_id, rebuild=bool(existing.get("rebuild")),
                                reason="resume_training", resumed=True)
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
        return _queue_agent(self, agent_id, rebuild=True, reason="full_rebuild", resumed=False)

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
core.Handler.do_GET = do_get
core.Handler.do_POST = do_post
core.Handler.do_PATCH = do_patch
core.Handler.do_DELETE = do_delete


if __name__ == "__main__":
    core.main()
