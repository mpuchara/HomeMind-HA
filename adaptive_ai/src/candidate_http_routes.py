"""Explicit HTTP ownership for legacy Candidate/Live routes in final composition.

Standalone feature installers retain their historical Handler wrappers.  The shipped final
composition disables those wrappers and registers the same user-visible contracts here,
after all Candidate decorators have installed their final manager methods.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, unquote, urlsplit

from agent_candidates import is_candidate
from runtime_http import CONTINUE


PRIORITY = 220


def register_routes(registry, core, manager):
    def candidate_ui(http, _params):
        return http.static("candidate_ui.js", "application/javascript; charset=utf-8")

    def candidate_preference_ui(http, _params):
        return http.static(
            "candidate_preference_ui.js",
            "application/javascript; charset=utf-8",
        )

    def candidates(http, _params):
        return http.send_json(200, {"candidates": manager.list_status()})

    def teach_status(http, params):
        agent_id = params["agent_id"]
        agent = core.STORE.get_agent_config(agent_id)
        if not agent:
            return http.send_json(404, {"error": "agent not found"})
        service = getattr(core.ENGINE, "rl_teaching", None)
        if service is None:
            return http.send_json(409, {"error": "Teach RL service is not ready"})
        result = service.status(agent_id)
        live_queue = getattr(core, "TRAINING_QUEUE", None)
        result["training_queue"] = live_queue.status_for(agent_id) if live_queue else None
        if core.HISTORY is not None:
            result["history"] = core.HISTORY.status()
        result["candidate"] = manager.status(agent_id)
        return http.send_json(200, result)

    def teach_train(http, params):
        agent_id = params["agent_id"]
        try:
            candidate = manager.request_build(agent_id, "teach_train")
            return http.send_json(
                202,
                {
                    "ok": True,
                    "candidate": candidate,
                    "training_queue": candidate.get("queue") if candidate else None,
                },
            )
        except ValueError as exc:
            return http.send_json(404, {"error": str(exc)})

    def generation(http, params, action):
        ref = unquote(params["ref"])
        try:
            if action == "comparison":
                result = manager.generation_comparison(ref)
                if result is None:
                    return http.send_json(
                        404, {"error": "Candidate generation comparison not found"}
                    )
                return http.send_json(200, result)
            query = parse_qs(urlsplit(http.path).query)
            now = time.time()
            start = float((query.get("start") or [now - 3600.0])[0])
            end = float((query.get("end") or [now])[0])
            return http.send_json(200, manager.generation_history(ref, start, end))
        except (TypeError, ValueError) as exc:
            return http.send_json(404, {"error": str(exc)})

    def candidate_live(http, _params):
        snapshots = getattr(manager, "live_snapshots", None)
        values = snapshots() if callable(snapshots) else []
        return http.send_json(200, {"ts": time.time(), "candidates": values})

    def live(http, _params):
        parsed = urlsplit(http.path)
        include_configs = parse_qs(parsed.query).get("bootstrap") == ["1"]
        payload = core.live_agent_payload(include_configs=include_configs)
        return http.send_json(200, payload)

    def discard(http, params):
        return http.send_json(200, manager.discard(params["agent_id"]))

    def learning_guard(http, params):
        agent_id = params["agent_id"]
        if not is_candidate(core.STORE, agent_id):
            existing = manager._candidate_row(agent_id)
            if existing:
                return http.send_json(
                    409,
                    {
                        "error": (
                            "Discard or promote the current Candidate before rebuilding "
                            "the Live agent"
                        ),
                        "candidate": manager.status(agent_id),
                    },
                )
        # Continue to queue.training.learning_rebuild when no Candidate blocks rebuild.
        return CONTINUE

    registry.register(
        "GET", "candidate.static", r"^/candidate_ui\.js$", candidate_ui,
        require_trusted=True, require_runtime=False, priority=PRIORITY,
    )
    registry.register(
        "GET", "candidate.preference_static",
        r"^/candidate_preference_ui\.js$", candidate_preference_ui,
        require_trusted=True, require_runtime=False, priority=PRIORITY,
    )
    registry.register(
        "GET", "candidate.list", r"^/api/candidates$", candidates,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    registry.register(
        "GET", "candidate.teach_rl.status",
        r"^/api/agents/(?P<agent_id>[^/]+)/teach-rl-status$", teach_status,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    registry.register(
        "POST", "candidate.teach_rl.train",
        r"^/api/agents/(?P<agent_id>[^/]+)/teach-rl-train$", teach_train,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    for action in ("history", "comparison"):
        registry.register(
            "GET",
            f"candidate.generation.{action}",
            rf"^/api/candidate-generations/(?P<ref>[^/]+)/{action}$",
            lambda http, params, action=action: generation(http, params, action),
            require_trusted=True,
            require_runtime=True,
            priority=PRIORITY,
        )
    registry.register(
        "GET", "candidate.live", r"^/api/candidate-live$", candidate_live,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    registry.register(
        "GET", "live.fast", r"^/api/live$", live,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    registry.register(
        "DELETE", "candidate.discard",
        r"^/api/agents/(?P<agent_id>[^/]+)/candidate$", discard,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    registry.register(
        "DELETE", "candidate.learning_guard",
        r"^/api/agents/(?P<agent_id>[^/]+)/learning$", learning_guard,
        require_trusted=True, require_runtime=True, priority=PRIORITY,
    )
    return registry
