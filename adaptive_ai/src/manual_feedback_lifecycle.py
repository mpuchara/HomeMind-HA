"""Make direct/manual device teaching available throughout the agent lifecycle.

Qualified explicit HA-user changes are handled by Engine.process_agent plus the
manual_feedback wrapper because that path understands pending Control actions.  This
bridge covers waiting/paused/needs-retrain agents and direct physical/device changes
without a Home Assistant user_id.  0.10.4 observes the *whole eligible context* before
applying the +/- policy update so a correction can discover previously unselected
ESPHome/phone/template/etc. entities.
"""
import math
import time


def _trainable_now(agent):
    # Avoid mutating the policy while the historical training worker owns it.
    return str(agent.get("training_state") or "waiting") != "training"


def _manual_origin(state):
    ctx = (state or {}).get("context") or {}
    if ctx.get("parent_id"):
        return None, None
    if ctx.get("user_id"):
        return "explicit_user", str(ctx.get("user_id"))
    # Direct device/integration state changes usually have a context id but no user or
    # parent. They are accepted only after own-command filtering in the event hook.
    return "direct_device", None


def install(core):
    if getattr(core, "_manual_feedback_lifecycle_installed", False):
        return

    import manual_feedback as feedback
    from context import target_value
    from manual_context_learning import observe as observe_manual_context

    # Install a state_changed bridge for two cases not fully covered by process_agent:
    # 1) any manual change while the agent is not qualified;
    # 2) a direct physical/device change without user_id, including qualified agents.
    # Qualified changes with an explicit HA user_id stay on the richer core path so
    # pending Control actions are evaluated exactly once.
    base_initialize = core.initialize_runtime

    def initialize_runtime():
        base_initialize()
        if not core.runtime_available():
            return
        engine = core.ENGINE
        if getattr(engine, "_manual_lifecycle_events_installed", False):
            return
        original_state_changed = engine.on_state_changed

        def on_state_changed(data):
            entity_id = data.get("entity_id") if isinstance(data, dict) else None
            new_state = data.get("new_state") if isinstance(data, dict) else None
            with engine.lock:
                old_state = engine.state_map.get(entity_id) if entity_id else None
            original_state_changed(data)
            if not entity_id or not new_state:
                return
            origin, user_id = _manual_origin(new_state)
            if not origin:
                return
            for agent in core.STORE.list_agent_configs():
                if not agent.get("enabled") or agent.get("target_entity") != entity_id or not _trainable_now(agent):
                    continue
                # Explicit HA-user changes on qualified agents are already processed by
                # Engine.process_agent; direct-device changes are not, so they use this bridge.
                if origin == "explicit_user" and agent.get("training_state") == "qualified":
                    continue
                before = target_value(old_state, agent["target_property"]) if old_state else None
                after = target_value(new_state, agent["target_property"])
                if before is None or after is None:
                    continue
                try:
                    before, after = float(before), float(after)
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(before) and math.isfinite(after)) or feedback._same(agent, before, after):
                    continue
                if engine.own_command_echo(agent, new_state, after):
                    # Includes corrections initiated from the app and ordinary HomeMind
                    # Control commands. Both were already attributed elsewhere.
                    continue
                with engine.lock:
                    state_map = dict(engine.state_map)
                rt = engine.runtime.setdefault(agent["id"], {})
                actor = user_id or "direct_device"
                predicted = rt.get("last_prediction")

                # Persist the broad context and possibly rotate the compact schema before
                # the positive/negative update.  The current correction can therefore
                # train a newly promoted sensor immediately.
                context_learning = observe_manual_context(
                    core, agent, state_map, after, rejected=predicted,
                    source=origin, user_id=actor,
                )
                negative = feedback._negative_prediction(
                    engine, agent, state_map, predicted, after, actor,
                    f"manual correction: rejected prediction ({origin})"
                )
                feedback._positive_demonstration(
                    engine, agent, state_map, after, actor,
                    f"manual demonstration ({origin})"
                )
                engine.set_manual_hold(agent, rt, time.time())
                rt["last_change_origin"] = "manual_user" if origin == "explicit_user" else "manual_device"
                rt["last_manual_correction_source"] = origin
                core.STORE.event(
                    agent["id"], "info", "manual_lifecycle_demo",
                    f"Manual teaching {before} → {after} recorded ({origin})",
                    {"before": before, "after": after, "user_id": user_id, "origin": origin,
                     "negative_applied": bool(negative), "context_learning": context_learning,
                     "training_state": agent.get("training_state")},
                )

        engine.on_state_changed = on_state_changed
        engine._manual_lifecycle_events_installed = True
        core.STORE.event(None, "info", "manual_lifecycle_ready",
                         "Manual teaching is active for UI and direct-device changes across the agent lifecycle", None)

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_lifecycle_installed = True
