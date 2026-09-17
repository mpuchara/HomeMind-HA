"""Instance-owned explicit HTTP routing for staged runtime composition.

The legacy stack grew by wrapping ``Handler.do_*`` in multiple feature modules.  Stage 16
introduces one final dispatcher.  Selected feature routes register by stable names and are
resolved before the compatibility handler chain.  Unmigrated endpoints still fall through
unchanged, which makes the refactor incremental rather than a rewrite.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import urlsplit


CONTRACT_VERSION = 1


class ExplicitRouteRegistry:
    def __init__(self, *, transport_name="http"):
        self.transport_name = str(transport_name)
        self._routes = OrderedDict()
        self._sequence = 0

    def register(self, method, name, pattern, callback, *, require_trusted=True,
                 require_runtime=True, priority=0):
        method = str(method).upper()
        name = str(name)
        key = (method, name)
        existing = self._routes.get(key)
        sequence = existing["sequence"] if existing is not None else self._sequence
        if existing is None:
            self._sequence += 1
        self._routes[key] = {
            "method": method,
            "name": name,
            "pattern_text": str(pattern),
            "pattern": re.compile(str(pattern)),
            "callback": callback,
            "require_trusted": bool(require_trusted),
            "require_runtime": bool(require_runtime),
            "priority": int(priority),
            "sequence": sequence,
        }
        return self._routes[key]

    def unregister(self, method, name):
        return self._routes.pop((str(method).upper(), str(name)), None)

    def routes(self, method=None):
        rows = list(self._routes.values())
        if method is not None:
            rows = [row for row in rows if row["method"] == str(method).upper()]
        rows.sort(key=lambda row: (-row["priority"], row["sequence"], row["name"]))
        return [{k: v for k, v in row.items() if k not in {"pattern", "callback"}} for row in rows]

    def dispatch(self, method, http):
        method = str(method).upper()
        path = urlsplit(http.path).path
        candidates = [row for row in self._routes.values() if row["method"] == method]
        candidates.sort(key=lambda row: (-row["priority"], row["sequence"], row["name"]))
        for row in candidates:
            match = row["pattern"].fullmatch(path)
            if match is None:
                continue
            if row["require_trusted"] and not http.require_trusted_client():
                return True
            if row["require_runtime"] and not http.require_runtime():
                return True
            row["callback"](http, match.groupdict())
            return True
        return False

    def descriptor(self):
        return {
            "version": CONTRACT_VERSION,
            "transport": self.transport_name,
            "routes": self.routes(),
            "fallback": "legacy_handler_chain_for_unmigrated_routes",
            "idempotency": "method+route_name_replaces_in_place_without_stacking",
            "mutable_module_globals": False,
        }


def install_dispatch(core, registry=None):
    """Install exactly one final Handler dispatcher for an explicit registry."""
    existing = getattr(core, "EXPLICIT_HTTP_ROUTES", None)
    if existing is not None and getattr(core, "_explicit_http_dispatch_installed", False):
        return existing
    registry = registry or existing or ExplicitRouteRegistry()
    core.EXPLICIT_HTTP_ROUTES = registry
    handler = core.Handler
    if getattr(handler, "_explicit_http_dispatch_installed", False):
        # The same core/Handler can be rebound to its existing registry without stacking.
        handler._explicit_http_route_registry = registry
        core._explicit_http_dispatch_installed = True
        return registry

    for method_name in ("do_GET", "do_POST", "do_PATCH", "do_DELETE"):
        original = getattr(handler, method_name)
        verb = method_name.split("_", 1)[1]

        def make_dispatch(base, http_method):
            def dispatched(http):
                active = getattr(type(http), "_explicit_http_route_registry", registry)
                if active.dispatch(http_method, http):
                    return None
                return base(http)
            dispatched.__name__ = method_name
            dispatched._explicit_http_fallback = base
            return dispatched

        setattr(handler, method_name, make_dispatch(original, verb))

    handler._explicit_http_route_registry = registry
    handler._explicit_http_dispatch_installed = True
    core._explicit_http_dispatch_installed = True
    return registry


def _feedback_action(core, action):
    from manual_feedback import apply_ui_correction, record_negative_feedback, teach_desired

    def handle(http, params):
        agent_id = params["agent_id"]
        agent = core.STORE.get_agent_config(agent_id)
        if not agent:
            return http.send_json(404, {"error": "agent not found"})
        try:
            payload = http.read_json()
            payload = payload if isinstance(payload, dict) else {}
            desired = payload.get("desired_value")
            error_kind = payload.get("error_kind") or "state"
            scope = payload.get("scope") or "similar_context"
            if action == "teaching":
                result = core.ENGINE.teaching.teach(
                    core.ENGINE, agent, desired, payload.get("sample_ts"),
                    source="teaching", error_kind=error_kind, scope=scope,
                    decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                    generation_id=payload.get("generation_id"), feedback_id=payload.get("feedback_id"),
                )
                return http.send_json(200, result)
            if action == "undo-teaching":
                return http.send_json(200, core.ENGINE.teaching.undo(
                    core.ENGINE, agent, feedback_id=payload.get("feedback_id")
                ))
            if action == "teach-desired":
                return http.send_json(200, teach_desired(
                    core, agent, desired, error_kind=error_kind, scope=scope,
                    decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                ))
            if action == "manual-feedback":
                return http.send_json(200, record_negative_feedback(
                    core, agent, selected_ts=payload.get("selected_ts"),
                    rejected_action=payload.get("rejected_action"), error_kind=error_kind,
                    scope=payload.get("scope") or "episode", decision_id=payload.get("decision_id"),
                    episode_id=payload.get("episode_id"), feedback_id=payload.get("feedback_id"),
                ))
            if action == "undo-feedback":
                journal = getattr(core.ENGINE, "manual_feedback_journal", None)
                if journal is None:
                    raise ValueError("Manual feedback journal unavailable")
                feedback_id = payload.get("feedback_id")
                if not feedback_id:
                    latest = journal.latest(agent["id"])
                    feedback_id = (latest or {}).get("feedback_id")
                if not feedback_id:
                    raise ValueError("No manual feedback to undo")
                row = journal.undo(
                    feedback_id, engine=core.ENGINE,
                    candidate_manager=getattr(core.ENGINE, "agent_candidates", None),
                )
                return http.send_json(200, {
                    "ok": True, "feedback_id": feedback_id, "feedback": row,
                    "ui_message": journal.ui_summary(row),
                })
            return http.send_json(200, apply_ui_correction(
                core, agent, desired, keep_current=bool(payload.get("keep_current", False)),
                error_kind=error_kind, scope=scope,
                decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                feedback_id=payload.get("feedback_id"),
            ))
        except ValueError as exc:
            return http.send_json(400, {"error": str(exc)})
        except Exception as exc:
            return http.send_json(502, {
                "error": f"Manual correction failed: {type(exc).__name__}: {exc}"
            })
    return handle


def register_feedback_routes(registry, core):
    """Replace the final-stack manual-feedback POST wrapper chain with named routes."""
    actions = (
        "manual-correction", "teach-desired", "teaching", "undo-teaching",
        "manual-feedback", "undo-feedback",
    )
    for action in actions:
        registry.register(
            "POST",
            f"feedback.{action}",
            rf"^/api/agents/(?P<agent_id>[^/]+)/{re.escape(action)}$",
            _feedback_action(core, action),
            require_trusted=True,
            require_runtime=True,
            priority=100,
        )
    return registry


def register_promotion_routes(registry, core, manager):
    """Expose Candidate promotion through one explicit route owner."""
    def target_mode(http, params):
        parent_id = params["agent_id"]
        try:
            body = http.read_json()
            body = body if isinstance(body, dict) else {}
            return http.send_json(200, manager.set_promotion_target_mode(
                parent_id, body.get("target_mode")
            ))
        except ValueError as exc:
            return http.send_json(409, {"error": str(exc), "candidate": manager.status(parent_id)})

    def promote(http, params):
        parent_id = params["agent_id"]
        try:
            body = http.read_json()
            body = body if isinstance(body, dict) else {}
            requested = body.get("target_mode")
            if requested is not None:
                manager.set_promotion_target_mode(parent_id, requested)
            return http.send_json(200, manager.promote(parent_id, requested))
        except (ValueError, RuntimeError) as exc:
            return http.send_json(409, {"error": str(exc), "candidate": manager.status(parent_id)})

    def promote_custom(http, params):
        parent_id = params["agent_id"]
        try:
            body = http.read_json()
            body = body if isinstance(body, dict) else {}
            result = manager.promote_custom(
                parent_id, body.get("target_mode"), body.get("conditions")
            )
            return http.send_json(200, result)
        except (ValueError, RuntimeError) as exc:
            return http.send_json(409, {"error": str(exc), "candidate": manager.status(parent_id)})

    registry.register(
        "POST", "promotion.target_mode",
        r"^/api/agents/(?P<agent_id>[^/]+)/candidate/target-mode$",
        target_mode, priority=200,
    )
    registry.register(
        "POST", "promotion.standard",
        r"^/api/agents/(?P<agent_id>[^/]+)/candidate/promote$",
        promote, priority=200,
    )
    registry.register(
        "POST", "promotion.custom",
        r"^/api/agents/(?P<agent_id>[^/]+)/candidate/promote-custom$",
        promote_custom, priority=200,
    )
    return registry
