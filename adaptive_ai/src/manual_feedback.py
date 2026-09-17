"""Explicit user corrections for Adaptive AI.

Stage 06 separates two effects that older code mixed together:

* an immediate user effect (physical command/manual hold or contextual override), and
* learning, which is recorded in ManualFeedbackJournal and evolves an isolated Candidate.

The established ActionIntent/Executor boundary remains unchanged. A UI manual correction
is an explicit user command and may call the existing Executor service boundary; Shadow
and unpromoted Candidate paths never do. Bare negative feedback records only the rejected
action and does not invent an alternative action.
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
    """Validate an explicitly chosen user value without autonomous step guards."""
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
    """Legacy direct update retained for old core physical events only.

    New UI/Teaching/Teach-RL paths do not call this helper. Their learning goes to an
    isolated Candidate. The helper remains for backwards-compatible physical-event code
    until the core event learner itself is versioned away.
    """
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
    STORE.event(
        agent["id"], "warning", "manual_prediction_rejected",
        f"Manual correction rejected predicted value {predicted}",
        {"predicted": predicted, "desired": desired, "user_id": user_id},
    )
    return True


def _positive_demonstration(engine, agent, state_map, desired, user_id, reason):
    """Legacy direct update retained for compatibility with pre-stage-06 callers."""
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


def _context_signature(engine, agent, state_map, timestamp):
    from teaching import fingerprint, signature
    policy = engine.policy(agent)
    sig = signature(policy, state_map, engine.temporal_history, timestamp)
    return policy, fingerprint(agent), sig


def _record_feedback(engine, agent, state_map, timestamp, *, source, rejected_action=None,
                     correct_action=None, error_kind="state", scope="similar_context",
                     decision_id=None, episode_id=None, generation_id=None, feedback_id=None):
    journal = getattr(engine, "manual_feedback_journal", None)
    if journal is None:
        return None
    policy, stamp, sig = _context_signature(engine, agent, state_map, timestamp)
    if not sig:
        raise ValueError("Incomplete context for manual feedback")
    return journal.record(
        agent_id=agent["id"], selected_ts=timestamp, source=source,
        rejected_action=rejected_action, correct_action=correct_action,
        error_kind=error_kind, scope=scope, decision_id=decision_id,
        episode_id=episode_id, generation_id=generation_id,
        fingerprint=stamp, context_signature=sig,
        feature_schema_version=getattr(policy.schema, "VERSION", None),
        policy_version=getattr(policy, "VERSION", None),
        deadband=float(agent.get("deadband") or .01), feedback_id=feedback_id,
    )


def _queue_candidate_label(engine, agent, state_map, timestamp, *, desired, rejected,
                           source, feedback_row, error_kind="state", scope="similar_context"):
    journal = getattr(engine, "manual_feedback_journal", None)
    if feedback_row is None:
        return None
    if feedback_row.get("application_status") == "conflict":
        return None
    if desired is None:
        if journal is not None:
            journal.set_status(
                feedback_row["feedback_id"], "applied",
                learning_effect={
                    "negative_only": True, "label_recorded": False,
                    "candidate_queued": False,
                },
            )
        return None

    service = getattr(engine, "rl_teaching", None)
    if service is None or not hasattr(service, "add_feedback_label"):
        if journal is not None:
            journal.set_status(
                feedback_row["feedback_id"], "recorded",
                learning_effect={"rebuild_required": True, "candidate_queued": False},
            )
        return None
    result = service.add_feedback_label(
        agent, desired, timestamp, previous_desired=rejected, source=source,
        error_kind=error_kind, scope=scope, states=state_map,
        temporal=engine.temporal_history, feedback_id=feedback_row["feedback_id"],
    )
    if result.get("conflict"):
        return result
    manager = getattr(engine, "agent_candidates", None)
    candidate = manager.enqueue(agent["id"], "manual_feedback") if manager is not None else None
    if journal is not None:
        row = journal.set_status(
            feedback_row["feedback_id"], "learning_queued" if candidate else "applied",
            learning_effect={
                "label_recorded": True, "candidate_queued": bool(candidate),
                "candidate_generation_id": (candidate or {}).get("generation_id") if isinstance(candidate, dict) else None,
                "rebuild_required": True,
            },
        )
        result["feedback"] = row
        result["ui_message"] = journal.ui_summary(row)
    return result


def apply_ui_correction(core, agent, desired_value=None, keep_current=False, *,
                        error_kind="state", scope="similar_context", decision_id=None,
                        episode_id=None, feedback_id=None):
    """Apply an explicit user action now; learn it only through a Candidate."""
    from context import target_call, target_value

    engine, store = core.ENGINE, core.STORE
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
        rejected = (
            pending.get("action_value")
            if pending and not _same(agent, pending.get("action_value"), desired)
            else predicted
        )
        feedback = _record_feedback(
            engine, agent, state_map, timestamp,
            source="ui_keep_current" if keep_current else "ui_manual_correction",
            rejected_action=rejected, correct_action=desired, error_kind=error_kind,
            scope=scope, decision_id=decision_id or (pending or {}).get("decision_id"),
            episode_id=episode_id or (pending or {}).get("episode_id"),
            feedback_id=feedback_id,
        )
        if pending:
            # The explicit user correction owns this outcome. Do not let a later settling
            # timer reinterpret it as weak acceptance of the rejected autonomous action.
            rt["pending"] = None

        service_name = None
        data = None
        if not keep_current:
            domain, service, data = target_call(
                agent["target_entity"], agent["target_property"], desired, state
            )
            service_name = f"{domain}.{service}"
            origin_scope = getattr(engine, "provenance_command_origin", None)
            context = origin_scope("user_intent") if callable(origin_scope) else None
            if context is not None:
                context.__enter__()
            try:
                engine.record_command(agent, desired)
                started = time.time()
                try:
                    response = engine.executor._service(domain, service, data)
                    engine.record_command(agent, desired, response)
                except Exception as exc:
                    rt.update(
                        last_service_ts=started, last_service=service_name,
                        last_service_ok=False, last_service_error=f"{type(exc).__name__}: {exc}",
                    )
                    if feedback and getattr(engine, "manual_feedback_journal", None):
                        engine.manual_feedback_journal.set_status(
                            feedback["feedback_id"], "failed",
                            immediate_effect={"physical_change": False, "error": str(exc)},
                        )
                    store.event(
                        agent["id"], "error", "manual_correction_service_failed", str(exc),
                        {"current": current, "desired": desired, "service": service_name},
                    )
                    raise
                rt.update(
                    last_service_ts=started, last_service=service_name,
                    last_service_data=data, last_service_ok=True, last_service_error=None,
                    last_service_latency_ms=(time.time() - started) * 1000.0,
                )
            finally:
                if context is not None:
                    context.__exit__(None, None, None)

        rt.update(
            last_manual_correction_ts=timestamp,
            last_manual_correction_value=desired,
            last_manual_correction_source="ui_keep_current" if keep_current else "ui",
            last_change_origin="manual_ui", decision_state="manual",
            decision_reason=(
                "User confirmed the current state; Candidate learning is queued separately"
                if keep_current else
                "User corrected the device state; manual priority is active and Candidate learning is separate"
            ),
        )
        engine.set_manual_hold(agent, rt, timestamp)
        engine.wake_event.set()

        journal = getattr(engine, "manual_feedback_journal", None)
        if feedback and journal is not None:
            feedback = journal.set_status(
                feedback["feedback_id"], "applied",
                immediate_effect={
                    "physical_change": not keep_current, "runtime_override": False,
                    "manual_hold": True, "service": service_name,
                },
            )
        learning = _queue_candidate_label(
            engine, agent, state_map, timestamp, desired=desired, rejected=rejected,
            source="ui_keep_current" if keep_current else "ui_manual_correction",
            feedback_row=feedback, error_kind=error_kind, scope=scope,
        )
        if learning and learning.get("feedback"):
            feedback = learning["feedback"]

        store.event(
            agent["id"], "info", "manual_correction_ui",
            f"User correction {current} → {desired}" if not keep_current else f"User confirmed current value {current}",
            {
                "current": current, "desired": desired, "service": service_name,
                "keep_current": keep_current, "feedback_id": (feedback or {}).get("feedback_id"),
                "learning_path": "candidate", "live_model_updated": False,
            },
        )
        return {
            "ok": True, "current_value": current, "desired_value": desired,
            "service": service_name, "keep_current": keep_current,
            "negative_applied": False, "positive_applied": False,
            "learning_path": "candidate", "live_model_updated": False,
            "manual_hold_until": rt.get("manual_override_until"),
            "feedback_id": (feedback or {}).get("feedback_id"), "feedback": feedback,
            "ui_message": journal.ui_summary(feedback) if journal and feedback else "Correction applied",
        }


def teach_desired(core, agent, desired_value=None, *, error_kind="state",
                  scope="similar_context", decision_id=None, episode_id=None):
    """Contextual Teaching: no HA service, immediate override + Candidate learning."""
    engine, store = core.ENGINE, core.STORE
    if engine is None or store is None:
        raise RuntimeError("runtime unavailable")
    result = engine.teaching.teach(
        engine, agent, desired=desired_value, sample_ts=None,
        source="teach_desired", error_kind=error_kind, scope=scope,
        decision_id=decision_id, episode_id=episode_id,
    )
    result["service"] = None
    result["learning_path"] = "candidate"
    return result


def record_negative_feedback(core, agent, *, selected_ts=None, rejected_action=None,
                             error_kind="state", scope="episode", decision_id=None,
                             episode_id=None, feedback_id=None):
    """Record rejection only. No positive action is inferred and no device command runs."""
    engine = core.ENGINE
    if engine is None:
        raise RuntimeError("runtime unavailable")
    timestamp = float(selected_ts if selected_ts is not None else time.time())
    with engine.lock:
        state_map = dict(engine.state_map)
    rt = engine.runtime.setdefault(agent["id"], {})
    if rejected_action is None:
        rejected_action = rt.get("last_prediction")
    feedback = _record_feedback(
        engine, agent, state_map, timestamp, source="negative_rating",
        rejected_action=rejected_action, correct_action=None, error_kind=error_kind,
        scope=scope, decision_id=decision_id, episode_id=episode_id,
        feedback_id=feedback_id,
    )
    journal = getattr(engine, "manual_feedback_journal", None)
    if feedback and journal is not None:
        feedback = journal.set_status(
            feedback["feedback_id"], "applied",
            immediate_effect={"physical_change": False, "runtime_override": False, "manual_hold": False},
            learning_effect={"negative_only": True, "candidate_queued": False, "label_recorded": False},
        )
    return {
        "ok": True, "feedback_id": (feedback or {}).get("feedback_id"),
        "feedback": feedback, "correct_action": None,
        "learning_path": "journal_only",
        "ui_message": journal.ui_summary(feedback) if journal and feedback else "Feedback recorded",
    }


def _physical_manual_snapshot(engine, agent, state_map):
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
    expected_ack = bool(
        pending and same_value(float(current), float(pending["action_value"]), float(agent.get("deadband") or 0.0))
    )
    if own_echo or expected_ack:
        return None
    return {
        "desired": float(current), "predicted": rt.get("last_prediction"),
        "user_id": user_id, "had_pending": bool(pending),
        "pending_value": pending.get("action_value") if pending else None,
        "decision_id": pending.get("decision_id") if pending else None,
        "episode_id": pending.get("episode_id") if pending else None,
        "timestamp": time.time(),
    }


def install_runtime_physical_equivalence(core, engine):
    """Journal physical corrections and feed the same Candidate learning contract.

    Core 0.14.x still performs its legacy positive online update for a physical wall/device
    action. We preserve it for compatibility, record that fact explicitly, and also create
    the retractable Candidate label. Undo therefore requests a clean full rebuild and can
    remove the complete manual-label influence without inverse updates.
    """
    if getattr(engine, "_manual_feedback_equivalence_installed", False):
        return
    original = engine.process_agent

    def process_agent(agent, state_map, changed_entities=None):
        snapshot = _physical_manual_snapshot(engine, agent, state_map)
        feedback = None
        if snapshot and str(agent.get("training_state") or "") != "training":
            rejected = snapshot.get("pending_value") if snapshot.get("had_pending") else snapshot.get("predicted")
            try:
                feedback = _record_feedback(
                    engine, agent, state_map, snapshot["timestamp"],
                    source="physical_manual_change", rejected_action=rejected,
                    correct_action=snapshot["desired"], error_kind="state",
                    scope="similar_context", decision_id=snapshot.get("decision_id"),
                    episode_id=snapshot.get("episode_id"),
                )
            except ValueError:
                feedback = None
        result = original(agent, state_map, changed_entities)
        if snapshot and feedback:
            journal = getattr(engine, "manual_feedback_journal", None)
            if journal is not None:
                feedback = journal.set_status(
                    feedback["feedback_id"], "applied",
                    immediate_effect={"physical_change": True, "manual_hold": True, "observed_external_user_action": True},
                    learning_effect={"legacy_live_model_update": True, "rebuild_required": True},
                )
            rejected = snapshot.get("pending_value") if snapshot.get("had_pending") else snapshot.get("predicted")
            learning = _queue_candidate_label(
                engine, agent, state_map, snapshot["timestamp"], desired=snapshot["desired"],
                rejected=rejected, source="physical_manual_change", feedback_row=feedback,
                error_kind="state", scope="similar_context",
            )
            if journal is not None and learning and learning.get("feedback"):
                journal.set_status(
                    feedback["feedback_id"], "learning_queued",
                    learning_effect={"legacy_live_model_update": True, "rebuild_required": True},
                )
        return result

    engine.process_agent = process_agent
    engine._manual_feedback_equivalence_installed = True


def install(core, attach_runtime=True):
    """Install manual-feedback HTTP surface and physical/manual equivalence."""
    if getattr(core.Handler, "_manual_feedback_installed", False):
        return
    original_post = core.Handler.do_POST
    original_initialize = core.initialize_runtime

    def initialize_runtime():
        original_initialize()
        if core.runtime_available():
            install_runtime_physical_equivalence(core, core.ENGINE)
            core.STORE.event(None, "info", "manual_feedback_ready", "Manual correction feedback path ready", None)

    actions = {
        "manual-correction", "teach-desired", "teaching", "undo-teaching",
        "manual-feedback", "undo-feedback",
    }

    def do_post(self):
        path, _, _ = self.path.partition("?")
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] in actions:
            if not self.require_trusted_client() or not self.require_runtime():
                return
            agent_id, action = parts[2], parts[3]
            agent = core.STORE.get_agent_config(agent_id)
            if not agent:
                return self.send_json(404, {"error": "agent not found"})
            try:
                payload = self.read_json()
                payload = payload if isinstance(payload, dict) else {}
                desired = payload.get("desired_value")
                error_kind = payload.get("error_kind") or "state"
                scope = payload.get("scope") or "similar_context"
                if action == "teaching":
                    return self.send_json(200, core.ENGINE.teaching.teach(
                        core.ENGINE, agent, desired, payload.get("sample_ts"),
                        source="teaching", error_kind=error_kind, scope=scope,
                        decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                        generation_id=payload.get("generation_id"), feedback_id=payload.get("feedback_id"),
                    ))
                if action == "undo-teaching":
                    return self.send_json(200, core.ENGINE.teaching.undo(
                        core.ENGINE, agent, feedback_id=payload.get("feedback_id")
                    ))
                if action == "teach-desired":
                    return self.send_json(200, teach_desired(
                        core, agent, desired, error_kind=error_kind, scope=scope,
                        decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                    ))
                if action == "manual-feedback":
                    return self.send_json(200, record_negative_feedback(
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
                    return self.send_json(200, {
                        "ok": True, "feedback_id": feedback_id, "feedback": row,
                        "ui_message": journal.ui_summary(row),
                    })
                keep_current = bool(payload.get("keep_current", False))
                return self.send_json(200, apply_ui_correction(
                    core, agent, desired, keep_current=keep_current,
                    error_kind=error_kind, scope=scope,
                    decision_id=payload.get("decision_id"), episode_id=payload.get("episode_id"),
                    feedback_id=payload.get("feedback_id"),
                ))
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self.send_json(502, {"error": f"Manual correction failed: {type(exc).__name__}: {exc}"})
        return original_post(self)

    if attach_runtime:
        core.initialize_runtime = initialize_runtime
    core.Handler.do_POST = do_post
    core.Handler._manual_feedback_installed = True
