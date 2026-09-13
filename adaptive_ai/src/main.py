from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import math
import os
import re
import traceback
from settings import (APP_VERSION, OPTIONS, STATIC_DIR, SUPPORTED_TARGETS, clamp, now_ts)
from storage import STORE
from ha import AUTOMATION_KNOWLEDGE
from context import (target_options_for_state)
from engine import Engine, HAEventStream
from history import HistoryManager
from home_bootstrap import HomeBootstrap

ENGINE = Engine()
HISTORY = None

def entity_summary(state):
    entity_id = state["entity_id"]
    domain = entity_id.split(".", 1)[0]
    attrs = state.get("attributes") or {}
    return {
        "entity_id": entity_id, "domain": domain, "name": attrs.get("friendly_name") or entity_id,
        "state": state.get("state"), "unit": attrs.get("unit_of_measurement"),
        "target_options": target_options_for_state(state),
    }


def validate_agent(p):
    if not isinstance(p, dict):
        return "expected an object"
    for key in ("name", "target_entity", "target_property", "min_value", "max_value"):
        if key not in p:
            return f"missing field: {key}"
    if not isinstance(p["target_entity"], str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", p["target_entity"]):
        return "invalid target entity ID"
    domain = p["target_entity"].split(".", 1)[0]
    allowed = {x["property"] for x in SUPPORTED_TARGETS.get(domain, [])}
    if p["target_property"] not in allowed:
        return f"unsupported target property for {domain}"
    try:
        inputs = p.get("input_entities", ["*"])
        if not isinstance(inputs, list) or not inputs or any(not isinstance(x, str) or (x != "*" and not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x)) for x in inputs):
            return "input_entities must be a non-empty list of entity IDs or *"
        for key in ("min_value", "max_value", "deadband", "action_interval", "exploration_step", "exploration_interval", "confidence_threshold", "ack_timeout", "settling_seconds", "manual_hold_seconds"):
            if key in p and not math.isfinite(float(p[key])):
                return f"{key} must be finite"
        for key in ("deadband", "action_interval", "exploration_interval"):
            if key in p and float(p[key]) <= 0:
                return f"{key} must be positive"
        for key in ("ack_timeout", "settling_seconds", "manual_hold_seconds"):
            if key in p and not 0 <= float(p[key]) <= 86400:
                return f"{key} must be between 0 (automatic) and 86400 seconds"
        if not 0 <= float(p.get("confidence_threshold", .75)) <= 1:
            return "confidence_threshold must be between 0 and 1"
        if p.get("mode", "shadow") not in ("shadow", "control", "paused"):
            return "invalid mode"
        if float(p["min_value"]) >= float(p["max_value"]):
            return "minimum must be smaller than maximum"
        if float(p.get("exploration_step", 0)) <= 0:
            return "exploration_step must be positive"
    except Exception:
        return "invalid numeric limits"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AdaptiveAI/0.4"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def send_bytes(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def static(self, name, content_type):
        path = STATIC_DIR / name
        if not path.exists():
            return self.send_json(404, {"error": "not_found"})
        return self.send_bytes(200, path.read_bytes(), content_type)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", ""):
                return self.static("index.html", "text/html; charset=utf-8")
            if path == "/style.css":
                return self.static("style.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self.static("app.js", "application/javascript; charset=utf-8")
            if path == "/settings.js":
                return self.static("settings.js", "application/javascript; charset=utf-8")
            if path == "/home.js":
                return self.static("home.js", "application/javascript; charset=utf-8")
            if path.startswith('/api/agents/') and path.endswith('/export'):
                agent = STORE.get_agent(path.split('/')[3])
                if not agent or agent.get('training_state') != 'qualified':
                    return self.send_json(409, {'error': 'Train a compatible model first'})
                return self.send_json(200, ENGINE.policy(agent).inference_export())
            if path == "/api/status":
                return self.send_json(200, ENGINE.status())
            if path == "/api/entities":
                with ENGINE.lock:
                    states = list(ENGINE.state_map.values())
                return self.send_json(200, sorted([entity_summary(s) for s in states], key=lambda x: (x["domain"], x["name"].lower())))
            if path == "/api/agents":
                agents = STORE.list_agents()
                for a in agents:
                    a["runtime"] = ENGINE.runtime_for(a)
                return self.send_json(200, agents)
            if path.startswith("/api/agents/") and path.endswith("/feedback"):
                agent_id = path.split("/")[3]
                return self.send_json(200, STORE.list_feedback(agent_id, 100))
            if path == "/api/events":
                limit = 100
                if "limit=" in query:
                    try:
                        limit = int(clamp(int(query.split("limit=", 1)[1].split("&", 1)[0]), 1, 500))
                    except Exception:
                        pass
                return self.send_json(200, STORE.list_events(limit))
            if path == "/health":
                return self.send_json(200, {"ok": True, "version": APP_VERSION})
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        try:
            if path in ('/api/home/bootstrap', '/api/home/cancel'):
                if not ENGINE.home_bootstrap:
                    return self.send_json(409, {'error': 'History engine is not ready'})
                if path.endswith('/cancel'):
                    ENGINE.home_bootstrap.cancel()
                    return self.send_json(202, {'ok': True})
                payload = self.read_json()
                if not ENGINE.home_bootstrap.start(import_recorder=payload.get('import_recorder', True)):
                    return self.send_json(409, {'error': 'One heavy training/bootstrap job at a time'})
                return self.send_json(202, {'ok': True})
            if path == "/api/discovery/rescan":
                with ENGINE.lock:
                    current = dict(ENGINE.state_map)
                    registry = dict(ENGINE.entity_registry)
                if not current or HISTORY is None:
                    return self.send_json(409, {"error": "Home Assistant state/history engine not ready"})
                AUTOMATION_KNOWLEDGE.scan(current, registry)
                start_ts = now_ts() - float(OPTIONS["history_bootstrap_days"]) * 86400.0
                created = HISTORY.auto_discover_agents(current, start_ts, threshold_override=1)
                return self.send_json(200, {"ok": True, "created": created, "training_started": 0, "manual_training": True, "history": HISTORY.status(), "automation_knowledge": AUTOMATION_KNOWLEDGE.status()})
            if path.startswith("/api/agents/") and path.endswith("/train"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") == "training":
                    return self.send_json(409, {"error": "this agent is already training"})
                partial = agent.get("training_state") != "needs_retrain" and agent.get("training_cursor_ts") is not None and float(agent.get("training_progress") or 0.0) < 0.999
                started = HISTORY.request_agent_resume(agent_id) if partial else HISTORY.request_agent_rebuild(agent_id)
                if not started:
                    return self.send_json(409, {"error": "another training job is already active; low-memory mode allows one at a time"})
                return self.send_json(202, {"ok": True, "state": "training", "resumed": bool(partial), "message": "Per-agent training started in low-memory mode"})
            if path.startswith("/api/agents/") and path.endswith("/resume"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") != "paused":
                    return self.send_json(409, {"error": "Resume is available only for PAUSED agents"})
                started = HISTORY.request_agent_resume(agent_id)
                if not started:
                    return self.send_json(409, {"error": "training job is already active"})
                return self.send_json(202, {"ok": True, "state": "training", "message": "Resume scheduled from saved cursor"})
            if path.startswith("/api/agents/") and path.endswith("/verify-control"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent:
                    return self.send_json(404, {"error": "agent not found"})
                try:
                    return self.send_json(200, ENGINE.executor.verify(agent))
                except ValueError as exc:
                    return self.send_json(409, {'error': str(exc)})
            if path == "/api/agents":
                payload = self.read_json()
                error = validate_agent(payload)
                if error:
                    return self.send_json(400, {"error": error})
                requested_control = payload.get("mode") == "control"
                agent = STORE.create_agent({**payload, "mode": "paused"} if requested_control else payload)
                if requested_control:
                    return self.send_json(409, {"error": "New candidates must pass the historical behaviour benchmark before Control can be enabled", "agent_id": agent["id"], "mode": "paused"})
                ENGINE.models.pop(agent["id"], None)
                return self.send_json(201, agent)
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_PATCH(self):
        path, _, _ = self.path.partition("?")
        try:
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                payload = self.read_json()
                if "mode" in payload and payload["mode"] not in ("shadow", "control", "paused"):
                    return self.send_json(400, {"error": "invalid mode"})
                existing = STORE.get_agent(agent_id)
                if not existing:
                    return self.send_json(404, {"error": "agent not found"})
                error = validate_agent({**existing, **payload})
                if error:
                    return self.send_json(400, {"error": error})
                if ENGINE.history_manager and agent_id in ENGINE.history_manager.agent_jobs:
                    return self.send_json(409, {'error': 'Wait for this training job before editing the agent'})
                with ENGINE.executor.target_lock(existing['target_entity']):
                    existing = STORE.get_agent(agent_id)
                    if payload.get("mode") == "control":
                        if any(k in payload for k in ("min_value", "max_value", "input_entities")):
                            return self.send_json(409, {"error": "Save model changes and Train before enabling Control"})
                        if existing.get("training_state") != "qualified":
                            score = existing.get("benchmark_score")
                            score_text = f"{score:.1%}" if score is not None else "not benchmarked"
                            return self.send_json(409, {"error": f"Control requires behaviour benchmark > {float(OPTIONS.get('candidate_benchmark_threshold', .78)):.0%}; candidate is {score_text}. Use Resume to continue from the saved cursor, or Rebuild after changing sensors/context."})
                        try:
                            ENGINE.take_control(existing, refresh=True)
                        except Exception as exc:
                            STORE.event(agent_id, "error", "automation_takeover_failed", str(exc))
                            return self.send_json(502, {"error": f"Control transition failed: {exc}. Successfully disabled automations remain off."})
                    agent = STORE.update_agent(agent_id, payload)
                    if payload.get("mode") == "control":
                        ENGINE.release_manual_hold(agent_id)
                    if not agent:
                        return self.send_json(404, {"error": "agent not found"})
                    if any(k in payload for k in ("min_value", "max_value", "input_entities")):
                        ENGINE.models.pop(agent_id, None)
                        ENGINE.runtime.pop(agent_id, None)
                    return self.send_json(200, agent)

            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_DELETE(self):
        path, _, _ = self.path.partition("?")
        try:
            if path.startswith("/api/agents/") and path.endswith("/learning"):
                agent_id = path.split("/")[3]
                if HISTORY is None or not STORE.get_agent(agent_id):
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if not HISTORY.request_agent_rebuild(agent_id):
                    return self.send_json(409, {"error": "Another heavy job is active"})
                return self.send_json(202, {"ok": True, "state": "training", "message": "Full rebuild scheduled from the beginning of local history"})
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                STORE.delete_agent(agent_id)
                ENGINE.models.pop(agent_id, None)
                ENGINE.runtime.pop(agent_id, None)
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})


def main():
    global HISTORY
    try:
        nice_by = int(OPTIONS.get("process_nice", 10))
        if nice_by > 0 and hasattr(os, "nice"):
            os.nice(nice_by)
            print(f"Adaptive AI process niceness increased by {nice_by}; Home Assistant keeps CPU priority", flush=True)
    except Exception as exc:
        print(f"[startup] Could not adjust process niceness: {exc}", flush=True)
    ENGINE.start()
    event_stream = HAEventStream(ENGINE)
    event_stream.start()
    HISTORY = HistoryManager(ENGINE)
    ENGINE.history_manager = HISTORY
    ENGINE.home_bootstrap = HomeBootstrap(ENGINE.context, HISTORY, STORE)
    HISTORY.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    print("Adaptive AI UI listening on :8099", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ENGINE.context.save(force=True)
        if ENGINE.home_bootstrap:
            ENGINE.home_bootstrap.cancel()
        ENGINE.stop_event.set()
        ENGINE.control_workers.shutdown(wait=False, cancel_futures=True)
        ENGINE.poll_worker.shutdown(wait=False, cancel_futures=True)
        if HISTORY is not None:
            HISTORY.stop_event.set()
        event_stream.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
