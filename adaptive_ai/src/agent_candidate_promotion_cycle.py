"""Candidate display lifecycle: stable names and per-promotion generation cycles.

Internal generation numbers stay monotonic because they are durable lineage/audit keys.
The user-facing Candidate number is relative to the currently active Live generation, so
once Gen N is promoted the next Candidate is displayed as Gen 1 again.

This layer also prevents hidden Candidate surrogate names from accumulating repeated
``Candidate`` suffixes when a Candidate creates the next Candidate generation.
"""
from __future__ import annotations

import re


_CANDIDATE_SUFFIX = re.compile(r"(?:\s*[·-]\s*Candidate)+\s*$", re.IGNORECASE)


def candidate_base_name(value):
    """Return a stable logical agent name without generated Candidate suffixes."""
    original = str(value or "").strip()
    name = original
    while name and _CANDIDATE_SUFFIX.search(name):
        reduced = _CANDIDATE_SUFFIX.sub("", name).strip()
        if reduced == name:
            break
        name = reduced
    return name or original or "Agent"


def _current_live_generation(store, root_id):
    """Absolute lineage generation currently active under the logical Root agent."""
    if not root_id:
        return 0
    try:
        with store.conn() as c:
            row = c.execute(
                """SELECT generation_number
                   FROM agent_candidate_generations
                   WHERE root_agent_id=? AND generation_type='live' AND agent_id=?
                     AND lifecycle_state='live'
                   ORDER BY updated_ts DESC,generation_number DESC
                   LIMIT 1""",
                (str(root_id), str(root_id)),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def candidate_cycle_number(store, result):
    """Translate monotonic lineage generation into the current Candidate-cycle number."""
    result = dict(result or {})
    try:
        absolute = int(result.get("generation_number") or result.get("generation") or 1)
    except (TypeError, ValueError):
        absolute = 1
    root_id = result.get("root_agent_id") or result.get("parent_agent_id")
    live_generation = _current_live_generation(store, root_id)
    return max(1, absolute - live_generation)


def _decorate(manager, result):
    if not isinstance(result, dict):
        return result
    out = dict(result)
    root_id = out.get("root_agent_id") or out.get("parent_agent_id")
    root = manager.store.get_agent_config(str(root_id)) if root_id else None
    base = candidate_base_name((root or {}).get("name") or out.get("parent_name") or root_id)
    out["parent_name"] = base
    out["candidate_name"] = f"{base} · Candidate"
    try:
        out["absolute_generation_number"] = int(
            out.get("generation_number") or out.get("generation") or 1
        )
    except (TypeError, ValueError):
        out["absolute_generation_number"] = 1
    out["candidate_generation_number"] = candidate_cycle_number(manager.store, out)
    return out


def install(manager):
    if getattr(manager, "_candidate_promotion_cycle_installed", False):
        return manager

    original_create = manager._create_candidate
    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)

    def create_candidate(parent):
        # Child Candidates may use another hidden Candidate as their direct parent. Keep
        # the exact model/config parent identity, but never inherit generated display text.
        clean_parent = dict(parent or {})
        clean_parent["name"] = candidate_base_name(
            clean_parent.get("name") or clean_parent.get("id")
        )
        return original_create(clean_parent)

    def status(parent_id):
        return _decorate(manager, original_status(parent_id))

    def list_status():
        return [_decorate(manager, item) for item in (original_list_status() or [])]

    def lineage_status(ref):
        return _decorate(manager, original_lineage_status(ref))

    manager._create_candidate = create_candidate
    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    manager._candidate_promotion_cycle_installed = True
    manager.candidate_display_contract = (
        "single_candidate_suffix_and_cycle_generation_relative_to_current_live"
    )
    return manager
