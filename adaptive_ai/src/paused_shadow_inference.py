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
import traceback


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
    """Patch only runtime scheduling/UI diagnostics; never Control qualification/Executor."""
    if getattr(core, "_PAUSED_SHADOW_INFERENCE_INSTALLED", False):
        return False
    if not core.runtime_available() or core.ENGINE is None or core.STORE is None:
        return False

    engine = core.ENGINE
    store = core.STORE
    original_runtime_for = engine.runtime_for

    def process(state_map, changed_entities=None):
        changed = set(changed_entities or ())
        groups = {}
        for agent in store.list_agent_configs():
            if not inference_eligible(agent, store, engine):
                engine.experiments.cancel(agent['id'], 'mode, training or availability changed')
                continue
            if changed:
                cached = engine.models.get(agent["id"])
                if not (changed & engine.event_dependencies(agent, cached)):
                    continue
            groups.setdefault(agent["target_entity"], []).append(agent)

        for target, agents in groups.items():
            active = engine.in_flight.get(target)
            if active is not None and not active.done():
                if changed and target not in engine.resubmit_targets:
                    engine.resubmit_targets.add(target)

                    def retry_completed(_future, entity=target):
                        with engine.lock:
                            engine.resubmit_targets.discard(entity)
                            engine.dirty_entities.add(entity)
                        engine.wake_event.set()

                    active.add_done_callback(retry_completed)
                continue
            engine.in_flight[target] = engine.control_workers.submit(
                engine.process_target, agents, changed
            )
        for target in list(engine.in_flight):
            if target not in groups and engine.in_flight[target].done():
                del engine.in_flight[target]

    def process_target(agents, changed_entities=None):
        with engine.lock:
            revision = engine.state_revision
            states = dict(engine.state_map)
        for agent in agents:
            if engine.stop_event.is_set():
                return
            try:
                latest = store.get_agent_config(agent["id"])
                if inference_eligible(latest, store, engine):
                    if changed_entities:
                        engine.process_agent(latest, states, changed_entities)
                    else:
                        engine.process_agent(latest, states)
            except Exception as exc:
                store.event(
                    agent["id"], "error", "agent_error", str(exc),
                    {"trace": traceback.format_exc(limit=4)},
                )
        if engine.state_revision != revision:
            engine.wake_event.set()

    def runtime_for(agent):
        payload = original_runtime_for(agent)
        payload["paused_shadow_inference"] = paused_shadow_eligible(agent, store, engine)
        payload["inference_eligible"] = inference_eligible(agent, store, engine)
        return payload

    engine.process = process
    engine.process_target = process_target
    engine.runtime_for = runtime_for
    core._PAUSED_SHADOW_INFERENCE_INSTALLED = True
    return True
