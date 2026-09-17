"""Keep explicit physical user corrections out of Live policy weights.

Engine.process_agent historically performed two immediate learning updates when it saw a
qualified explicit-user device change: a negative update for a rejected pending action and
a positive demonstration for the observed state.  Stage 06 keeps the immediate/manual-hold
semantics but routes learning through ManualFeedbackJournal -> Candidate instead.

This compatibility guard is installed outside the legacy engine method.  It suppresses
only those two manual-policy updates during the exact physical-correction branch; ordinary
outcome rewards before/after the branch remain untouched.  No Executor or HA transport is
changed.
"""
from __future__ import annotations

import time

from manual_feedback import _physical_manual_snapshot
from manual_feedback_unified import latest_feedback_since


class _PolicyProxy:
    def __init__(self, base, suppress):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_suppress", suppress)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def __setattr__(self, name, value):
        if name in {"_base", "_suppress"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._base, name, value)

    def update(self, *args, **kwargs):
        if self._suppress():
            return None
        return self._base.update(*args, **kwargs)


def install(core):
    engine = getattr(core, "ENGINE", None)
    store = getattr(core, "STORE", None)
    if engine is None or store is None:
        return
    if getattr(engine, "_manual_feedback_live_isolation_installed", False):
        return

    original_process = engine.process_agent

    def process_agent(agent, state_map, changed_entities=None):
        snapshot = _physical_manual_snapshot(engine, agent, state_map)
        if not snapshot or str(agent.get("training_state") or "") == "training":
            return original_process(agent, state_map, changed_entities)

        suppress = {"manual": False}
        started = time.time()
        original_policy = engine.policy
        original_physical = engine.teaching.physical_correction
        original_add_feedback = store.add_feedback

        def physical_correction(*args, **kwargs):
            result = original_physical(*args, **kwargs)
            # The legacy negative + positive policy updates happen after this callback.
            suppress["manual"] = True
            return result

        def policy(subject):
            base = original_policy(subject)
            if suppress["manual"] and str(subject.get("id")) == str(agent["id"]):
                return _PolicyProxy(base, lambda: suppress["manual"])
            return base

        def add_feedback(agent_id, action_index, action_value, reward, reason, features, user_id=None):
            reason_text = str(reason or "").lower()
            if (suppress["manual"] and str(agent_id) == str(agent["id"])
                    and ("manual correction" in reason_text or "manual demonstration" in reason_text)):
                return None
            return original_add_feedback(
                agent_id, action_index, action_value, reward, reason, features, user_id
            )

        engine.policy = policy
        engine.teaching.physical_correction = physical_correction
        store.add_feedback = add_feedback
        try:
            result = original_process(agent, state_map, changed_entities)
        finally:
            engine.policy = original_policy
            engine.teaching.physical_correction = original_physical
            store.add_feedback = original_add_feedback

        # The inner stage-06 wrapper already created the unified feedback fact and
        # Candidate label.  Correct its compatibility metadata: the guarded legacy updates
        # were intentionally suppressed, so Live weights did not change.
        journal = getattr(engine, "manual_feedback_journal", None)
        feedback = latest_feedback_since(
            journal, agent["id"], started - 0.01,
            sources={"physical_manual_change"},
        )
        if feedback is not None:
            journal.set_status(
                feedback["feedback_id"], feedback.get("application_status") or "applied",
                learning_effect={
                    "legacy_live_model_update": False,
                    "live_model_updated": False,
                },
            )
        return result

    engine.process_agent = process_agent
    engine._manual_feedback_live_isolation_installed = True
    store.event(
        None, "info", "manual_feedback_live_isolation_ready",
        "Physical manual corrections keep immediate priority but learn only through Candidate",
        {"live_model_update": False, "action_boundary": "unchanged_executor_only"},
    )
