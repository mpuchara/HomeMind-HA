"""Teach-RL implementation backed by the stage-06 ManualFeedbackJournal.

The base RLTeaching service keeps deterministic history/rebuild behaviour. This subclass
changes only the label contract: every new label is first represented as unified feedback,
conflicts are retained as facts but not trained, and undo retires the linked label rather
than applying an inverse model update.
"""
from __future__ import annotations

import math
import time

from context import target_value
from manual_feedback import _manual_value
from teaching import fingerprint as contextual_fingerprint, signature as teaching_signature
from teaching_rl import RLTeaching, fingerprint as rl_fingerprint


class JournaledRLTeaching(RLTeaching):
    def __init__(self, store, engine):
        super().__init__(store, engine)
        self.feedback_journal = getattr(engine, "manual_feedback_journal", None)
        self.candidate_feedback_listener = None

    def labels(self, agent_id, include_undone=False):
        rows = super().labels(agent_id, include_undone=include_undone)
        journal = self.feedback_journal or getattr(self.engine, "manual_feedback_journal", None)
        if journal is not None and not include_undone:
            rows = journal.filter_linked_rows("teach_rl_label", rows)
        return rows

    def _insert_label(self, agent, desired, timestamp, previous, *, source,
                      generation_id=None, decision_id=None, episode_id=None,
                      error_kind="state", scope="similar_context", states=None,
                      temporal=None, feedback_id=None):
        timestamp = float(timestamp)
        if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > time.time() + 2:
            raise ValueError("Nieprawidłowy czas próbki")
        if agent.get("training_state") == "training":
            raise ValueError("Poczekaj na zakończenie treningu")

        if states is None or temporal is None:
            states, temporal, policy = self.engine.teaching.point_context(
                self.engine, agent, timestamp
            )
        else:
            policy = self.engine.policy(agent)
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        if current is None:
            raise ValueError("Brak stanu urządzenia w wybranej chwili")
        target_state = states.get(agent["target_entity"])
        if not target_state:
            raise ValueError("Brak stanu urządzenia w wybranej chwili")
        desired = _manual_value(agent, target_state, desired)
        sig = teaching_signature(policy, states, temporal, timestamp)
        if not sig:
            raise ValueError("Niepełny kontekst czujników w tej chwili")

        journal = self.feedback_journal or getattr(self.engine, "manual_feedback_journal", None)
        journal_row = None
        if journal is not None:
            journal_row = journal.record(
                agent_id=agent["id"], selected_ts=timestamp, source=source,
                rejected_action=previous, correct_action=desired, error_kind=error_kind,
                scope=scope, decision_id=decision_id, episode_id=episode_id,
                generation_id=generation_id, fingerprint=contextual_fingerprint(agent),
                context_signature=sig,
                feature_schema_version=getattr(policy.schema, "VERSION", None),
                policy_version=getattr(policy, "VERSION", None),
                deadband=float(agent.get("deadband") or .01), feedback_id=feedback_id,
            )
            if journal_row.get("application_status") == "conflict":
                return {
                    "ok": True, "label_id": None, "sample_ts": timestamp,
                    "desired_value": float(desired), "previous_desired": previous,
                    "feedback_id": journal_row["feedback_id"], "feedback": journal_row,
                    "conflict": True, "ui_message": journal.ui_summary(journal_row),
                }

        with self.store.lock, self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL",
                (agent["id"],),
            ).fetchone()[0]
            if count >= self.MAX_LABELS:
                raise ValueError("Limit 256 aktywnych punktów Teach")
            row = c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (agent["id"], time.time(), timestamp, float(desired), previous, rl_fingerprint(agent)),
            )
            label_id = int(row.lastrowid)

        if journal is not None and journal_row is not None:
            journal.link(
                journal_row["feedback_id"], "learning", "teach_rl_label", label_id,
                metadata={"source": source, "generation_id": generation_id},
            )
            journal_row = journal.set_status(
                journal_row["feedback_id"], "learning_queued",
                immediate_effect={"physical_change": False, "runtime_override": False},
                learning_effect={
                    "label_recorded": True, "teach_rl_label_id": label_id,
                    "candidate_queued": False, "rebuild_required": True,
                },
            )

        self.store.event(
            agent["id"], "info", "teach_rl_label_added",
            f"Teach RL label added: Desired {desired}",
            {
                "label_id": label_id, "sample_ts": timestamp, "desired": desired,
                "feedback_id": journal_row.get("feedback_id") if journal_row else None,
                "source": source,
            },
        )
        result = {
            "ok": True, "label_id": label_id, "sample_ts": timestamp,
            "desired_value": float(desired), "previous_desired": previous,
        }
        if journal_row is not None:
            result.update(
                feedback_id=journal_row["feedback_id"], feedback=journal_row,
                ui_message=journal.ui_summary(journal_row),
            )
        return result

    def add_label(self, agent, desired, sample_ts):
        timestamp = float(sample_ts)
        point = self.point(agent, timestamp)
        if point["current"] is None:
            raise ValueError("Brak stanu urządzenia w wybranej chwili")
        states, temporal, _ = self.engine.teaching.point_context(self.engine, agent, timestamp)
        result = self._insert_label(
            agent, desired, timestamp, point.get("desired"), source="teach_rl",
            states=states, temporal=temporal, scope="similar_context",
        )
        listener = self.candidate_feedback_listener
        if callable(listener) and result.get("label_id") is not None:
            listener("teach_rl_added", agent, result)
        return result

    def add_feedback_label(self, agent, desired, sample_ts, *, previous_desired=None,
                           source="correct", generation_id=None, decision_id=None,
                           episode_id=None, error_kind="state", scope="similar_context",
                           states=None, temporal=None, feedback_id=None):
        result = self._insert_label(
            agent, desired, sample_ts, previous_desired, source=source,
            generation_id=generation_id, decision_id=decision_id, episode_id=episode_id,
            error_kind=error_kind, scope=scope, states=states, temporal=temporal,
            feedback_id=feedback_id,
        )
        return result

    def undo(self, agent, feedback_id=None):
        journal = self.feedback_journal or getattr(self.engine, "manual_feedback_journal", None)
        linked_feedback = feedback_id
        label_id = None
        if linked_feedback is None and journal is not None:
            with self.store.conn() as c:
                row = c.execute(
                    """SELECT l.id,e.feedback_id FROM teaching_rl_labels l
                       LEFT JOIN manual_feedback_effects e
                         ON e.ref_type='teach_rl_label' AND e.ref_id=CAST(l.id AS TEXT)
                       WHERE l.agent_id=? AND l.undone_ts IS NULL ORDER BY l.id DESC LIMIT 1""",
                    (agent["id"],),
                ).fetchone()
            if row:
                label_id = int(row["id"])
                linked_feedback = row["feedback_id"]
        if journal is not None and linked_feedback:
            result = journal.undo(linked_feedback, engine=self.engine, candidate_manager=None)
            out = {
                "ok": True, "undone_id": label_id, "feedback_id": str(linked_feedback),
                "feedback": result, "full_rebuild_required": True,
                "ui_message": journal.ui_summary(result),
            }
            listener = self.candidate_feedback_listener
            if callable(listener):
                listener("teach_rl_undone", agent, out)
            return out

        result = super().undo(agent)
        result["full_rebuild_required"] = True
        listener = self.candidate_feedback_listener
        if callable(listener):
            listener("teach_rl_undone", agent, result)
        return result
