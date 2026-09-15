"""Keep fast-light replay anchored to the room's structural occupancy signal.

Fast agents can legitimately use remote/upstream context for anticipation, but the
``primary_occupancy_sensor`` has stronger semantics than an ordinary feature: history
replay uses its ON/OFF edges to anchor desired-state learning and to cut inherited dwell
periods.  Therefore a whole-home sensor with a slightly stronger historical correlation
must not displace a known local occupancy sensor as the primary anchor.

This extension does not alter the schema or the control path.  It only normalizes
``selection_meta`` on the already-selected policy:

1. selected local occupancy wins;
2. if HA area/device metadata is unavailable, a selected occupancy sensor explicitly
   referenced by an existing target automation is the structural fallback;
3. otherwise preserve the selector's existing primary occupancy choice.

Remote sensors remain in the active schema/challenger pool and may still contribute to
prediction; they simply cannot redefine the fast agent's room anchor when better
structural evidence exists.
"""
import math

from context import entity_capability_tags, is_fast_reactive_agent


def _finite_score(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _occupancy_entities(policy, engine):
    selected = set(getattr(getattr(policy, "schema", None), "entities", []) or [])
    with engine.lock:
        states = dict(getattr(engine, "state_map", {}) or {})
    return {
        eid for eid in selected
        if "occupancy" in entity_capability_tags(eid, states.get(eid) or {})
    }


def _best(candidates, relevance):
    values = sorted(
        {str(eid) for eid in candidates if eid},
        key=lambda eid: (-_finite_score((relevance or {}).get(eid, 0.0)), eid),
    )
    return values[0] if values else None


def normalize_fast_primary(agent, policy, engine):
    """Return diagnostics after enforcing the structural fast-agent primary anchor."""
    if not is_fast_reactive_agent(agent):
        return {"changed": False, "reason": "not_fast", "primary": None}

    meta = getattr(policy, "selection_meta", None)
    if not isinstance(meta, dict):
        return {"changed": False, "reason": "no_selection_meta", "primary": None}

    selected = set(getattr(getattr(policy, "schema", None), "entities", []) or [])
    if not selected:
        return {"changed": False, "reason": "empty_schema", "primary": None}

    occupancy = _occupancy_entities(policy, engine)
    aid = str(agent.get("id") or "")
    relevance = (getattr(engine, "context_relevance", {}) or {}).get(aid) or {}

    local_candidates = [
        eid for eid in (meta.get("primary_local_sensors") or [])
        if eid in selected and eid in occupancy
    ]
    local_single = meta.get("primary_local_sensor")
    if local_single in selected and local_single in occupancy:
        local_candidates.append(local_single)

    chosen = _best(local_candidates, relevance)
    source = "local" if chosen else None

    if chosen is None:
        reasons = meta.get("selection_reasons") or {}
        automation_candidates = []
        for eid in occupancy:
            why = reasons.get(eid) or []
            if isinstance(why, str):
                why = [why]
            if "automation" in why:
                automation_candidates.append(eid)
        chosen = _best(automation_candidates, relevance)
        source = "automation" if chosen else None

    old = meta.get("primary_occupancy_sensor")
    if chosen is None:
        # With no structural local/automation evidence, preserve the selector's current
        # choice rather than inventing a new semantic rule from correlation alone.
        chosen = old if old in selected and old in occupancy else None
        source = "existing" if chosen else "none"

    changed = bool(chosen and chosen != old)
    if changed:
        meta["primary_occupancy_previous"] = old
    if chosen:
        meta["primary_occupancy_sensor"] = chosen
    meta["primary_occupancy_source"] = source
    meta["primary_occupancy_structural"] = source in ("local", "automation")

    return {
        "changed": changed,
        "previous": old,
        "primary": chosen,
        "source": source,
        "local_candidates": sorted(set(local_candidates)),
    }


def install(store, engine):
    """Normalize policy metadata before Tournament/history consumers see the policy."""
    if getattr(engine, "_fast_local_primary_installed", False):
        return False

    original_policy = engine.policy

    def policy_with_structural_primary(agent):
        policy = original_policy(agent)
        result = normalize_fast_primary(agent, policy, engine)
        if result.get("changed"):
            try:
                store.event(
                    agent.get("id"), "warning", "fast_primary_anchor_corrected",
                    "Fast agent primary occupancy anchor corrected",
                    {
                        "previous": result.get("previous"),
                        "primary": result.get("primary"),
                        "source": result.get("source"),
                        "local_candidates": result.get("local_candidates") or [],
                    },
                )
            except Exception:
                pass
        return policy

    engine.policy = policy_with_structural_primary
    engine._fast_local_primary_installed = True
    return True
