"""Make explicit/manual device feedback available throughout the agent lifecycle.

Qualified explicit HA-user changes are handled by Engine.process_agent plus the stage-06
physical-feedback wrapper.  This bridge covers waiting/paused/needs-retrain agents only
when HA supplies explicit user identity.  A physical state transition without ``user_id``
is provenance-ambiguous and must not silently become a user demonstration.

Stage 06 records the immediate user fact and broad context, then queues Candidate learning
when a compatible model exists.  It never updates Live policy weights in place.
"""
import math
import time

from manual_feedback_unified import observe_linked_context


def _trainable_now(agent):
    # Avoid changing feedback/training state while the historical worker owns the agent.
    return str(agent.get("training_state") or "waiting") != "training"


def _manual_origin(state):
    ctx = (state or {}).get("context") or {}
    if ctx.get("parent_id"):
        return None, None
    if ctx.get("user_id"):
        return "explicit_user", str(ctx.get("user_id"))
    # No user id is not evidence of manual intent. It may be a device, integration,
    # automation, restored state or command echo whose context mapping is unavailable.
    return None, None


def _record_without_policy(engine, agent, timestamp, before, after, source):
    """Persist a pre-model user fact without inventing a reusable context signature."""
    journal = getattr(engine, "manual_feedback_journal", None)
    if journal is None:
        return None
    row = journal.record(
        agent_id=agent["id"], selected_ts=timestamp, source=source,
        rejected_action=before, correct_action=after, error_kind="state",
        scope="similar_context", fingerprint=None,
        context_signature={
            "meta:signature_contract": 2.0,
            "meta:context_complete": 0.0,
            "meta:home_known": 0.0,
        },
        deadband=float(agent.get("deadband") or .01),
    )
    return journal.set_status(
        row["feedback_id"], "applied",
        immediate_effect={"physical_change": True, "runtime_override": False,
                          "manual_hold": True, "observed_external_user_action": True},
        learning_effect={"context_complete": False, "label_recorded": False,
                         "candidate_queued": False, "live_model_updated": False},
    )


def install_runtime(core):
    """Attach the explicit-user state bridge after adapters and before HA workers start."""
    if not core.runtime_available():
        return
    import manual_feedback as feedback
    from context import target_value

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
            # Qualified agents are handled exactly once by process_agent, where pending
            # decisions/provenance are available. This bridge only fills other lifecycle states.
            if agent.get("training_state") == "qualified":
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
            predicted = rt.get("last_prediction")
            timestamp = time.time()
            feedback_row = None
            if core.STORE.get_model(agent["id"]) is not None:
                try:
                    feedback_row = feedback._record_feedback(
                        engine, agent, state_map, timestamp,
                        source=origin, rejected_action=predicted,
                        correct_action=after, error_kind="state", scope="similar_context",
                    )
                except ValueError:
                    feedback_row = None
            if feedback_row is None:
                feedback_row = _record_without_policy(
                    engine, agent, timestamp, before, after, origin
                )

            journal = getattr(engine, "manual_feedback_journal", None)
            if feedback_row is not None and journal is not None:
                feedback_row = journal.set_status(
                    feedback_row["feedback_id"], "applied",
                    immediate_effect={"physical_change": True, "runtime_override": False,
                                      "manual_hold": True, "observed_external_user_action": True},
                    learning_effect={"live_model_updated": False},
                )

            context_learning = observe_linked_context(
                core, agent, state_map, after, rejected=predicted,
                source=origin, user_id=user_id,
                feedback_id=(feedback_row or {}).get("feedback_id"),
            )
            learning = None
            if core.STORE.get_model(agent["id"]) is not None and feedback_row is not None:
                learning = feedback._queue_candidate_label(
                    engine, agent, state_map, timestamp, desired=after, rejected=predicted,
                    source=origin, feedback_row=feedback_row,
                    error_kind="state", scope="similar_context",
                )
                if learning and learning.get("feedback"):
                    feedback_row = learning["feedback"]

            engine.set_manual_hold(agent, rt, timestamp)
            rt["last_change_origin"] = "manual_user"
            rt["last_manual_correction_source"] = origin
            core.STORE.event(
                agent["id"], "info", "manual_lifecycle_demo",
                f"Manual preference {before} → {after} recorded ({origin})",
                {
                    "before": before, "after": after, "user_id": user_id, "origin": origin,
                    "feedback_id": (feedback_row or {}).get("feedback_id"),
                    "context_learning": context_learning,
                    "candidate_learning": bool(learning),
                    "live_model_updated": False,
                    "training_state": agent.get("training_state"),
                },
            )

    engine.on_state_changed = on_state_changed
    engine._manual_lifecycle_events_installed = True
    core.STORE.event(
        None, "info", "manual_lifecycle_ready",
        "Explicit-user feedback is active across the lifecycle; learning is Candidate-only",
        {"live_model_updated": False, "unknown_user_changes": "not_feedback"},
    )


def install(core):
    """Compatibility hook for runtimes that initialize through a wrapper."""
    if getattr(core, "_manual_feedback_lifecycle_installed", False):
        return
    base_initialize = core.initialize_runtime

    def initialize_runtime():
        base_initialize()
        install_runtime(core)

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_lifecycle_installed = True
