"""Stage-06 unified manual-feedback helpers.

This module keeps one durable feedback fact across Teaching, Teach RL, Correct,
physical/manual changes and workflow corrections.  It deliberately does not own
physical execution or Candidate promotion.  Its responsibilities are narrower:

* unresolved contradictory instructions remain conflicts until one is undone;
* broad manual-context observations are linked to the same feedback fact;
* undo retires both explicit labels and the linked broad-context evidence;
* helpers expose the immediate-vs-learning split without mutating Live policy.
"""
from __future__ import annotations

import json

from manual_feedback_contract import (
    CONFLICT_DISTANCE,
    ManualFeedbackJournal,
    _signature_digest,
)


class UnifiedManualFeedbackJournal(ManualFeedbackJournal):
    """ManualFeedbackJournal with persistent unresolved-conflict semantics."""

    def _find_conflicts(self, *, agent_id, root_agent_id, episode_id, fingerprint,
                        signature, correct_action, deadband, scope):
        if correct_action is None or not signature:
            return []
        with self.store.conn() as c:
            rows = c.execute(
                """SELECT * FROM manual_feedback_journal
                   WHERE COALESCE(root_agent_id,agent_id)=? AND undone_ts IS NULL
                     AND correct_action IS NOT NULL
                     AND application_status IN
                         ('recorded','applied','learning_queued','rebuild_queued','conflict')
                   ORDER BY created_ts""",
                (str(root_agent_id or agent_id),),
            ).fetchall()
        try:
            from teaching import distance as context_distance
        except Exception:
            context_distance = None
        conflicts = []
        tolerance = max(0.01, float(deadband or 0.0))
        for raw in rows:
            row = dict(raw)
            if fingerprint and row.get("fingerprint") and str(row["fingerprint"]) != str(fingerprint):
                continue
            if not self._scope_can_conflict(scope, str(row.get("scope") or "similar_context")):
                continue
            if scope == "episode" or str(row.get("scope")) == "episode":
                if not episode_id or str(row.get("episode_id") or "") != str(episode_id):
                    continue
            try:
                previous_action = float(row["correct_action"])
            except (TypeError, ValueError):
                continue
            if abs(previous_action - float(correct_action)) <= tolerance:
                continue
            try:
                old_sig = json.loads(row.get("context_signature_json") or "{}")
            except Exception:
                continue
            if callable(context_distance):
                try:
                    dist = context_distance(signature, old_sig)
                except Exception:
                    dist = None
            else:
                dist = 0.0 if _signature_digest(signature) == row.get("context_digest") else None
            if dist is not None and float(dist) <= CONFLICT_DISTANCE:
                conflicts.append(str(row["feedback_id"]))
        return conflicts

    def set_status(self, feedback_id, status, *, immediate_effect=None, learning_effect=None,
                   undo_status=None):
        """A conflict cannot be accidentally reactivated by a downstream compatibility path."""
        current = self.get(feedback_id)
        requested = str(status)
        if current and str(current.get("application_status") or "") == "conflict":
            if requested not in {"conflict", "undone"}:
                requested = "conflict"
        return super().set_status(
            feedback_id, requested, immediate_effect=immediate_effect,
            learning_effect=learning_effect, undo_status=undo_status,
        )

    def _retire_linked_labels(self, feedback_id, timestamp):
        # Capture auxiliary effects before the base method marks every effect undone.
        with self.store.conn() as c:
            effects = [dict(row) for row in c.execute(
                "SELECT * FROM manual_feedback_effects WHERE feedback_id=?",
                (str(feedback_id),),
            ).fetchall()]
        retired = list(super()._retire_linked_labels(feedback_id, timestamp))

        context_ids = [
            int(effect["ref_id"])
            for effect in effects
            if str(effect.get("ref_type") or "") == "manual_context_feedback"
            and str(effect.get("ref_id") or "").isdigit()
        ]
        if context_ids:
            placeholders = ",".join("?" for _ in context_ids)
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    f"DELETE FROM manual_context_feedback WHERE id IN ({placeholders})",
                    tuple(context_ids),
                )
            try:
                import manual_context_learning as context_learning
                row = self.get(feedback_id) or {}
                aid = str(row.get("agent_id") or "")
                context_learning._SCORE_CACHE.pop(aid, None)
                context_learning._OBSERVATION_CACHE.pop(aid, None)
            except Exception:
                pass
            retired.extend(
                {"ref_type": "manual_context_feedback", "ref_id": str(row_id)}
                for row_id in context_ids
            )
        return retired


def observe_linked_context(core, agent, state_map, desired, rejected=None, *,
                           source="manual", user_id=None, feedback_id=None):
    """Persist broad context and bind the exact inserted row to one feedback fact.

    ``manual_context_learning.observe`` predates the journal and returns aggregate
    diagnostics rather than a row id.  Holding the Store RLock around the call makes the
    inserted-row lookup deterministic without changing that public API.
    """
    import manual_context_learning as context_learning

    if core is None or core.STORE is None or core.ENGINE is None:
        return {"recorded": False, "reason": "runtime unavailable"}
    store = core.STORE
    with store.lock:
        with store.conn() as c:
            before = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM manual_context_feedback WHERE agent_id=?",
                (str(agent["id"]),),
            ).fetchone()[0]
        result = context_learning.observe(
            core, agent, state_map, desired, rejected=rejected,
            source=source, user_id=user_id, refresh_policy=False,
        )
        observation_id = None
        if result.get("recorded"):
            with store.conn() as c:
                row = c.execute(
                    """SELECT id FROM manual_context_feedback
                       WHERE agent_id=? AND id>? ORDER BY id DESC LIMIT 1""",
                    (str(agent["id"]), int(before or 0)),
                ).fetchone()
            observation_id = int(row["id"]) if row else None

    result = dict(result or {})
    result["observation_id"] = observation_id
    journal = getattr(core.ENGINE, "manual_feedback_journal", None)
    if journal is not None and feedback_id and observation_id is not None:
        journal.link(
            feedback_id, "context", "manual_context_feedback", observation_id,
            metadata={"source": source},
        )
    return result


def latest_feedback_since(journal, agent_id, started_ts, *, sources=None):
    """Return a feedback fact created by this synchronous UI/workflow operation."""
    if journal is None:
        return None
    row = journal.latest(agent_id)
    if not row or float(row.get("created_ts") or 0.0) + 1e-6 < float(started_ts):
        return None
    if sources and str(row.get("source") or "") not in set(str(x) for x in sources):
        return None
    return row
