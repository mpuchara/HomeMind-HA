"""Compact Candidate-card decision summary.

The Candidate card should show the same decision vocabulary as a Live agent: physical
Current, direct-parent Desired, Candidate Desired and Candidate model Confidence.  The
Shadow runtime persists every retained generation under one event id, so the parent value
shown here is taken from the *same observed Shadow event* as the Candidate whenever
possible.  No policy is replayed and no physical action is dispatched.
"""
from __future__ import annotations

import math
import time
from urllib.parse import urlsplit

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
    child_event_id = str(child.get("event_id") or "")
    parent_decision = _decision_for_event(
        store, parent_generation_id, child_event_id
    )
    if parent_decision is None and not child_event_id.startswith("candidate-passive:"):
        # A pre-upgrade database may lack a matching event row. Falling back to a fresh
        # observed parent row is acceptable only for legacy shared events. A passive
        # Candidate-only observation must keep Parent Desired unknown rather than pairing
        # it with an unrelated stale Parent prediction.
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


def live_candidate_snapshots(manager):
    """Fast UI-only snapshots without rebuilding Candidate metrics/status.

    The active Candidate Shadow runtime owns current decision tiles in RAM. SQLite is only
    a restart/backfill source inside that runtime, never the normal 1 s UI polling path.
    No policy replay, Executor call or learning mutation happens here.
    """
    hot = getattr(manager, "candidate_live_runtime_snapshots", None)
    if callable(hot):
        return list(hot() or [])
    now = time.time()
    with manager.store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT g.generation_id,g.parent_generation_id,g.root_agent_id,g.agent_id,
                      child.ts AS child_ts,child.event_id AS child_event_id,
                      child.desired AS child_desired,child.confidence AS child_confidence,
                      parent.desired AS parent_desired,parent.confidence AS parent_confidence
               FROM agent_candidate_generations g
               LEFT JOIN candidate_generation_decisions child
                 ON child.generation_id=g.generation_id
                AND child.ts=(SELECT MAX(d.ts) FROM candidate_generation_decisions d
                              WHERE d.generation_id=g.generation_id)
               LEFT JOIN candidate_generation_decisions parent
                 ON parent.generation_id=g.parent_generation_id
                AND parent.event_id=child.event_id
               WHERE g.generation_type='candidate' AND g.agent_id IS NOT NULL
                 AND g.lifecycle_state NOT IN ('discarded','pruned','promoted')
               ORDER BY g.root_agent_id,g.generation_number,g.created_ts"""
        ).fetchall()]
    with manager.engine.lock:
        state_map = dict(manager.engine.state_map)

    roots = {}
    snapshots = []
    for row in rows:
        root_id = str(row.get("root_agent_id") or "")
        if not root_id:
            continue
        root = roots.get(root_id)
        if root is None:
            root = manager.store.get_agent_config(root_id)
            roots[root_id] = root
        if not root:
            continue
        current = target_value(state_map.get(root.get("target_entity")), root.get("target_property"))
        try:
            current = None if current is None else float(current)
            if current is not None and not math.isfinite(current):
                current = None
        except (TypeError, ValueError):
            current = None
        child_ts = row.get("child_ts")
        fresh = child_ts is not None and now - float(child_ts) <= DECISION_STALE_SECONDS
        snapshots.append({
            "generation_id": row.get("generation_id"),
            "candidate_id": row.get("agent_id"),
            "root_agent_id": root_id,
            "target_property": root.get("target_property"),
            "shadow_current": current,
            "parent_desired": row.get("parent_desired") if fresh else None,
            "candidate_desired": row.get("child_desired") if fresh else None,
            "candidate_confidence": row.get("child_confidence") if fresh else None,
            "shadow_timestamp": float(child_ts) if fresh else None,
            "live_snapshot_ts": now,
        })
    return snapshots


def install(manager):
    if getattr(manager, "_candidate_card_summary_installed", False):
        return manager

    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)
    handler = manager.core.Handler
    original_get = handler.do_GET

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

    def do_get(http):
        path = urlsplit(http.path).path
        if path == "/api/candidate-live":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            return http.send_json(200, {"ts": time.time(), "candidates": live_candidate_snapshots(manager)})
        return original_get(http)

    manager.status = status
    manager.list_status = list_status
    manager.live_snapshots = lambda: live_candidate_snapshots(manager)
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    handler.do_GET = do_get
    manager._candidate_card_summary_installed = True
    manager.candidate_card_decision_contract = (
        "ram_first_current_plus_same_observed_event_direct_parent_desired_plus_candidate_desired"
    )
    return manager
