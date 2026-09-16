"""Compact Candidate-card decision summary.

The Candidate card should show the same decision vocabulary as a Live agent: physical
Current, direct-parent Desired, Candidate Desired and Candidate model Confidence.  The
Shadow runtime persists every retained generation under one event id, so the parent value
shown here is taken from the *same observed Shadow event* as the Candidate whenever
possible.  No policy is replayed and no physical action is dispatched.
"""
from __future__ import annotations

import time


DECISION_STALE_SECONDS = 95.0


def _generation(store, *, generation_id=None, agent_id=None):
    with store.conn() as c:
        if generation_id is not None:
            row = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE generation_id=?",
                (str(generation_id),),
            ).fetchone()
        elif agent_id is not None:
            row = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
                (str(agent_id),),
            ).fetchone()
        else:
            return None
    return dict(row) if row else None


def _latest_decision(store, generation_id):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_generation_decisions
               WHERE generation_id=? ORDER BY ts DESC LIMIT 1""",
            (str(generation_id),),
        ).fetchone()
    return dict(row) if row else None


def _decision_for_event(store, generation_id, event_id):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_generation_decisions
               WHERE generation_id=? AND event_id=? ORDER BY ts DESC LIMIT 1""",
            (str(generation_id), str(event_id)),
        ).fetchone()
    return dict(row) if row else None


def decorate_candidate_status(store, result, *, now=None):
    """Add direct-parent decision fields without changing Candidate learning semantics."""
    if not result:
        return result
    now = time.time() if now is None else float(now)

    candidate_id = result.get("candidate_id")
    candidate = store.get_agent_config(str(candidate_id)) if candidate_id else None
    parent_id = result.get("parent_agent_id")
    parent = store.get_agent_config(str(parent_id)) if parent_id else None
    subject = candidate or parent or {}
    result["target_property"] = subject.get("target_property")
    result["min_value"] = subject.get("min_value")
    result["max_value"] = subject.get("max_value")

    generation = None
    if result.get("generation_id"):
        generation = _generation(store, generation_id=result.get("generation_id"))
    if generation is None and candidate_id:
        generation = _generation(store, agent_id=candidate_id)
    if not generation or not generation.get("parent_generation_id"):
        result["parent_desired"] = None
        result["parent_confidence"] = None
        result["parent_shadow_timestamp"] = None
        return result

    child = _latest_decision(store, generation["generation_id"])
    if not child or now - float(child.get("ts") or 0.0) > DECISION_STALE_SECONDS:
        result["parent_desired"] = None
        result["parent_confidence"] = None
        result["parent_shadow_timestamp"] = None
        return result

    parent_generation_id = generation["parent_generation_id"]
    parent_decision = _decision_for_event(
        store, parent_generation_id, child.get("event_id")
    )
    if parent_decision is None:
        # A pre-upgrade database may lack a matching event row.  Falling back to a fresh
        # observed parent row is still better than replaying today's parent policy.
        parent_decision = _latest_decision(store, parent_generation_id)
    if (
        not parent_decision
        or now - float(parent_decision.get("ts") or 0.0) > DECISION_STALE_SECONDS
        or abs(float(parent_decision.get("ts") or 0.0) - float(child.get("ts") or 0.0))
        > DECISION_STALE_SECONDS
    ):
        result["parent_desired"] = None
        result["parent_confidence"] = None
        result["parent_shadow_timestamp"] = None
        return result

    result["parent_desired"] = parent_decision.get("desired")
    result["parent_confidence"] = parent_decision.get("confidence")
    result["parent_shadow_timestamp"] = parent_decision.get("ts")
    result["parent_generation_id"] = parent_generation_id
    return result


def install(manager):
    if getattr(manager, "_candidate_card_summary_installed", False):
        return manager

    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)

    def status(parent_id):
        return decorate_candidate_status(manager.store, original_status(parent_id))

    def list_status():
        return [
            decorate_candidate_status(manager.store, dict(item))
            for item in (original_list_status() or [])
            if item
        ]

    def lineage_status(ref):
        result = original_lineage_status(ref) if original_lineage_status is not None else None
        return decorate_candidate_status(manager.store, result)

    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    manager._candidate_card_summary_installed = True
    manager.candidate_card_decision_contract = (
        "current_plus_same_observed_event_direct_parent_desired_plus_candidate_desired"
    )
    return manager
