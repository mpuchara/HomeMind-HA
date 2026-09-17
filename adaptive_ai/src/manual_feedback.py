"""Stage-06 public manual-feedback contract.

The pre-stage-06 implementation is retained byte-for-byte as ``manual_feedback_legacy``
for API/data compatibility.  This module re-exports that surface and owns the single
runtime equivalence wrapper used by current entrypoints.

When ``engine.manual_feedback_candidate_only`` is enabled, explicit physical user changes
keep their immediate/manual-hold semantics but the legacy negative/positive Live-policy
updates are suppressed.  Learning is recorded in ManualFeedbackJournal and routed to an
isolated Candidate.  Runtimes that do not opt into the stage-06 contract retain legacy
behaviour.
"""
from __future__ import annotations

import time

import manual_feedback_legacy as _legacy


# Preserve the complete historical module surface, including private helpers imported by
# Teaching and compatibility modules.  Implementations keep their original globals, so old
# callers are not silently reinterpreted.
UI_USER_ID = _legacy.UI_USER_ID
_same = _legacy._same
_manual_value = _legacy._manual_value
_policy_features = _legacy._policy_features
_negative_prediction = _legacy._negative_prediction
_positive_demonstration = _legacy._positive_demonstration
_context_signature = _legacy._context_signature
_record_feedback = _legacy._record_feedback
_feedback_context_complete = _legacy._feedback_context_complete
_queue_candidate_label = _legacy._queue_candidate_label
apply_ui_correction = _legacy.apply_ui_correction
_observe_teach_context = _legacy._observe_teach_context
teach_desired = _legacy.teach_desired
record_negative_feedback = _legacy.record_negative_feedback
_physical_manual_snapshot = _legacy._physical_manual_snapshot


class _PolicyProxy:
    """Delegate a policy while suppressing only the legacy manual update window."""

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


def install_runtime_physical_equivalence(core, engine):
    """Install the one physical/manual runtime bridge.

    This replaces the older two-wrapper composition.  The wrapper already existed to map
    explicit HA-user state changes into manual learning.  Stage 06 extends that same
    contract with a Candidate-only mode instead of wrapping ``process_agent`` a second
    time.
    """
    if getattr(engine, "_manual_feedback_equivalence_installed", False):
        return
    original = engine.process_agent

    def process_agent(agent, state_map, changed_entities=None):
        snapshot = _physical_manual_snapshot(engine, agent, state_map)
        journal = getattr(engine, "manual_feedback_journal", None)
        feedback = None
        qualified = str(agent.get("training_state") or "") != "training"

        if snapshot and qualified and journal is not None:
            rejected = (snapshot.get("pending_value") if snapshot.get("had_pending")
                        else snapshot.get("predicted"))
            try:
                feedback = _record_feedback(
                    engine, agent, state_map, snapshot["timestamp"],
                    source="physical_manual_change", rejected_action=rejected,
                    correct_action=snapshot["desired"], error_kind="state",
                    scope="similar_context", decision_id=snapshot.get("decision_id"),
                    episode_id=snapshot.get("episode_id"),
                )
            except ValueError:
                # The physical user action has already happened.  A malformed/ambiguous
                # learning fact must not prevent manual priority or normal processing.
                feedback = None

        candidate_only = bool(
            snapshot and qualified and getattr(engine, "manual_feedback_candidate_only", False)
        )

        # Engine.process_agent historically performs two direct policy writes after
        # Teaching.physical_correction: rejection of a pending AI action and a positive
        # manual demonstration.  In stage-06 mode suppress exactly that window, while
        # leaving ordinary outcome rewards before/after it untouched.
        if candidate_only:
            suppress = {"manual": False}
            original_policy = engine.policy
            original_physical = engine.teaching.physical_correction
            original_add_feedback = core.STORE.add_feedback

            def physical_correction(*args, **kwargs):
                result = original_physical(*args, **kwargs)
                suppress["manual"] = True
                return result

            def policy(subject):
                base = original_policy(subject)
                if suppress["manual"] and str(subject.get("id")) == str(agent["id"]):
                    return _PolicyProxy(base, lambda: suppress["manual"])
                return base

            def add_feedback(agent_id, action_index, action_value, reward, reason, features,
                             user_id=None):
                reason_text = str(reason or "").lower()
                if (suppress["manual"] and str(agent_id) == str(agent["id"])
                        and ("manual correction" in reason_text
                             or "manual demonstration" in reason_text)):
                    # The demonstration is the last direct manual write in the legacy
                    # branch; release suppression afterwards so later rewards are normal.
                    if "manual demonstration" in reason_text:
                        suppress["manual"] = False
                    return None
                return original_add_feedback(
                    agent_id, action_index, action_value, reward, reason, features, user_id
                )

            engine.policy = policy
            engine.teaching.physical_correction = physical_correction
            core.STORE.add_feedback = add_feedback
            try:
                result = original(agent, state_map, changed_entities)
            finally:
                engine.policy = original_policy
                engine.teaching.physical_correction = original_physical
                core.STORE.add_feedback = original_add_feedback
        else:
            result = original(agent, state_map, changed_entities)

        if snapshot and candidate_only:
            rt = engine.runtime.setdefault(agent["id"], {})
            # Do not leave a synthetic Live reward in diagnostics when its model write was
            # deliberately suppressed.  The durable journal is the source of truth.
            if str(rt.get("last_reward_reason") or "").lower().startswith("manual correction"):
                rt["last_reward"] = None
                rt["last_reward_reason"] = "manual correction queued to Candidate"

        if snapshot and feedback:
            journal = getattr(engine, "manual_feedback_journal", None)
            if journal is not None:
                feedback = journal.set_status(
                    feedback["feedback_id"], "applied",
                    immediate_effect={
                        "physical_change": True,
                        "manual_hold": True,
                        "observed_external_user_action": True,
                    },
                    learning_effect={
                        "legacy_live_model_update": not candidate_only,
                        "live_model_updated": not candidate_only,
                        "rebuild_required": True,
                    },
                )
            rejected = (snapshot.get("pending_value") if snapshot.get("had_pending")
                        else snapshot.get("predicted"))
            learning = _queue_candidate_label(
                engine, agent, state_map, snapshot["timestamp"], desired=snapshot["desired"],
                rejected=rejected, source="physical_manual_change", feedback_row=feedback,
                error_kind="state", scope="similar_context",
            )
            if journal is not None and learning and learning.get("feedback"):
                journal.set_status(
                    feedback["feedback_id"], "learning_queued",
                    learning_effect={
                        "legacy_live_model_update": not candidate_only,
                        "live_model_updated": not candidate_only,
                        "rebuild_required": True,
                    },
                )
        return result

    engine.process_agent = process_agent
    engine._manual_feedback_equivalence_installed = True


def install(core, attach_runtime=True):
    """Install the historical HTTP surface plus the stage-06 runtime contract."""
    _legacy.install(core, attach_runtime=False)
    if not attach_runtime or getattr(core, "_manual_feedback_facade_runtime_installed", False):
        return

    base_initialize = core.initialize_runtime

    def initialize_runtime():
        base_initialize()
        if core.runtime_available():
            install_runtime_physical_equivalence(core, core.ENGINE)
            core.STORE.event(
                None, "info", "manual_feedback_ready",
                "Manual correction feedback path ready",
                {"runtime_contract": "single_physical_equivalence_wrapper"},
            )

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_facade_runtime_installed = True
