"""Make explicit/manual teaching a first-class signal throughout the agent lifecycle.

Qualified agents are already handled by the core/manual_feedback path.  Waiting,
paused and needs-retrain agents do not normally run inference, so without this bridge a
wall switch would be invisible to online learning until after Train.  This module lets
those agents accumulate direct user demonstrations immediately while still keeping
Control disabled until the normal qualification benchmark passes.
"""
import math


def _trainable_now(agent):
    # Avoid mutating the policy while the historical training worker owns it.
    return str(agent.get("training_state") or "waiting") != "training"


def install(core):
    if getattr(core, "_manual_feedback_lifecycle_installed", False):
        return

    import manual_feedback as feedback
    from context import target_value

    # Extend the UI correction path.  The base implementation always performs the user
    # command; for pre-qualified agents it intentionally does not touch the model.  Add
    # the same +/- preference update here so manual teaching can be the seed dataset.
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

    # Install a lightweight state_changed bridge for agents that are not yet qualified.
    # Qualified agents keep using Engine.process_agent, which has richer pending-action
    # semantics and is already wrapped by manual_feedback for Shadow prediction penalty.
    base_initialize = core.initialize_runtime

    def initialize_runtime():
        base_initialize()
        if not core.runtime_available():
            return
        engine = core.ENGINE
        if getattr(engine, "_manual_prequalification_events_installed", False):
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
            ctx = (new_state.get("context") or {}) if isinstance(new_state, dict) else {}
            user_id = ctx.get("user_id") if not ctx.get("parent_id") else None
            if not user_id:
                return
            for agent in core.STORE.list_agent_configs():
                if (not agent.get("enabled") or agent.get("target_entity") != entity_id
                        or agent.get("training_state") == "qualified" or not _trainable_now(agent)):
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
                    # Includes corrections initiated from the app; those were already
                    # learned in apply_ui_correction and must not be counted twice.
                    continue
                with engine.lock:
                    state_map = dict(engine.state_map)
                rt = engine.runtime.setdefault(agent["id"], {})
                feedback._negative_prediction(
                    engine, agent, state_map, rt.get("last_prediction"), after, user_id,
                    "manual correction: rejected prediction (physical, pre-qualification)"
                )
                feedback._positive_demonstration(
                    engine, agent, state_map, after, user_id,
                    "manual demonstration (physical, pre-qualification)"
                )
                engine.set_manual_hold(agent, rt, __import__("time").time())
                rt["last_change_origin"] = "manual_user"
                rt["last_manual_correction_source"] = "physical"
                core.STORE.event(
                    agent["id"], "info", "manual_prequalification_demo",
                    f"Manual teaching {before} → {after} recorded before qualification",
                    {"before": before, "after": after, "user_id": user_id},
                )

        engine.on_state_changed = on_state_changed
        engine._manual_prequalification_events_installed = True
        core.STORE.event(None, "info", "manual_prequalification_ready",
                         "Manual teaching is active for waiting/paused agents", None)

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_lifecycle_installed = True
