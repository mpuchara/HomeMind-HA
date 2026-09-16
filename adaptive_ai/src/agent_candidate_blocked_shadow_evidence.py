"""Collect passive Candidate A/B evidence without mutating visible offline-gate state.

0.14.2 let an offline-blocked Candidate collect future paired evidence by temporarily
rewriting its persisted edge/lifecycle state to ``comparing``. The Candidate UI polls the
same persisted state, so it could observe that short-lived rewrite and visibly alternate
between ``Offline gate blocked`` and ``A/B comparison``.

This hotfix keeps the persisted lifecycle authoritative and stable:

* blocked Candidates remain physically passive Shadow observers,
* the Shadow comparison runtime may read a blocked leaf for future paired evidence,
* comparison summaries are persisted without changing a blocked lifecycle state,
* standard Promote remains blocked by the unchanged offline gate,
* explicit custom Promote may still use the existing audited one-shot offline override.

The same corrective bundle also installs the binary Correct class-balancing layer before
observation hooks are finalized.  Error-only user labels are therefore balanced with
context-matched, correctly-predicted historical anchors instead of being interpreted as
the natural ON/OFF class distribution.

No ActionIntent or Executor ownership is added here.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import time

import agent_candidate_shadow_runtime as shadow_runtime
import agent_candidate_user_promotion as user_promotion
from agent_candidate_balanced_correct import install as install_balanced_correct


_BLOCKED_STATES = {"offline_blocked", "insufficient_evidence"}
_PATCHED = False
_ORIGINAL_TEMPORARY_GATE = None


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _blocked_state(row):
    row = dict(row or {})
    gate = _json(row.get("offline_gate_json"), {})
    state = str(row.get("state") or "")
    if state in _BLOCKED_STATES:
        return state
    if gate and not gate.get("passed"):
        return "insufficient_evidence" if str(gate.get("status") or "") == "insufficient_evidence" else "offline_blocked"
    return None


def _active_comparison_edge(manager, root_id):
    """Return the newest direct-parent edge that is eligible for passive comparison.

    Offline failure blocks promotion, not observation. Reading this edge does not mutate
    any Candidate state and therefore cannot leak a transient A/B lifecycle into the UI.
    """
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT e.*, child.generation_id AS child_generation_id,
                      child.parent_generation_id AS parent_generation_id,
                      parent.agent_id AS comparison_parent_agent_id
               FROM agent_candidates e
               JOIN agent_candidate_generations child ON child.agent_id=e.candidate_id
               JOIN agent_candidate_generations parent ON parent.generation_id=child.parent_generation_id
               WHERE child.root_agent_id=?
                 AND e.state IN ('comparing','ready','offline_blocked','insufficient_evidence')
                 AND child.lifecycle_state NOT IN ('discarded','pruned','promoted')
               ORDER BY child.generation_number DESC,child.created_ts DESC LIMIT 1""",
            (str(root_id),),
        ).fetchone()
    return dict(row) if row else None


def _persist_summary(manager, edge, summary):
    """Persist paired evidence while preserving a failed offline-gate lifecycle."""
    summary = dict(summary or {})
    summary["updated_ts"] = time.time()
    parent_gid = edge["parent_generation_id"]
    child_gid = edge["child_generation_id"]
    generation = shadow_runtime._generation(manager.store, generation_id=child_gid)
    if not generation:
        raise RuntimeError("Candidate generation disappeared while persisting comparison")
    root_id = str(generation["root_agent_id"])
    raw = json.dumps(summary, separators=(",", ":"))
    current_row = manager._candidate_row(edge["parent_agent_id"]) or edge
    derived = manager._comparison_summary({**current_row, "comparison_json": raw})
    blocked_state = _blocked_state(current_row)
    next_state = blocked_state or ("ready" if derived.get("promotable") else "comparing")

    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """INSERT INTO candidate_generation_comparisons
               (parent_generation_id,child_generation_id,root_agent_id,summary_json,updated_ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 summary_json=excluded.summary_json,updated_ts=excluded.updated_ts""",
            (parent_gid, child_gid, root_id, raw, time.time()),
        )
        c.execute(
            """UPDATE agent_candidates SET comparison_json=?,state=?,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=?""",
            (raw, next_state, time.time(), edge["parent_agent_id"], edge["candidate_id"]),
        )
        c.execute(
            """UPDATE agent_candidate_generations SET comparison_json=?,lifecycle_state=?,updated_ts=?
               WHERE generation_id=?""",
            (raw, next_state, time.time(), child_gid),
        )
    return derived


def install(manager):
    global _PATCHED, _ORIGINAL_TEMPORARY_GATE
    # Install learning semantics first.  This is idempotent and patches the module-level
    # Correct hooks that the already-decorated Candidate manager resolves at call time.
    manager = install_balanced_correct(manager)
    if getattr(manager, "_candidate_stable_blocked_shadow_installed", False):
        return manager

    if not _PATCHED:
        # The Shadow runtime resolves these module globals at call time, so replacing the
        # helpers here affects its already-installed closures without adding a second
        # inference/pairing implementation.
        shadow_runtime._active_comparison_edge = _active_comparison_edge
        shadow_runtime._persist_summary = _persist_summary

        _ORIGINAL_TEMPORARY_GATE = user_promotion._temporary_offline_gate_pass

        @contextmanager
        def stable_temporary_gate(manager_obj, row, *, purpose):
            if purpose == "observation":
                # Passive observation no longer needs to impersonate a passed offline
                # gate. The Shadow runtime explicitly accepts blocked leaves now.
                yield
                return
            with _ORIGINAL_TEMPORARY_GATE(manager_obj, row, purpose=purpose):
                yield

        user_promotion._temporary_offline_gate_pass = stable_temporary_gate
        _PATCHED = True

    manager._candidate_stable_blocked_shadow_installed = True
    manager.candidate_offline_gate_observation_contract = (
        "blocked_lifecycle_stays_persisted_while_passive_future_ab_evidence_collects"
    )
    return manager
