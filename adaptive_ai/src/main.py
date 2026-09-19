from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import math
import os
import re
import threading
import time
import traceback
from urllib.parse import parse_qs

from settings import (APP_VERSION, OPTIONS, STATIC_DIR, SUPPORTED_TARGETS, clamp, now_ts)

# Runtime-heavy modules are imported only after the Ingress HTTP server is listening.
ENGINE = None
HISTORY = None
EVENT_STREAM = None
STORE = None
AUTOMATION_KNOWLEDGE = None
target_options_for_state = None
default_action_interval = None
assess_control_qualification = None
review_status = None
set_review_approval = None

STARTUP_LOCK = threading.RLock()
STARTUP = {
    "state": "http_starting",
    "step": 0,
    "steps": 7,
    "message": "Starting Adaptive AI web interface",
    "ready": False,
    "error": None,
    "started_at": time.time(),
}


def set_startup(state, step, message, *, ready=False, error=None):
    with STARTUP_LOCK:
        STARTUP.update(
            state=str(state), step=int(step), message=str(message),
            ready=bool(ready), error=None if error is None else str(error),
        )


def startup_snapshot():
    with STARTUP_LOCK:
        data = dict(STARTUP)
    data["elapsed_seconds"] = max(0.0, time.time() - float(data.get("started_at") or time.time()))
    return data


def runtime_available():
    return ENGINE is not None and STORE is not None and AUTOMATION_KNOWLEDGE is not None


def migrate_legacy_fast_intervals():
    """Migrate the old manual-agent UI default of 60 s for genuinely fast targets."""
    if STORE is None or default_action_interval is None:
        return 0
    migrated = []
    for agent in STORE.list_agent_configs():
        if agent.get("auto_created"):
            continue
        try:
            configured = float(agent.get("action_interval") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(configured - 60.0) > 1e-9:
            continue
        desired = float(default_action_interval(agent.get("target_entity"), agent.get("target_property")))
        if desired > 2.0:
            continue
        STORE.update_agent(agent["id"], {"action_interval": desired})
        migrated.append({"agent_id": agent["id"], "from": configured, "to": desired})
    if migrated:
        STORE.event(None, "info", "fast_action_interval_migration",
                    f"Migrated {len(migrated)} manual fast agent(s) from the old 60 s action interval",
                    {"agents": migrated})
    return len(migrated)


def prepare_runtime_extensions():
    """Entrypoint hook: STORE exists, but policy/engine imports have not run yet."""


def prepare_engine_extensions():
    """Entrypoint hook: install observers before any worker consumes HA events."""


def initialize_runtime():
    """Load the control runtime in the background after HTTP is already available."""
    global ENGINE, HISTORY, EVENT_STREAM, STORE, AUTOMATION_KNOWLEDGE
    global target_options_for_state, default_action_interval
    global assess_control_qualification, review_status, set_review_approval
    try:
        set_startup("loading_runtime", 1, "Loading local database and runtime modules")
        from storage import STORE as runtime_store
        STORE = runtime_store
        prepare_runtime_extensions()
        from ha import AUTOMATION_KNOWLEDGE as automation_knowledge
        from context import target_options_for_state as target_options
        from context import default_action_interval as default_interval
        from engine import Engine, HAEventStream
        from history import HistoryManager
        from home_bootstrap import HomeBootstrap
        from qualification import assess_control_qualification as assess_qualification
        from control import review_status as get_review_status, set_review_approval as set_approval

        STORE = runtime_store
        AUTOMATION_KNOWLEDGE = automation_knowledge
        target_options_for_state = target_options
        default_action_interval = default_interval
        assess_control_qualification = assess_qualification
        review_status = get_review_status
        set_review_approval = set_approval

        set_startup("building_runtime", 2, "Preparing device policies and local state")
        ENGINE = Engine()
        migrated = migrate_legacy_fast_intervals()
        if migrated:
            print(f"[startup] Fast-agent interval migration: {migrated}", flush=True)

        prepare_engine_extensions()

        set_startup("starting_engine", 3, "Connecting to Home Assistant and reading current states")
        ENGINE.start()

        set_startup("starting_realtime", 4, "Starting realtime Home Assistant event stream")
        EVENT_STREAM = HAEventStream(ENGINE)
        EVENT_STREAM.start()

        set_startup("starting_history", 5, "Preparing history and training manager")
        HISTORY = HistoryManager(ENGINE)
        ENGINE.history_manager = HISTORY
        ENGINE.home_bootstrap = HomeBootstrap(ENGINE.context, HISTORY, STORE)
        HISTORY.start()

        set_startup("reconciling_control", 6, "Checking persistent Control ownership")
        try:
            leases = ENGINE.executor.handoff.journal.all()
            if leases:
                try:
                    ENGINE.refresh_states()
                except Exception:
                    pass
                result = ENGINE.executor.reconcile_control()
                STORE.event(None, "info" if not result.get("failed") else "warning",
                            "control_startup_reconcile", "Persistent Control ownership reconciled", result)
        except Exception as exc:
            STORE.event(None, "error", "control_startup_reconcile_failed", str(exc), None)

        set_startup("ready", 7, "Adaptive AI runtime is ready", ready=True)
        # Open the background inference gate only after all runtime extensions, realtime
        # state handling, history manager and ownership reconciliation are composed.
        # This prevents the initial all-agent prediction burst from starving Ingress.
        inference_gate = getattr(ENGINE, "inference_enabled", None)
        if inference_gate is not None:
            # Give Ingress/static/status requests a deterministic head start before the
            # first all-agent proactive inference pass. Realtime state is already being
            # ingested and dirty transitions are retained by Engine during this grace.
            ENGINE.startup_inference_not_before = time.monotonic() + 3.0
            inference_gate.set()
            ENGINE.wake_event.set()
        print("Adaptive AI runtime initialized", flush=True)
    except Exception as exc:
        traceback.print_exc()
        set_startup("error", STARTUP.get("step", 0), f"Startup failed: {type(exc).__name__}: {exc}", error=exc)


def entity_summary(state):
    entity_id = state["entity_id"]
    domain = entity_id.split(".", 1)[0]
    attrs = state.get("attributes") or {}
    options = target_options_for_state(state) if target_options_for_state is not None else []
    return {
        "entity_id": entity_id, "domain": domain, "name": attrs.get("friendly_name") or entity_id,
        "state": state.get("state"), "unit": attrs.get("unit_of_measurement"),
        "target_options": options,
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
    server_version = "AdaptiveAI/0.10"

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

    def trusted_client(self):
        # Unit/dev runs without Supervisor are intentionally local-friendly. Under
        # Supervisor, Ingress must arrive from the Supervisor proxy (plus loopback).
        if not os.environ.get("SUPERVISOR_TOKEN"):
            return True
        if not hasattr(self, "client_address") or not self.client_address:
            return True
        peer = str(self.client_address[0])
        configured = os.environ.get("ADAPTIVE_AI_TRUSTED_PROXY_IPS", "172.30.32.2,127.0.0.1,::1")
        allowed = {x.strip() for x in configured.split(",") if x.strip()}
        return peer in allowed

    def require_trusted_client(self):
        if self.trusted_client():
            return True
        self.send_json(403, {"error": "forbidden", "message": "Direct add-on HTTP access is not allowed"})
        return False

    def require_runtime(self):
        startup = startup_snapshot()
        # ENGINE exists before prepare_engine_extensions() completes. Internal installers
        # deliberately use runtime_available() during that construction window, but HTTP
        # clients must not see the half-built runtime: there is no engine loop/inference
        # yet, so Shadow cards would misleadingly show Desired=— for minutes on a slow Pi.
        if runtime_available() and startup.get("ready"):
            return True
        self.send_json(503, {"error": "runtime_starting", "startup": startup})
        return False

    def status_payload(self):
        startup = startup_snapshot()
        if ENGINE is None:
            return {
                "version": APP_VERSION,
                "ha_connected": False,
                "ha_error": None,
                "engine_error": startup.get("error"),
                "state_count": 0,
                "agent_count": 0,
                "average_confidence": 0.0,
                "historical_experience_count": 0,
                "realtime": {"connected": False, "error": None, "registry_entries": 0, "last_event": None},
                "history": {"phase": "starting", "progress": 0.0, "message": startup.get("message"), "archive": {"n": 0, "days": 0, "entities": 0}},
                "home_intelligence": {}, "home_bootstrap": {}, "telemetry": {}, "heavy_job": None,
                "options": OPTIONS, "startup": startup,
            }
        status = ENGINE.status()
        status["startup"] = startup
        return status

    def do_GET(self):
        if not self.require_trusted_client():
            return
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", ""):
                return self.static("index.html", "text/html; charset=utf-8")
            if path == "/style.css":
                return self.static("style.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self.static("app.js", "application/javascript; charset=utf-8")
            if path == "/p0.js":
                return self.static("p0.js", "application/javascript; charset=utf-8")
            if path == "/experiments.js":
                return self.static("experiments.js", "application/javascript; charset=utf-8")
            if path == "/settings.js":
                return self.static("settings.js", "application/javascript; charset=utf-8")
            if path == "/home.js":
                return self.static("home.js", "application/javascript; charset=utf-8")
            if path == "/api/status":
                return self.send_json(200, self.status_payload())
            if path == "/health":
                startup = startup_snapshot()
                return self.send_json(200, {"ok": startup.get("error") is None, "ready": bool(startup.get("ready")), "version": APP_VERSION, "startup": startup})
            if not self.require_runtime():
                return
            if path == '/api/live':
                # No model serialization, history counts, recommendations or status.
                agents = STORE.list_agent_configs()
                with ENGINE.lock:
                    from context import target_value
                    values = [{"id": a['id'], "current_value": target_value(ENGINE.state_map.get(a['target_entity']), a['target_property']),
                        "last_prediction": (ENGINE.runtime.get(a['id']) or {}).get('last_prediction'),
                        "teaching_id": (ENGINE.runtime.get(a['id']) or {}).get('teaching_id')}
                        for a in agents]
                payload = {"ts": time.time(), "agents": values}
                if parse_qs(query).get('bootstrap') == ['1']:
                    payload['configs'] = agents
                return self.send_json(200, payload)
            if path.startswith('/api/agents/') and path.endswith(('/teaching-history', '/teaching-point')):
                agent = STORE.get_agent_config(path.split('/')[3])
                if not agent:
                    return self.send_json(404, {'error': 'agent not found'})
                params = parse_qs(query)
                try:
                    if path.endswith('/teaching-point'):
                        result = ENGINE.teaching.point(ENGINE, agent, params.get('ts', [None])[0])
                    else:
                        result = ENGINE.teaching.history(ENGINE, agent, params.get('start', [None])[0], params.get('end', [None])[0])
                    return self.send_json(200, result)
                except (ValueError, TypeError) as exc:
                    return self.send_json(400, {'error': str(exc)})
            if path.startswith('/api/agents/') and path.endswith('/export'):
                agent = STORE.get_agent(path.split('/')[3])
                if not agent or agent.get('training_state') != 'qualified':
                    return self.send_json(409, {'error': 'Train a compatible model first'})
                return self.send_json(200, ENGINE.policy(agent).inference_export())
            if path.startswith('/api/agents/') and path.endswith('/experiments'):
                agent = STORE.get_agent_config(path.split('/')[3])
                if not agent:
                    return self.send_json(404, {'error': 'agent not found'})
                return self.send_json(200, ENGINE.experiments.status(agent['id']))
            if path == "/api/entities":
                with ENGINE.lock:
                    states = list(ENGINE.state_map.values())
                return self.send_json(200, sorted([entity_summary(s) for s in states], key=lambda x: (x["domain"], x["name"].lower())))
            if path == "/api/agents":
                agents = STORE.list_agents()
                for a in agents:
                    runtime = ENGINE.runtime_for(a)
                    internal = ENGINE.runtime.get(a["id"]) or {}
                    runtime["event_to_service_ms"] = internal.get("event_to_service_ms")
                    runtime["intent_to_service_ms"] = internal.get("intent_to_service_ms")
                    ack = runtime.get("ack_latency_seconds")
                    runtime["service_to_ack_ms"] = None if ack is None else float(ack) * 1000.0
                    if runtime.get("event_to_service_ms") is not None and runtime.get("service_to_ack_ms") is not None:
                        runtime["event_to_ack_ms"] = float(runtime["event_to_service_ms"]) + float(runtime["service_to_ack_ms"])
                    else:
                        runtime["event_to_ack_ms"] = None
                    with ENGINE.lock:
                        target_state = ENGINE.state_map.get(a["target_entity"])
                    a["control_qualification"] = assess_control_qualification(a)
                    a["control_review"] = review_status(STORE, a, target_state)
                    a["control_lease"] = ENGINE.executor.handoff.journal.get(a["target_entity"])
                    a["runtime"] = runtime
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
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def _release_before_heavy_job(self, agent, reason):
        if agent and agent.get("mode") == "control":
            ENGINE.executor.release_control(agent, reason=reason)

    def do_POST(self):
        if not self.require_trusted_client():
            return
        path, _, _ = self.path.partition("?")
        try:
            if not self.require_runtime():
                return
            if path.startswith('/api/agents/') and path.endswith('/experiments'):
                agent = STORE.get_agent_config(path.split('/')[3])
                if not agent:
                    return self.send_json(404, {'error': 'agent not found'})
                payload = self.read_json()
                with ENGINE.executor.target_lock(agent['target_entity']):
                    agent = STORE.get_agent_config(agent['id'])
                    if not agent:
                        return self.send_json(404, {'error': 'agent not found'})
                    try:
                        result = ENGINE.experiments.configure(agent, payload)
                    except ValueError as exc:
                        return self.send_json(400, {'error': str(exc)})
                ENGINE.wake_event.set()
                return self.send_json(200, result)
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
                initial_training = list(getattr(HISTORY, "initial_training_enqueued", []) or [])
                training_started = sum(1 for row in initial_training if row.get("state") == "active")
                return self.send_json(200, {
                    "ok": True,
                    "created": created,
                    "training_started": training_started,
                    "training_queued": len(initial_training),
                    "manual_training": False,
                    "training_mode": "automatic_initial_fifo",
                    "history": HISTORY.status(),
                    "automation_knowledge": AUTOMATION_KNOWLEDGE.status(),
                })
            if path.startswith("/api/agents/") and path.endswith("/train"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") == "training":
                    return self.send_json(409, {"error": "this agent is already training"})
                try:
                    self._release_before_heavy_job(agent, "training")
                except Exception as exc:
                    return self.send_json(502, {"error": f"Could not release Control before training: {exc}"})
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
                try:
                    self._release_before_heavy_job(agent, "resume_training")
                except Exception as exc:
                    return self.send_json(502, {"error": f"Could not release Control before training: {exc}"})
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
        if not self.require_trusted_client():
            return
        path, _, _ = self.path.partition("?")
        try:
            if not self.require_runtime():
                return
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                payload = self.read_json()
                review_request = payload.pop("control_reviewed", None)
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
                    if review_request is not None:
                        with ENGINE.lock:
                            target_state = ENGINE.state_map.get(existing["target_entity"])
                        if not target_state:
                            return self.send_json(409, {"error": "Target state unavailable; cannot review Control guard"})
                        set_review_approval(STORE, existing, target_state, bool(review_request))
                        if not bool(review_request) and existing.get("mode") == "control":
                            payload["mode"] = "shadow"

                    model_change = any(k in payload for k in ("min_value", "max_value", "input_entities"))
                    disabling = "enabled" in payload and not bool(payload.get("enabled"))
                    leaving_control = existing.get("mode") == "control" and (
                        ("mode" in payload and payload.get("mode") != "control") or model_change or disabling
                    )
                    if leaving_control:
                        try:
                            ENGINE.executor.release_control(existing, reason="mode_or_settings_change")
                        except Exception as exc:
                            return self.send_json(502, {"error": f"Could not release Control safely: {exc}"})

                    if payload.get("mode") == "control":
                        if model_change:
                            return self.send_json(409, {"error": "Save model changes and Train before enabling Control"})
                        if existing.get("training_state") != "qualified":
                            return self.send_json(409, {"error": "Control requires a completed historical benchmark. Use Train/Resume first."})
                        qualification = assess_control_qualification(existing)
                        if not qualification.get("passed"):
                            return self.send_json(409, {"error": qualification.get("reason"), "control_qualification": qualification})
                        try:
                            ENGINE.take_control(existing, refresh=True)
                        except Exception as exc:
                            STORE.event(agent_id, "error", "automation_takeover_failed", str(exc))
                            return self.send_json(502, {"error": f"Control transition failed: {exc}. Any partial handoff was rolled back when possible."})

                    try:
                        agent = STORE.update_agent(agent_id, payload) if payload else STORE.get_agent(agent_id)
                    except Exception:
                        if payload.get("mode") == "control":
                            try:
                                ENGINE.executor.release_control(existing, reason="mode_commit_failed")
                            except Exception:
                                pass
                        raise
                    if payload.get("mode") == "control":
                        ENGINE.release_manual_hold(agent_id)
                    if not agent:
                        return self.send_json(404, {"error": "agent not found"})
                    if model_change:
                        ENGINE.models.pop(agent_id, None)
                        ENGINE.runtime.pop(agent_id, None)
                    return self.send_json(200, agent)
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_DELETE(self):
        if not self.require_trusted_client():
            return
        path, _, _ = self.path.partition("?")
        try:
            if not self.require_runtime():
                return
            if path.startswith("/api/agents/") and path.endswith("/learning"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if HISTORY is None or not agent:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                try:
                    self._release_before_heavy_job(agent, "full_rebuild")
                except Exception as exc:
                    return self.send_json(502, {"error": f"Could not release Control before rebuild: {exc}"})
                if not HISTORY.request_agent_rebuild(agent_id):
                    return self.send_json(409, {"error": "Another heavy job is active"})
                return self.send_json(202, {"ok": True, "state": "training", "message": "Full rebuild scheduled from the beginning of local history"})
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if agent and agent.get("mode") == "control":
                    try:
                        ENGINE.executor.release_control(agent, reason="agent_deleted")
                    except Exception as exc:
                        return self.send_json(502, {"error": f"Could not restore previous controllers before deletion: {exc}"})
                STORE.delete_agent(agent_id)
                ENGINE.models.pop(agent_id, None)
                ENGINE.runtime.pop(agent_id, None)
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})


def shutdown_runtime():
    try:
        if ENGINE is not None:
            try:
                errors = ENGINE.executor.release_all_control(reason="shutdown")
                if errors and STORE is not None:
                    STORE.event(None, "error", "control_shutdown_restore_failed",
                                "Some Control leases could not be restored during shutdown", {"errors": errors})
            except Exception:
                traceback.print_exc()
            ENGINE.context.save(force=True)
            if ENGINE.home_bootstrap:
                ENGINE.home_bootstrap.cancel()
            ENGINE.stop_event.set()
            ENGINE.control_workers.shutdown(wait=False, cancel_futures=True)
            ENGINE.poll_worker.shutdown(wait=False, cancel_futures=True)
        if HISTORY is not None:
            HISTORY.stop_event.set()
        if EVENT_STREAM is not None:
            EVENT_STREAM.stop_event.set()
    except Exception:
        traceback.print_exc()


def run_initialize_runtime():
    """Also surface failures in outer runtime wrappers (for example the FIFO queue)."""
    try:
        initialize_runtime()
    except Exception as exc:
        traceback.print_exc()
        set_startup('error', STARTUP.get('step', 0),
                    f'Startup failed: {type(exc).__name__}: {exc}', error=exc)


def main():
    try:
        nice_by = int(OPTIONS.get("process_nice", 10))
        if nice_by > 0 and hasattr(os, "nice"):
            os.nice(nice_by)
            print(f"Adaptive AI process niceness increased by {nice_by}; Home Assistant keeps CPU priority", flush=True)
    except Exception as exc:
        print(f"[startup] Could not adjust process niceness: {exc}", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    set_startup("http_ready", 0, "Web interface ready; starting Adaptive AI runtime")
    print("Adaptive AI UI listening on :8099", flush=True)
    runtime_thread = threading.Thread(target=run_initialize_runtime, name="adaptive-ai-runtime-init", daemon=True)
    runtime_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_runtime()
        server.server_close()


if __name__ == "__main__":
    main()
