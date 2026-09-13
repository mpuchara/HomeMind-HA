"""Explicit user corrections for Adaptive AI.

A correction from the UI is intentionally equivalent to a physical/manual device
change: reject the wrong policy choice, reinforce the demonstrated value, apply a
manual-priority hold, and (for the UI path) optionally perform the requested HA service
call.

0.10.4 adds a second teaching operation: "current state is correct".  That operation
changes no device state; it simply rejects a conflicting Desired value and reinforces the
state the user is already observing.  Every correction is also passed to the full-context
observer before the policy update so previously unselected sensors can be discovered and
promoted into the live schema.
"""
import math
import time


UI_USER_ID = "adaptive_ai_ui"


def _same(agent, a, b):
    from control import same_value
    if a is None or b is None:
        return False
    return same_value(float(a), float(b), float(agent.get("deadband") or 0.0))


def _manual_value(agent, state, desired):
    """Validate an explicitly chosen user value without automatic-control step guards.

    The user is the guardrail here, just like a wall switch or thermostat dial.  We
    still intersect with device/user limits and quantize to the hardware step.
    """
    value = float(desired)
    if not math.isfinite(value):
        raise ValueError("desired_value must be finite")
    attrs = (state or {}).get("attributes") or {}
    prop = str(agent.get("target_property") or "")
    domain = str(agent.get("target_entity") or "").split(".", 1)[0]
    lo, hi = float(agent.get("min_value") or 0.0), float(agent.get("max_value") or 0.0)

    if prop == "power":
        return 1.0 if value >= 0.5 else 0.0
    if prop in ("brightness_pct", "position", "percentage", "volume_pct", "humidity"):
        lo, hi = max(lo, 0.0), min(hi, 100.0)
    if prop == "temperature":
        lo = max(lo, float(attrs.get("min_temp", lo)))
        hi = min(hi, float(attrs.get("max_temp", hi)))
    elif prop == "humidity":
        lo = max(lo, float(attrs.get("min_humidity", lo)))
        hi = min(hi, float(attrs.get("max_humidity", hi)))
    elif domain in ("number", "input_number"):
        lo = max(lo, float(attrs.get("min", lo)))
        hi = min(hi, float(attrs.get("max", hi)))
    elif prop == "option_index":
        options = list(attrs.get("options") or [])
        hi = min(hi, float(max(0, len(options) - 1))) if options else hi
        lo = max(lo, 0.0)

    if not all(math.isfinite(x) for x in (lo, hi)) or lo > hi:
        raise ValueError("User limits do not intersect device limits")
    value = min(hi, max(lo, value))

    step = 0.0
    origin = 0.0
    if prop == "temperature":
        step = float(attrs.get("target_temp_step") or 0.0)
        origin = float(attrs.get("min_temp", lo))
    elif domain in ("number", "input_number"):
        step = float(attrs.get("step") or 0.0)
        origin = float(attrs.get("min", lo))
    elif prop == "percentage":
        step = float(attrs.get("percentage_step") or 1.0)
    elif prop in ("brightness_pct", "position", "volume_pct", "humidity", "option_index"):
        step = 1.0
    if step > 0 and math.isfinite(step):
        value = origin + round((value - origin) / step) * step
        value = min(hi, max(lo, value))
    return round(float(value), 8)


def _policy_features(engine, agent, state_map, timestamp):
    policy = engine.policy(agent)
    features, _, _ = policy.features(state_map, engine.temporal_history, at_ts=timestamp)
    return policy, features


def _negative_prediction(engine, agent, state_map, predicted, desired, user_id, reason):
    """Punish a wrong displayed/predicted action when no pending command exists."""
    if predicted is None or _same(agent, predicted, desired):
        return False
    timestamp = time.time()
    policy, features = _policy_features(engine, agent, state_map, timestamp)
    idx = min(range(len(policy.actions)), key=lambda i: abs(float(policy.actions[i]) - float(predicted)))
    for horizon in policy.horizons:
        policy.update(horizon, idx, features, -1.0)
    engine.models[agent["id"]] = policy
    from storage import STORE
    STORE.save_model(agent["id"], policy.serialize())
    STORE.add_feedback(agent["id"], idx, policy.actions[idx], -1.0, reason, features, user_id)
    rt = engine.runtime.setdefault(agent["id"], {})
    rt["last_reward"] = -1.0
    rt["last_reward_reason"] = reason
    STORE.event(agent["id"], "warning", "manual_prediction_rejected",
                f"Manual correction rejected predicted value {predicted}",
                {"predicted": predicted, "desired": desired, "user_id": user_id})
    return True


def _positive_demonstration(engine, agent, state_map, desired, user_id, reason):
    timestamp = time.time()
    policy, features = _policy_features(engine, agent, state_map, timestamp)
    idx = min(range(len(policy.actions)), key=lambda i: abs(float(policy.actions[i]) - float(desired)))
    for horizon in policy.horizons:
        policy.update(horizon, idx, features, 1.0)
    from storage import STORE
    STORE.save_model(agent["id"], policy.serialize())
    STORE.add_feedback(agent["id"], idx, policy.actions[idx], 1.0, reason, features, user_id)
    rt = engine.runtime.setdefault(agent["id"], {})
    rt["last_reward"] = 1.0
    rt["last_reward_reason"] = reason
    return idx


def apply_ui_correction(core, agent, desired_value=None, keep_current=False):
    """Perform and learn one explicit user correction.

    ``keep_current=True`` means "Desired is wrong; the state I see now is correct".  It
    performs no HA service call.  Otherwise binary targets are one-tap toggles and
    continuous/categorical targets use the explicit value supplied by the UI.
    """
    from context import target_call, target_value
    from manual_context_learning import observe as observe_manual_context

    engine = core.ENGINE
    store = core.STORE
    if engine is None or store is None:
        raise RuntimeError("runtime unavailable")

    with engine.executor.target_lock(agent["target_entity"]):
        with engine.lock:
            state_map = dict(engine.state_map)
        state = state_map.get(agent["target_entity"])
        current = target_value(state, agent["target_property"])
        if current is None or not math.isfinite(float(current)):
            raise ValueError("Target state/value unavailable")

        keep_current = bool(keep_current)
        if keep_current:
            desired = _manual_value(agent, state, current)
        else:
            if desired_value is None:
                if agent["target_property"] != "power":
                    raise ValueError("desired_value is required for non-binary targets")
                desired_value = 0.0 if float(current) >= 0.5 else 1.0
            desired = _manual_value(agent, state, desired_value)
            if _same(agent, current, desired):
                raise ValueError("The requested correction is already the current state")

        timestamp = time.time()
        rt = engine.runtime.setdefault(agent["id"], {})
        engine.experiments.cancel(agent["id"], "explicit user correction")
        pending = rt.get("pending")
        predicted = rt.get("last_prediction")
        rejected = pending.get("action_value") if pending and not _same(agent, pending.get("action_value"), desired) else predicted

        # Observe the broad context *before* updating the policy.  If repeated manual
        # evidence promotes a previously unselected ESPHome/phone/etc. entity, the +/-
        # update below immediately trains the newly inserted feature slot.
        context_learning = observe_manual_context(
            core, agent, state_map, desired, rejected=rejected,
            source="ui_keep_current" if keep_current else "ui_correction",
            user_id=UI_USER_ID,
        )

        training_state = str(agent.get("training_state") or "waiting")
        can_learn = training_state != "training"
        qualified = training_state == "qualified"
        negative_applied = False

        if qualified and pending and not _same(agent, pending.get("action_value"), desired):
            result = engine.executor.reward_engine.evaluate(
                manual_correction=True, chatter=bool(pending.get("chatter"))
            )
            rt["reward_components_pending"] = result.components
            engine._reward_pending(agent, rt, result.value, "manual correction (UI)", UI_USER_ID)
            negative_applied = True
        elif can_learn:
            negative_applied = _negative_prediction(
                engine, agent, state_map, predicted, desired,
                UI_USER_ID, "manual correction: rejected prediction (UI)"
            )

        positive_applied = False
        if can_learn:
            _positive_demonstration(
                engine, agent, state_map, desired, UI_USER_ID,
                "manual demonstration (UI: current correct)" if keep_current else "manual demonstration (UI)"
            )
            positive_applied = True

        service_name = None
        data = None
        if not keep_current:
            # A UI correction is a direct user action, not an autonomous policy action.
            # It bypasses Control qualification/confidence while retaining device limits.
            domain, service, data = target_call(agent["target_entity"], agent["target_property"], desired, state)
            service_name = f"{domain}.{service}"
            engine.record_command(agent, desired)
            started = time.time()
            try:
                response = engine.executor._service(domain, service, data)
                engine.record_command(agent, desired, response)
            except Exception as exc:
                rt.update(last_service_ts=started, last_service=service_name,
                          last_service_ok=False, last_service_error=f"{type(exc).__name__}: {exc}")
                store.event(agent["id"], "error", "manual_correction_service_failed", str(exc),
                            {"current": current, "desired": desired, "service": service_name})
                raise
            rt.update(
                last_service_ts=started,
                last_service=service_name,
                last_service_data=data,
                last_service_ok=True,
                last_service_error=None,
                last_service_latency_ms=(time.time() - started) * 1000.0,
            )

        rt.update(
            last_manual_correction_ts=timestamp,
            last_manual_correction_value=desired,
            last_manual_correction_source="ui_keep_current" if keep_current else "ui",
            last_change_origin="manual_ui",
            decision_state="manual",
            decision_reason=("User confirmed the current state and rejected a conflicting Desired value"
                             if keep_current else "User corrected the device state; manual priority is active"),
        )
        engine.set_manual_hold(agent, rt, timestamp)
        engine.wake_event.set()
        store.event(agent["id"], "info", "manual_correction_ui",
                    (f"User confirmed current value {current}" if keep_current else f"User correction {current} → {desired}"),
                    {"current": current, "desired": desired, "service": service_name,
                     "keep_current": keep_current, "negative_applied": negative_applied,
                     "positive_applied": positive_applied, "context_learning": context_learning})
        return {
            "ok": True,
            "current_value": current,
            "desired_value": desired,
            "service": service_name,
            "keep_current": keep_current,
            "negative_applied": negative_applied,
            "positive_applied": positive_applied,
            "training_state": training_state,
            "manual_hold_until": rt.get("manual_override_until"),
            "context_learning": context_learning,
        }


def _physical_manual_snapshot(engine, agent, state_map):
    """Detect the same physical manual edge the core runtime is about to learn."""
    from context import target_value
    from control import same_value
    aid = agent["id"]
    rt = engine.runtime.get(aid) or {}
    previous = rt.get("previous_target")
    if previous is None:
        return None
    state = state_map.get(agent["target_entity"])
    current = target_value(state, agent["target_property"])
    if current is None:
        return None
    changed = abs(float(current) - float(previous)) > max(.01, float(agent.get("deadband") or 0.0) * .05)
    if not changed:
        return None
    context = (state or {}).get("context") or {}
    user_id = context.get("user_id") if not context.get("parent_id") else None
    if not user_id:
        return None
    pending = rt.get("pending")
    own_echo = engine.own_command_echo(agent, state, current)
    expected_ack = bool(pending and same_value(float(current), float(pending["action_value"]), float(agent.get("deadband") or 0.0)))
    if own_echo or expected_ack:
        return None
    return {
        "desired": float(current),
        "predicted": rt.get("last_prediction"),
        "user_id": user_id,
        "had_pending": bool(pending),
        "pending_value": pending.get("action_value") if pending else None,
    }


def install_runtime_physical_equivalence(core, engine):
    """Make physical user changes observe full context and reject wrong Shadow output."""
    if getattr(engine, "_manual_feedback_equivalence_installed", False):
        return
    original = engine.process_agent

    def process_agent(agent, state_map, changed_entities=None):
        snapshot = _physical_manual_snapshot(engine, agent, state_map)
        if snapshot and str(agent.get("training_state") or "") != "training":
            from manual_context_learning import observe as observe_manual_context
            rejected = snapshot.get("pending_value") if snapshot.get("had_pending") else snapshot.get("predicted")
            observe_manual_context(
                core, agent, state_map, snapshot["desired"], rejected=rejected,
                source="physical_explicit_user", user_id=snapshot["user_id"],
            )
        result = original(agent, state_map, changed_entities)
        if snapshot and not snapshot["had_pending"] and agent.get("training_state") == "qualified":
            _negative_prediction(
                engine, agent, state_map, snapshot["predicted"], snapshot["desired"],
                snapshot["user_id"], "manual correction: rejected prediction (physical)"
            )
        return result

    engine.process_agent = process_agent
    engine._manual_feedback_equivalence_installed = True


def install(core):
    """Install HTTP endpoint and physical/manual learning equivalence."""
    if getattr(core.Handler, "_manual_feedback_installed", False):
        return
    original_post = core.Handler.do_POST
    original_initialize = core.initialize_runtime

    def initialize_runtime():
        original_initialize()
        if core.runtime_available():
            install_runtime_physical_equivalence(core, core.ENGINE)
            core.STORE.event(None, "info", "manual_feedback_ready",
                             "Manual correction feedback path ready", None)

    def do_post(self):
        path, _, _ = self.path.partition("?")
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "manual-correction":
            if not self.require_trusted_client():
                return
            if not self.require_runtime():
                return
            agent_id = parts[2]
            agent = core.STORE.get_agent_config(agent_id)
            if not agent:
                return self.send_json(404, {"error": "agent not found"})
            try:
                payload = self.read_json()
                payload = payload if isinstance(payload, dict) else {}
                desired = payload.get("desired_value")
                keep_current = bool(payload.get("keep_current", False))
                return self.send_json(200, apply_ui_correction(core, agent, desired, keep_current=keep_current))
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self.send_json(502, {"error": f"Manual correction failed: {type(exc).__name__}: {exc}"})
        return original_post(self)

    core.initialize_runtime = initialize_runtime
    core.Handler.do_POST = do_post
    core.Handler._manual_feedback_installed = True
