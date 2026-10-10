"""Editable threshold references from the same sensor, only on Explore requests.

These are configuration suggestions, not training labels or learned thresholds.
No HA request, service call or inference-path work is performed here.
"""
import math

from additional_signal import normalize
from lighting_conditions import illumination_sensor


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 1000000 else None
    except (TypeError, ValueError):
        return None


def _unit(state):
    value = str((state.get("attributes") or {}).get("unit_of_measurement") or "raw").lower()
    return "lx" if value == "lux" else value


def _below_bounds(tree, states):
    """Only necessary direct-state darkness bounds; never flatten OR/NOT/templates."""
    if not isinstance(tree, dict):
        return []
    if tree.get("kind") == "and":
        return [row for child in tree.get("children") or [] for row in _below_bounds(child, states)]
    if tree.get("kind") != "numeric_state" or tree.get("attribute") or tree.get("below") is None:
        return []
    raw = tree["below"]
    helper = raw if isinstance(raw, str) and "." in raw and _number(raw) is None else None
    number = _number((states.get(helper) or {}).get("state") if helper else raw)
    if number is None:
        return []
    return [(eid, number, helper) for eid in tree.get("entities") or []
            if illumination_sensor(eid, states.get(eid))]


def suggestions(selected, agents, automations, states):
    """Rank saved choice, target automation, other Live goals, then other automations.

    Alternatives remain visible. Units must match the selected sensor exactly;
    another illuminance sensor or a changed lx/raw scale never supplies a default.
    """
    rows = {}

    def add(config, source, rank):
        eid = config["entity_id"]
        state = states.get(eid) or {}
        if not state or not illumination_sensor(eid, state) or _unit(state) != config.get("unit", "raw"):
            return
        rows.setdefault(eid, []).append({"config": config, "source": source, "rank": rank})

    for agent in [selected] + [a for a in agents if a.get("id") != selected.get("id")]:
        config = agent.get("additional_signal") or {}
        if config.get("purpose") != "avoid_bright_on":
            continue
        own = agent.get("id") == selected.get("id")
        if not own and (not agent.get("enabled") or agent.get("training_state") != "qualified"):
            continue
        try:
            config = normalize(config)
        except (ValueError, TypeError):
            continue
        add(config, {"kind": "selected_agent" if own else "agent", "id": agent.get("id"),
                     "name": agent.get("name") or agent.get("id"), "target": agent.get("target_entity")}, 0 if own else 2)

    seen = set()
    for info in automations:
        aid = info.get("entity_id")
        if not aid or aid in seen or not info.get("direct_on_conditions"):
            continue
        seen.add(aid)
        if not any(str(s).endswith(".turn_on") for s in info.get("action_services") or []):
            continue
        # AND may contain several upper limits on one sensor. Its necessary
        # darkness boundary is the smallest, not an arbitrary leaf.
        bounds = {}
        for eid, number, helper in _below_bounds(info.get("condition_tree"), states):
            if eid not in bounds or number < bounds[eid][0]:
                bounds[eid] = (number, helper)
        target = selected.get("target_entity") in (info.get("target_entities") or [])
        current = info.get("enabled") and info.get("config_status") not in ("cached", "unavailable")
        for eid, (number, helper) in bounds.items():
            try:
                config = normalize({"entity_id": eid, "purpose": "avoid_bright_on", "threshold": number,
                                    "hysteresis": 0, "max_age_seconds": 900, "unit": _unit(states.get(eid) or {})})
            except (ValueError, TypeError):
                continue
            add(config, {"kind": "automation", "id": aid, "name": info.get("name") or aid,
                         "bound_entity": helper, "enabled": bool(info.get("enabled")),
                         "config_status": info.get("config_status") or "fresh"},
                (1 if target else 3) if current else (4 if target else 5))

    out = {}
    for eid, values in rows.items():
        values.sort(key=lambda row: (row["rank"], str(row["source"]["id"]), row["config"]["threshold"]))
        alternatives = [{"config": row["config"], "source": row["source"]} for row in values]
        out[eid] = {**alternatives[0], "alternatives": alternatives,
                    "conflicting": len({(r["config"]["threshold"], r["config"]["hysteresis"]) for r in values}) > 1}
    return out


def for_manager(manager, selected):
    from ha import AUTOMATION_KNOWLEDGE
    from agent_candidates import _candidate_ids
    with manager.engine.lock:
        states = dict(manager.engine.state_map)
    with AUTOMATION_KNOWLEDGE.lock:
        automations = list(AUTOMATION_KNOWLEDGE.automations)
    hidden = _candidate_ids(manager.store)
    agents = [a for a in manager.store.list_agent_configs() if str(a.get("id")) not in hidden]
    return suggestions(selected, agents, automations, states)


def fill_missing_threshold(config, defaults):
    """Explicit values always win, including explicit invalid values (validated later)."""
    if (not isinstance(config, dict) or config.get("purpose", "context") != "avoid_bright_on"
            or "threshold" in config):
        return config, None
    reference = defaults.get(config.get("entity_id"))
    if not reference:
        raise ValueError("No brightness threshold reference for this sensor; enter an explicit threshold")
    inherited = reference["config"]
    return {**config, "threshold": inherited["threshold"],
            "unit": config.get("unit", inherited["unit"]),
            "hysteresis": config.get("hysteresis", inherited["hysteresis"]),
            "max_age_seconds": config.get("max_age_seconds", inherited["max_age_seconds"])}, reference["source"]
