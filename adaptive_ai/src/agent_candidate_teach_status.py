"""Expose Candidate status without making the Teach dialog think Live is training.

Candidate work runs under a hidden surrogate ID. The parent Live agent must remain
editable: users may add/undo more Teach points while Candidate is queued or building; those
edits only advance the Candidate feedback revision and cause a newer build when required.

The explicit "Build Candidate now" action is a build request, not new feedback. It must
therefore be idempotent while a Candidate is already queued/building and must never advance
the feedback revision by itself.

This late Teach extension also installs the observed-Desired history overlay after
``RLTeaching`` exists, so the Teach chart shows the same effective Desired that was visible
on the live agent card instead of replaying today's policy over yesterday's context.
"""

import json
import time

from agent_candidates import _blank_comparison, is_candidate
from teach_observed_history import install as install_observed_history


def _install_build_request(core, manager):
    if getattr(manager, "_candidate_build_request_installed", False):
        return manager

    store = core.STORE
    original_status = manager.status

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        feedback_revision = int(result.get("feedback_revision") or 0)
        build_revision = int(result.get("build_revision") or 0)
        dirty = bool(result.get("dirty"))
        # "stale" means exactly what the UI says: a build snapshot exists and newer
        # explicit feedback arrived afterwards. A first queued build is merely pending,
        # not stale.
        newer_feedback = bool(build_revision > 0 and feedback_revision > build_revision)
        result["stale"] = newer_feedback
        result["new_feedback_since_build"] = newer_feedback
        result["build_pending"] = bool(result.get("state") == "queued" or dirty)
        result["build_current_revision"] = bool(feedback_revision == build_revision and not dirty)
        return result

    def request_build(parent_id, reason="teach_train"):
        parent_id = str(parent_id)
        parent = store.get_agent_config(parent_id)
        if not parent or is_candidate(store, parent_id):
            raise ValueError("live agent not found")
        with manager.lock:
            row = manager._candidate_row(parent_id)
            if not row:
                row = manager._create_candidate(parent)
            state = str(row.get("state") or "queued")
            now = time.time()

            if state == "building":
                # Most important guard: pressing Build Candidate now during an active
                # build does not create a fake feedback revision and cannot stale the
                # build that is already running.
                manager.wake_event.set()
                return manager.status(parent_id)

            with store.lock, store.conn() as c:
                if state == "queued":
                    # Preserve revisions and dirty state; only make this an immediate
                    # request so the debounce layer does not wait for more Teach clicks.
                    c.execute(
                        """UPDATE agent_candidates SET reason=?,queued_ts=?,last_error=NULL,
                           discard_requested=0,updated_ts=? WHERE parent_agent_id=?""",
                        (str(reason), now, now, parent_id),
                    )
                else:
                    # Rebuild the current data snapshot from a completed/failed Candidate
                    # without pretending that new user feedback arrived.
                    c.execute(
                        """UPDATE agent_candidates SET state='queued',reason=?,dirty=1,queued_ts=?,
                           comparison_json=?,last_error=NULL,discard_requested=0,updated_ts=?
                           WHERE parent_agent_id=?""",
                        (str(reason), now, json.dumps(_blank_comparison()), now, parent_id),
                    )
            fresh = manager._candidate_row(parent_id) or row
            store.event(
                parent_id, "info", "agent_candidate_build_requested",
                "Candidate build requested for the current feedback revision",
                {"candidate_id": fresh.get("candidate_id"),
                 "feedback_revision": int(fresh.get("feedback_revision") or 0),
                 "build_revision": int(fresh.get("build_revision") or 0),
                 "reason": str(reason)},
            )
            manager.wake_event.set()
            return manager.status(parent_id)

    manager.status = status
    manager.request_build = request_build
    manager._candidate_build_request_installed = True
    return manager


def install(core, manager):
    manager = _install_build_request(core, manager)
    service = getattr(core.ENGINE, "rl_teaching", None)
    store = getattr(core, "STORE", None)
    # Production Store/RLTeaching expose these persistence/history contracts. Keep the
    # Candidate status extension usable in lightweight test/diagnostic runtimes that only
    # provide status(), without pretending they can serve historical Desired.
    if (
        service is not None
        and callable(getattr(service, "history", None))
        and callable(getattr(service, "point", None))
        and store is not None
        and hasattr(store, "lock")
        and callable(getattr(store, "conn", None))
    ):
        install_observed_history(store, core.ENGINE, service)

    handler = core.Handler
    if getattr(handler, "_agent_candidate_teach_status", False):
        return manager
    original_get = handler.do_GET
    original_post = handler.do_POST

    def do_get(http):
        path, _, _ = http.path.partition("?")
        if path.startswith("/api/agents/") and path.endswith("/teach-rl-status"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            agent_id = path.split("/")[3]
            agent = core.STORE.get_agent_config(agent_id)
            if not agent:
                return http.send_json(404, {"error": "agent not found"})
            service = getattr(core.ENGINE, "rl_teaching", None)
            if service is None:
                return http.send_json(409, {"error": "Teach RL service is not ready"})
            result = service.status(agent_id)
            # Only a real Live-agent queue entry blocks editing. Candidate training is
            # intentionally independent and is surfaced in a separate field/card.
            live_queue = getattr(core, "TRAINING_QUEUE", None)
            result["training_queue"] = live_queue.status_for(agent_id) if live_queue else None
            if core.HISTORY is not None:
                result["history"] = core.HISTORY.status()
            result["candidate"] = manager.status(agent_id)
            return http.send_json(200, result)
        return original_get(http)

    def do_post(http):
        path, _, _ = http.path.partition("?")
        if path.startswith("/api/agents/") and path.endswith("/teach-rl-train"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            agent_id = path.split("/")[3]
            try:
                candidate = manager.request_build(agent_id, "teach_train")
                return http.send_json(202, {"ok": True, "candidate": candidate,
                                            "training_queue": candidate.get("queue") if candidate else None})
            except ValueError as exc:
                return http.send_json(404, {"error": str(exc)})
        return original_post(http)

    handler.do_GET = do_get
    handler.do_POST = do_post
    handler._agent_candidate_teach_status = True
    return manager
