"""Anchor fast agents to the currently working Home Assistant automation first.

For fast binary targets (lights/switches/input_booleans), an existing HA automation is
the best available description of the user's intended behaviour. The first learned
policy should therefore start from that automation's trigger/condition entities rather
than immediately mixing in every correlated whole-home sensor.

The shared Home Intelligence trajectory features remain part of every policy through the
normal feature tail, so room-to-room motion can still help prediction immediately. New
raw sensors stay outside the active schema initially and are free to enter Sensor
Tournament as challengers. They can later be promoted, including replacing the
automation-derived primary occupancy sensor, but only through the existing stricter
primary-sensor promotion gates and post-promotion probation.

Persisted policies are never rewritten in place. Existing agents need Rebuild to adopt
an automation-first schema safely because feature-slot weights must stay aligned.
"""
import math

from context import entity_capability_tags, is_fast_reactive_agent


def _finite_score(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _preferred_automation_infos(infos):
    """Use currently enabled controllers; after Control takeover fall back to known ones."""
    infos = [dict(x) for x in (infos or []) if isinstance(x, dict)]
    enabled = [x for x in infos if bool(x.get("enabled"))]
    return enabled if enabled else infos


def automation_baseline_entities(infos):
    out = set()
    for info in _preferred_automation_infos(infos):
        out.update(str(x) for x in (info.get("context_entities") or []) if x)
    return out


def _automation_diagnostics(infos):
    return [
        {
            "entity_id": str(info.get("entity_id") or ""),
            "name": str(info.get("name") or info.get("entity_id") or ""),
            "enabled": bool(info.get("enabled")),
            "context_count": len(info.get("context_entities") or []),
            "baseline_rules": list(info.get("baseline_rules") or []),
            "action_services": list(info.get("action_services") or []),
            "baseline_contract": info.get("baseline_contract") or "structural_prior_not_ground_truth",
        }
        for info in _preferred_automation_infos(infos)
        if info.get("entity_id")
    ]


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


def normalize_fast_primary(agent, policy, engine, automation_infos=None):
    """Make the target automation's occupancy input the primary replay anchor first."""
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
    baseline = automation_baseline_entities(automation_infos)
    selected_baseline = sorted(selected & baseline)

    automation_candidates = [eid for eid in selected_baseline if eid in occupancy]
    chosen = _best(automation_candidates, relevance)
    source = "automation" if chosen else None

    local_candidates = [
        eid for eid in (meta.get("primary_local_sensors") or [])
        if eid in selected and eid in occupancy
    ]
    local_single = meta.get("primary_local_sensor")
    if local_single in selected and local_single in occupancy:
        local_candidates.append(local_single)

    if chosen is None:
        chosen = _best(local_candidates, relevance)
        source = "local" if chosen else None

    old = meta.get("primary_occupancy_sensor")
    if chosen is None:
        chosen = old if old in selected and old in occupancy else None
        source = "existing" if chosen else "none"

    changed = bool(chosen and chosen != old)
    if changed:
        meta["primary_occupancy_previous"] = old
    if chosen:
        meta["primary_occupancy_sensor"] = chosen

    automation_rows = _automation_diagnostics(automation_infos)
    if baseline:
        meta["automation_baseline_candidates"] = sorted(baseline)
        meta["automation_baseline_entities"] = selected_baseline
        meta["automation_baseline_automations"] = automation_rows
        meta["automation_baseline_current"] = any(row.get("enabled") for row in automation_rows)
        meta["automation_baseline_mode"] = "automation_first"

    meta["primary_occupancy_source"] = source
    meta["primary_occupancy_structural"] = source in ("automation", "local")

    return {
        "changed": changed,
        "previous": old,
        "primary": chosen,
        "source": source,
        "automation_candidates": sorted(set(automation_candidates)),
        "automation_baseline_entities": selected_baseline,
        "local_candidates": sorted(set(local_candidates)),
    }


def install(store, engine):
    """Install automation-first schema seeding and primary-anchor normalization."""
    if getattr(engine, "_fast_local_primary_installed", False):
        return False

    from ha import AUTOMATION_KNOWLEDGE
    import policy as policy_module

    if not getattr(policy_module, "_automation_baseline_selector_installed", False):
        original_select = policy_module.select_context_entities

        def select_with_automation_baseline(agent, state_map, registry, hint_entities,
                                            max_entities=None, relevance_scores=None):
            if not is_fast_reactive_agent(agent):
                return original_select(
                    agent, state_map, registry, hint_entities,
                    max_entities=max_entities, relevance_scores=relevance_scores,
                )
            requested = set(agent.get("input_entities") or ["*"])
            if "*" not in requested:
                return original_select(
                    agent, state_map, registry, hint_entities,
                    max_entities=max_entities, relevance_scores=relevance_scores,
                )

            _, infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
            baseline = automation_baseline_entities(infos)
            if not baseline:
                return original_select(
                    agent, state_map, registry, hint_entities,
                    max_entities=max_entities, relevance_scores=relevance_scores,
                )

            restricted = dict(agent)
            restricted["input_entities"] = sorted(baseline)
            selected, meta = original_select(
                restricted, state_map, registry, baseline,
                max_entities=max_entities, relevance_scores=relevance_scores,
            )
            if not selected:
                return original_select(
                    agent, state_map, registry, hint_entities,
                    max_entities=max_entities, relevance_scores=relevance_scores,
                )

            meta = dict(meta or {})
            rows = _automation_diagnostics(infos)
            meta["automation_baseline_candidates"] = sorted(baseline)
            meta["automation_baseline_entities"] = list(selected)
            meta["automation_baseline_automations"] = rows
            meta["automation_baseline_current"] = any(row.get("enabled") for row in rows)
            meta["automation_baseline_mode"] = "automation_first"
            meta["automation_baseline_extra_sensors"] = "sensor_tournament"
            return selected, meta

        policy_module.select_context_entities = select_with_automation_baseline
        policy_module._automation_baseline_selector_installed = True

    original_policy = engine.policy

    def policy_with_structural_primary(agent):
        policy = original_policy(agent)
        _, infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        result = normalize_fast_primary(agent, policy, engine, infos)
        if result.get("changed"):
            try:
                store.event(
                    agent.get("id"), "warning", "fast_primary_anchor_corrected",
                    "Fast agent primary occupancy anchor corrected",
                    {
                        "previous": result.get("previous"),
                        "primary": result.get("primary"),
                        "source": result.get("source"),
                        "automation_candidates": result.get("automation_candidates") or [],
                        "automation_baseline_entities": result.get("automation_baseline_entities") or [],
                        "local_candidates": result.get("local_candidates") or [],
                    },
                )
            except Exception:
                pass
        return policy

    engine.policy = policy_with_structural_primary
    engine._fast_local_primary_installed = True
    return True
