"""Expose Candidate status without making the Teach dialog think Live is training.

Candidate work runs under a hidden surrogate ID.  The parent Live agent must remain
editable: users may add/undo more Teach points while Candidate is queued or building; those
edits only advance the Candidate feedback revision and cause a newer build when required.

This late Teach extension also installs the observed-Desired history overlay after
``RLTeaching`` exists, so the Teach chart shows the same effective Desired that was visible
on the live agent card instead of replaying today's policy over yesterday's context.
"""

from teach_observed_history import install as install_observed_history


def install(core, manager):
    service = getattr(core.ENGINE, "rl_teaching", None)
    store = getattr(core, "STORE", None)
    # Production Store/RLTeaching expose these persistence/history contracts.  Keep the
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

    handler.do_GET = do_get
    handler._agent_candidate_teach_status = True
    return manager
