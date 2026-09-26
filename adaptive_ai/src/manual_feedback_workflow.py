"""Bind generation workflow corrections to ManualFeedbackJournal.

The generation workflow predates the stage-06 journal.  This adapter is intentionally
small and reversible: it wraps the already-tested public manager methods instead of
creating another learning subsystem.

Correct points become JournaledRLTeaching labels, Change decision exposes the Teaching
journal fact in its response, and undo removes the complete linked influence.  Candidate
creation remains owned by agent_workflow_actions and the parent model stays immutable.
"""
from __future__ import annotations

import time

from agent_workflow_actions import _resolve_generation
from manual_feedback import _manual_value
from manual_feedback_unified import latest_feedback_since
from teaching_rl import fingerprint as rl_fingerprint
from training_budget import TRAINING_BUDGET



def _linked_feedback_for_label(manager, label_id):
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT e.feedback_id
               FROM manual_feedback_effects e
               WHERE e.ref_type='teach_rl_label' AND e.ref_id=?
               ORDER BY e.updated_ts DESC LIMIT 1""",
            (str(label_id),),
        ).fetchone()
    return str(row["feedback_id"]) if row and row["feedback_id"] else None


def _latest_correct_label(manager, agent):
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT l.id,e.feedback_id,j.application_status
               FROM teaching_rl_labels l
               LEFT JOIN manual_feedback_effects e
                 ON e.ref_type='teach_rl_label' AND e.ref_id=CAST(l.id AS TEXT)
               LEFT JOIN manual_feedback_journal j ON j.feedback_id=e.feedback_id
               WHERE l.agent_id=? AND l.undone_ts IS NULL AND l.fingerprint=?
                 AND (j.application_status IS NULL OR j.application_status!='conflict')
               ORDER BY l.id DESC LIMIT 1""",
            (str(agent["id"]), rl_fingerprint(agent)),
        ).fetchone()
    return dict(row) if row else None


def _retire_same_correct_point(manager, agent, sample_ts):
    """Latest instruction wins for the same marked instant, as before stage 06."""
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT l.id,e.feedback_id
               FROM teaching_rl_labels l
               LEFT JOIN manual_feedback_effects e
                 ON e.ref_type='teach_rl_label' AND e.ref_id=CAST(l.id AS TEXT)
               WHERE l.agent_id=? AND l.undone_ts IS NULL AND l.fingerprint=?
                 AND ABS(l.sample_ts-?)<0.001
               ORDER BY l.id DESC LIMIT 1""",
            (str(agent["id"]), rl_fingerprint(agent), float(sample_ts)),
        ).fetchone()
    if not row:
        return None
    label_id = int(row["id"])
    feedback_id = row["feedback_id"]
    journal = getattr(manager.engine, "manual_feedback_journal", None)
    if journal is not None and feedback_id:
        # No inverse update.  If this old point had already reached a Candidate, the
        # journal records rebuild_pending; the next Correct commit coalesces fresh truth.
        journal.undo(str(feedback_id), engine=manager.engine, candidate_manager=None)
    else:
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                "UPDATE teaching_rl_labels SET undone_ts=? WHERE id=? AND undone_ts IS NULL",
                (time.time(), label_id),
            )
    return label_id


def _mark_correct_candidate(manager, generation, agent, result):
    journal = getattr(manager.engine, "manual_feedback_journal", None)
    child_generation_id = result.get("child_generation_id") if isinstance(result, dict) else None
    if journal is None or not child_generation_id:
        return []
    with manager.store.conn() as c:
        rows = c.execute(
            """SELECT DISTINCT j.feedback_id
               FROM teaching_rl_labels l
               JOIN manual_feedback_effects e
                 ON e.ref_type='teach_rl_label' AND e.ref_id=CAST(l.id AS TEXT)
               JOIN manual_feedback_journal j ON j.feedback_id=e.feedback_id
               WHERE l.agent_id=? AND l.undone_ts IS NULL AND l.fingerprint=?
                 AND j.undone_ts IS NULL AND j.application_status!='conflict'
                 AND j.source='correct'
                 AND (j.generation_id=? OR j.generation_id IS NULL)
               ORDER BY j.created_ts""",
            (str(agent["id"]), rl_fingerprint(agent), str(generation["generation_id"])),
        ).fetchall()
    feedback_ids = []
    for raw in rows:
        feedback_id = str(raw["feedback_id"])
        journal.link(
            feedback_id, "learning", "candidate_generation", child_generation_id,
            metadata={"source": "correct", "parent_generation_id": generation["generation_id"]},
        )
        journal.set_status(
            feedback_id, "learning_queued",
            learning_effect={
                "candidate_queued": True,
                "candidate_generation_id": child_generation_id,
                "candidate_reason": "correct",
                "rebuild_required": True,
            },
        )
        feedback_ids.append(feedback_id)
    return feedback_ids


def _link_change_context(manager, feedback_id, agent_id, started_ts):
    if not feedback_id:
        return None
    journal = getattr(manager.engine, "manual_feedback_journal", None)
    if journal is None:
        return None
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT id FROM manual_context_feedback
               WHERE agent_id=? AND created_ts>=? ORDER BY id DESC LIMIT 1""",
            (str(agent_id), float(started_ts) - 0.001),
        ).fetchone()
    if row:
        journal.link(
            feedback_id, "context", "manual_context_feedback", int(row["id"]),
            metadata={"source": "workflow_change_decision"},
        )
        return int(row["id"])
    return None


def install(manager):
    if getattr(manager, "_manual_feedback_workflow_installed", False):
        return manager
    required = (
        "workflow_add_correct_label", "workflow_undo_correct_label",
        "workflow_correct_commit", "workflow_change_decision", "workflow_correct_point",
    )
    if not all(callable(getattr(manager, name, None)) for name in required):
        manager.manual_feedback_workflow_contract = "pending_generation_workflow_install"
        return manager

    original_add = manager.workflow_add_correct_label
    original_undo = manager.workflow_undo_correct_label
    original_commit = manager.workflow_correct_commit
    original_change = manager.workflow_change_decision

    def add_correct_label(ref, desired, sample_ts):
        service = getattr(manager.engine, "rl_teaching", None)
        journal = getattr(manager.engine, "manual_feedback_journal", None)
        if journal is None or not callable(getattr(service, "add_feedback_label", None)):
            return original_add(ref, desired, sample_ts)

        generation, agent = _resolve_generation(manager, ref)
        point = manager.workflow_correct_point(ref, float(sample_ts))
        if point.get("desired") is None:
            raise ValueError("This generation has no observed prediction at that moment; a gap cannot be corrected")
        if point.get("current") is None or not point.get("context_complete"):
            raise ValueError("Historical context is incomplete at that moment")
        # This one context reconstruction is necessary for the durable feedback signature.
        # It is user-interactive work, so do not let a different agent's historical replay
        # consume the same Pi core while we reconstruct it.
        TRAINING_BUDGET.request_interactive_window(2.0, reason="correct_label")
        states, temporal, _ = manager.engine.teaching.point_context(
            manager.engine, agent, float(sample_ts)
        )
        target_state = states.get(agent["target_entity"])
        if not target_state:
            raise ValueError("Target state unavailable at the selected moment")
        normalized = _manual_value(agent, target_state, desired)
        _retire_same_correct_point(manager, agent, sample_ts)
        result = service.add_feedback_label(
            agent, normalized, float(sample_ts),
            previous_desired=float(point["desired"]), source="correct",
            generation_id=generation["generation_id"], error_kind="state",
            scope="similar_context", states=states, temporal=temporal,
        )
        result = dict(result or {})
        result["generation_id"] = generation["generation_id"]
        result["parent_model_unchanged"] = True
        return result

    def undo_correct_label(ref):
        generation, agent = _resolve_generation(manager, ref)
        row = _latest_correct_label(manager, agent)
        journal = getattr(manager.engine, "manual_feedback_journal", None)
        if not row or journal is None or not row.get("feedback_id"):
            return original_undo(ref)
        feedback_id = str(row["feedback_id"])
        feedback = journal.undo(
            feedback_id, engine=manager.engine, candidate_manager=manager,
        )
        return {
            "ok": True, "undone_id": int(row["id"]),
            "generation_id": generation["generation_id"],
            "feedback_id": feedback_id, "feedback": feedback,
            "ui_message": journal.ui_summary(feedback),
        }

    def correct_commit(ref, request_id=None):
        generation, agent = _resolve_generation(manager, ref)
        # The durable Correct queue owns request_id idempotency. This adapter must
        # preserve that public workflow signature when wrapping agent_workflow_actions.
        # Parent-model immutability is already enforced inside agent_workflow_actions
        # using the semantic model identity around child creation. Do not compare the
        # raw persisted JSON here: Store-owned bookkeeping fields (_history_watermark,
        # _benchmark_counts) may legitimately change without changing the policy, and
        # rolling those changes back would discard valid concurrent runtime bookkeeping.
        result = original_commit(ref, request_id=request_id)
        feedback_ids = _mark_correct_candidate(manager, generation, agent, result)
        if feedback_ids:
            result = dict(result)
            result["feedback_ids"] = feedback_ids
            result["live_model_updated"] = False
        return result

    def change_decision(ref, desired_value=None):
        generation, agent = _resolve_generation(manager, ref)
        journal = getattr(manager.engine, "manual_feedback_journal", None)
        previous_feedback = journal.latest(agent["id"], include_undone=True) if journal is not None else None
        started = time.time()
        # agent_workflow_actions performs the authoritative semantic parent-model guard.
        # This journal adapter must not add a second raw-JSON equality guard because
        # Store-owned bookkeeping is intentionally outside policy identity.
        result = original_change(ref, desired_value)

        feedback = latest_feedback_since(
            journal, agent["id"], started - 0.01,
            sources={"wrong_decision", "workflow_change_decision", "change_decision"},
        )
        if feedback and (previous_feedback or {}).get("feedback_id") == feedback.get("feedback_id"):
            feedback = None
        if feedback is not None:
            feedback_id = str(feedback["feedback_id"])
            child_generation_id = result.get("child_generation_id")
            if child_generation_id:
                journal.link(
                    feedback_id, "learning", "candidate_generation", child_generation_id,
                    metadata={"source": "change_decision",
                              "parent_generation_id": generation["generation_id"]},
                )
            context_id = _link_change_context(manager, feedback_id, agent["id"], started)
            feedback = journal.set_status(
                feedback_id, "learning_queued" if child_generation_id else "applied",
                immediate_effect={"runtime_override": True, "physical_change": False},
                learning_effect={
                    "candidate_queued": bool(child_generation_id),
                    "candidate_generation_id": child_generation_id,
                    "candidate_reason": "change_decision",
                    "manual_context_observation_id": context_id,
                    "rebuild_required": bool(child_generation_id),
                    "live_model_updated": False,
                },
            )
            result = dict(result)
            result.update(
                feedback_id=feedback_id, feedback=feedback,
                ui_message=journal.ui_summary(feedback), live_model_updated=False,
            )
        return result

    manager.workflow_add_correct_label = add_correct_label
    manager.workflow_undo_correct_label = undo_correct_label
    manager.workflow_correct_commit = correct_commit
    manager.workflow_change_decision = change_decision
    manager._manual_feedback_workflow_installed = True
    manager.manual_feedback_workflow_contract = (
        "correct_and_change_decision_share_journal_undo_rebuild_parent_immutable"
    )
    return manager
