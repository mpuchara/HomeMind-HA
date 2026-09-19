"""Allow a trained-but-paused policy to keep running inference in Shadow.

`training_state='paused'` means historical/offline training is paused or did not clear
Control qualification. It must not silently mean that Shadow inference is disabled when
the user explicitly selects mode='shadow'.

This extension keeps Control strict:
- qualified policies may run in Shadow or Control, unchanged;
- paused policies may run only in Shadow and only when a persisted/current model exists;
- waiting/training/needs_retrain policies remain inactive;
- paused policies never become Control-qualified by this module.

The Executor remains untouched. Shadow decisions still flow through the normal
Policy -> ActionIntent -> Executor path, where mode='shadow' prevents HA service calls.
"""

def paused_shadow_eligible(agent, store, engine=None):
    """Return True only when an existing paused policy may safely infer in Shadow."""
    if not agent or not agent.get("enabled"):
        return False
    if str(agent.get("mode") or "paused") != "shadow":
        return False
    if str(agent.get("training_state") or "") != "paused":
        return False
    aid = str(agent.get("id") or "")
    if not aid:
        return False
    if engine is not None and aid in (getattr(engine, "models", {}) or {}):
        return True
    try:
        raw = store.get_model(aid)
    except Exception:
        return False
    return isinstance(raw, dict) and bool(raw)


def inference_eligible(agent, store, engine=None):
    """Runtime inference eligibility, intentionally separate from Control qualification."""
    if not agent or not agent.get("enabled"):
        return False
    if str(agent.get("mode") or "paused") == "paused":
        return False
    if str(agent.get("training_state") or "") == "qualified":
        return True
    return paused_shadow_eligible(agent, store, engine)


def install(core):
    """Extend eligibility only; the core Engine remains the single runtime scheduler."""
    if getattr(core, "_PAUSED_SHADOW_INFERENCE_INSTALLED", False):
        return False
    if not core.runtime_available() or core.ENGINE is None or core.STORE is None:
        return False

    engine = core.ENGINE
    store = core.STORE
    original_runtime_for = engine.runtime_for

    # Do not patch Engine.process/process_target. The core scheduler owns event indexing,
    # pass-level snapshots and bounded workers; replacing it here previously reintroduced
    # all-agent scans and per-agent SQLite reads on every HA event.
    engine.inference_eligible = lambda agent: inference_eligible(agent, store, engine)
    if hasattr(engine, "agent_index_at"):
        engine.agent_index_at = 0.0

    def runtime_for(agent):
        payload = original_runtime_for(agent)
        payload["paused_shadow_inference"] = paused_shadow_eligible(agent, store, engine)
        payload["inference_eligible"] = inference_eligible(agent, store, engine)
        return payload

    engine.runtime_for = runtime_for
    core._PAUSED_SHADOW_INFERENCE_INSTALLED = True
    return True
