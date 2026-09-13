"""Make explicit/manual teaching a first-class signal throughout the agent lifecycle.

Qualified agents are already handled by the core/manual_feedback path. Waiting,
paused and needs-retrain agents do not normally run inference, so without this bridge a
wall switch would be invisible to online learning until after Train. This module lets
those agents accumulate direct user demonstrations immediately while still keeping
Control disabled until the normal qualification benchmark passes.

Home Assistant integrations commonly report a physical device change with no user_id.
A target change with no parent context and no matching HomeMind command echo is therefore
treated as a direct-device/manual demonstration. Changes carrying a parent context are
left to automations/integrations and are not labelled as user preference here.
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

    # Extend the UI correction path. The base implementation always performs the user
    # command; for pre-qualified agents it intentionally does not touch the model. Add
    # the same +/- preference update here so manual teaching can seed the policy.
    base_apply = feedback.apply_ui_correction

    def apply_ui_correction(core_arg, agent, desired_value=None):
        prelearned_positive = False
        prelearned_negative = False
        if agent.get("training_state") != "qualified" and _trainable_now(agent):
            engine = core_arg.ENGINE
            with engine.lock:
                state_map = dict(engine.state_map)
            state = state_map.get(agent["target_entity"])
            current = target_value(state, agent["target_property"])
            if current is not None and math.isfinite(float(current)):
                desired = desired_value
                if desired is None and agent["target_property"] == "power":
                    desired = 0.0 if float(current) >= 0.5 else 1.0
                if desired is not None:
                    desired = feedback._manual_value(agent, state, desired)
                    if not feedback._same(agent, current, desired):
                        rt = engine.runtime.setdefault(agent["id"], {})
                        prelearned_negative = feedback._negative_prediction(
                            engine, agent, state_map, rt.get("last_prediction"), desired,
                            feedback.UI_USER_ID, "manual correction: rejected prediction (UI)"
                        )
                        feedback._positive_demonstration(
                            engine, agent, state_map, desired, feedback.UI_USER_ID,
                            "manual demonstration (UI, pre-qualification)"
                        )
                        prelearned_positive = True
        result = base_apply(core_arg, agent, desired_value)
        if prelearned_positive:
            result["positive_applied"] = True
            result["negative_applied"] = bool(result.get("negative_applied") or prelearned_negative)
            result["learning_phase"] = "pre_qualification"
        return result

    feedback.apply_ui_correction = apply_ui_correction

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
                feedback._negative_prediction(
                    engine, agent, state_map, rt.get("last_prediction"), after, actor,
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
                     "training_state": agent.get("training_state")},
                )

        engine.on_state_changed = on_state_changed
        engine._manual_lifecycle_events_installed = True
        core.STORE.event(None, "info", "manual_lifecycle_ready",
                         "Manual teaching is active for UI and direct-device changes across the agent lifecycle", None)

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_lifecycle_installed = True
