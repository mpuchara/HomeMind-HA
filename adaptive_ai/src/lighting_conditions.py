"""Tri-state illumination evidence from HA conditions, never a control permission.

Keep boolean structure and entity-valued bounds. A partial/unreadable condition
must not turn an occupied daylight OFF into an erroneous negative training label.
"""
import math
import re


def illumination_sensor(entity_id, state=None):
    if not str(entity_id).startswith("sensor."):
        return False
    attrs = (state or {}).get("attributes") or {}
    text = (str(entity_id) + " " + str(attrs.get("friendly_name") or "")).lower()
    return (attrs.get("device_class") == "illuminance"
            or str(attrs.get("unit_of_measurement") or "").lower() in ("lx", "lux")
            or bool(re.search(r"(?:^|[_. -])(?:light|illuminance|illuminance_value|lux|light_level|ambient_light)(?:$|[_. -])", text)))


def condition_tree(value, depth=0):
    """Small JSON-only tree; do not execute templates or flatten OR/NOT."""
    if depth > 12:
        return {"kind": "unknown"}
    if isinstance(value, list):
        if len(value) > 64:
            return {"kind": "unknown"}
        return {"kind": "and", "children": [condition_tree(v, depth + 1) for v in value]}
    if not isinstance(value, dict):
        return {"kind": "unknown"}
    kind = value.get("condition")
    if kind in ("and", "or", "not"):
        children = value.get("conditions")
        if not isinstance(children, list) or len(children) > 64:
            return {"kind": "unknown"}
        return {"kind": kind, "children": [condition_tree(v, depth + 1) for v in children]}
    if kind == "numeric_state" and not value.get("value_template"):
        entities = value.get("entity_id", [])
        entities = entities if isinstance(entities, list) else [entities]
        if len(entities) <= 64 and all(isinstance(e, str) for e in entities):
            return {"kind": kind, "entities": entities, "above": value.get("above"),
                    "below": value.get("below"), "attribute": value.get("attribute")}
    return {"kind": "unknown"}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _evaluate(tree, states):
    """Return (contains illumination, truth or unknown, sensor ids).

    Unrelated AND conditions do not assert lighting need; unrelated OR/NOT arms
    make it unknown because they may bypass the illumination restriction.
    """
    kind = tree.get("kind")
    if kind == "numeric_state":
        entities = tree.get("entities") or []
        light = [e for e in entities if illumination_sensor(e, states.get(e))]
        if not light:
            return False, None, []
        results = []
        for eid in light:
            st = states.get(eid) or {}
            value = _number((st.get("attributes") or {}).get(tree["attribute"])) if tree.get("attribute") else _number(st.get("state"))
            result = None if value is None else True
            bounded = False
            for key in ("above", "below"):
                bound = tree.get(key)
                if bound is None:
                    continue
                bounded = True
                if isinstance(bound, str) and "." in bound and _number(bound) is None:
                    bound = (states.get(bound) or {}).get("state")
                number = _number(bound)
                if number is None or value is None:
                    result = None
                    break
                if not (value > number if key == "above" else value < number):
                    result = False
            results.append(result if bounded else None)
        truth = False if False in results else None if None in results else True
        return True, truth, light
    children = [_evaluate(c, states) for c in tree.get("children") or []]
    relevant = [row for row in children if row[0]]
    if not relevant:
        return False, None, []
    ids = sorted({eid for row in relevant for eid in row[2]})
    if kind == "and":
        values = [row[1] for row in relevant]
        truth = False if False in values else None if None in values else True
    elif kind == "or":
        values = [row[1] if row[0] else None for row in children]
        truth = True if True in values else None if None in values else False
    elif kind == "not":
        def has_unrelated(node):
            if node.get("kind") == "numeric_state":
                return not any(illumination_sensor(e, states.get(e)) for e in node.get("entities") or [])
            if node.get("kind") not in ("and", "or", "not"):
                return True
            return any(has_unrelated(c) for c in node.get("children") or [])
        if any(has_unrelated(c) for c in tree.get("children") or []):
            return True, None, ids
        # HA NOT means none of its children is satisfied.
        values = [row[1] if row[0] else None for row in children]
        truth = False if True in values else None if None in values else True
    else:
        truth = None
    return True, truth, ids


def _automation_lighting_context(selection_meta, states):
    """ON illumination eligibility, independent of occupancy and agent predictions."""
    meta = selection_meta or {}
    infos = list(meta.get("automation_baseline_automations") or [])
    enabled = [info for info in infos if info.get("enabled")]
    infos = enabled or infos  # retained controllers after Control takeover
    on = [info for info in infos if any(str(s).endswith(".turn_on") for s in info.get("action_services") or [])]
    rows = []
    ids = set()
    configured = False
    selected = meta.get("automation_baseline_candidates") or meta.get("automation_baseline_entities") or []
    selected_light = any(illumination_sensor(e, states.get(e)) for e in selected)
    def has_unknown(tree):
        return tree.get("kind") == "unknown" or any(has_unknown(c) for c in tree.get("children") or [])
    for info in on:
        tree = info.get("condition_tree")
        if not isinstance(tree, dict) or not info.get("direct_on_conditions", False):
            rows.append(None)
            continue
        relevant, truth, sensors = _evaluate(tree, states)
        configured |= relevant
        ids.update(sensors)
        rows.append(truth if relevant else None if selected_light and has_unknown(tree) else True)
    if not configured:
        # Fresh ungated ON controllers preserve bathroom behavior. Legacy metadata
        # plus a light sensor cannot certify that occupied OFF was a mistake.
        uncertain = selected_light and (not rows or None in rows)
        return {"configured": uncertain, "need": None if uncertain else True,
                "sensors": [], "reason": "unresolved_conditions" if uncertain else "ungated"}
    need = True if True in rows else None if None in rows else False
    return {"configured": True, "need": need, "sensors": sorted(ids),
            "reason": "illumination_allowed" if need is True else "bright_enough" if need is False else "illumination_unknown"}


def lighting_context(selection_meta, states, at_ts=None, temporal=None):
    out = _automation_lighting_context(selection_meta, states)
    from additional_signal import evaluate
    evidence = evaluate(selection_meta, states, at_ts, temporal)
    if evidence.get("purpose") == "avoid_bright_on":
        out = {**out, "configured": True, "additional_signal": evidence,
               "sensors": sorted(set(out["sensors"]) | {evidence["entity_id"]})}
        if evidence.get("need") is not True:
            out.update(need=evidence["need"], reason=evidence["reason"])
    return out


def direct_on_conditions(actions):
    """Only unconditional literal top-level ON services, with no branch/toggle/OFF."""
    actions = actions if isinstance(actions, list) else [actions]
    return bool(actions) and all(isinstance(a, dict)
        and str(a.get("action", a.get("service", ""))).endswith(".turn_on")
        and not any(k in a for k in ("choose", "if", "repeat", "parallel")) for a in actions)
