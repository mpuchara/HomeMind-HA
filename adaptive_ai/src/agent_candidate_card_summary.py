"""Compact Candidate-card decision summary.

The Candidate card should show the same decision vocabulary as a Live agent: physical
Current, direct-parent Desired, Candidate Desired and Candidate model Confidence. The
Shadow runtime persists every retained generation under one event id, so the parent value
shown here is taken from the *same observed Shadow event* as the Candidate whenever
possible. No policy is replayed and no physical action is dispatched.
"""
from __future__ import annotations

import time

from context import target_value


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
        # A pre-upgrade database may lack a matching event row. Falling back to a fresh
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


def live_decision_snapshots(manager, *, now=None):
    """Cheap UI snapshot for Current / parent Desired / Candidate Desired.

    This intentionally avoids full Candidate status/comparison calculation. Current is read
    from the in-memory Home Assistant state map; Desired values are the latest actually
    observed Shadow event for the active lineage tip and its direct parent.
    """
    from agent_candidate_lineage import _active_tip

    now = time.time() if now is None else float(now)
    with manager.store.conn() as c:
        roots = [str(r[0]) for r in c.execute(
            """SELECT DISTINCT root_agent_id FROM agent_candidate_generations
               WHERE generation_type='candidate'
                 AND lifecycle_state NOT IN ('discarded','pruned','promoted')"""
        ).fetchall()]
    with manager.engine.lock:
        state_map = dict(manager.engine.state_map)

    output = []
    for root in roots:
        tip = _active_tip(manager.store, root)
        if not tip or not tip.get("agent_id") or not tip.get("parent_generation_id"):
            continue
        subject = manager.store.get_agent_config(str(tip["agent_id"])) or manager.store.get_agent_config(root)
        if not subject:
            continue
        current = target_value(state_map.get(subject["target_entity"]), subject["target_property"])
        try:
            current = None if current is None else float(current)
        except (TypeError, ValueError):
            current = None

        child = _latest_decision(manager.store, tip["generation_id"])
        fresh_child = bool(child and now - float(child.get("ts") or 0.0) <= DECISION_STALE_SECONDS)
        parent = None
        if fresh_child:
            parent = _decision_for_event(
                manager.store, tip["parent_generation_id"], child.get("event_id")
            )
            if parent is None:
                parent = _latest_decision(manager.store, tip["parent_generation_id"])
            if parent and now - float(parent.get("ts") or 0.0) > DECISION_STALE_SECONDS:
                parent = None

        output.append({
            "root_agent_id": root,
            "generation_id": str(tip["generation_id"]),
            "parent_generation_id": str(tip["parent_generation_id"]),
            "target_property": subject.get("target_property"),
            "current": current,
            "parent_desired": None if parent is None else parent.get("desired"),
            "candidate_desired": None if not fresh_child else child.get("desired"),
            "candidate_confidence": None if not fresh_child else child.get("confidence"),
            "shadow_timestamp": None if not fresh_child else child.get("ts"),
        })
    return output


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
    manager.live_decision_snapshots = lambda: live_decision_snapshots(manager)
    manager._candidate_card_summary_installed = True
    manager.candidate_card_decision_contract = (
        "realtime_physical_current_plus_same_observed_event_direct_parent_desired_plus_candidate_desired"
    )
    return manager
