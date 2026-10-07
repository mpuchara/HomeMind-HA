"""Audited fallback for unassigned automation radars, never occupancy labels."""
from fast_automation_replay import paired_numeric_baseline
from radar_context import radar_family, radar_role


def automation_radar_mapping(states, registry, mapping, by_target):
    claims, audits = {}, []
    for target, infos in sorted((by_target or {}).items()):
        area = mapping.get(target)
        if not area:
            continue
        infos = [dict(info) for info in infos if isinstance(info, dict)]
        enabled = [info for info in infos if info.get("enabled")]
        # Control takeover disables the original controllers. Their retained structure
        # is still useful; it does not become a presence label or a dispatch rule.
        selected = enabled or [dict(info, enabled=True) for info in infos]
        pair = paired_numeric_baseline({"automation_baseline_automations": selected})
        if not pair:
            continue
        anchor = pair["sensor"]
        if radar_role(anchor, states.get(anchor)) != "energy":
            continue
        if mapping.get(anchor) and mapping[anchor] != area:
            audits.append({"anchor": anchor, "target": target, "reason": "area_conflict"})
            continue
        if any(info.get("boundary_for") or info.get("arrival_precursor_for")
               for info in (registry.get(anchor, {}), (states.get(anchor) or {}).get("attributes") or {})):
            continue
        device = registry.get(anchor, {}).get("device_id")
        family = radar_family(anchor) if not device else None
        siblings = {anchor}
        for eid, state in states.items():
            reg = registry.get(eid, {})
            same_device = bool(device and reg.get("device_id") == device)
            same_family = bool(family and not reg.get("device_id") and radar_family(eid) == family)
            if radar_role(eid, state) and (same_device or same_family):
                siblings.add(eid)
        for eid in siblings:
            claims.setdefault(eid, {}).setdefault(area, []).append({"target": target, "anchor": anchor})
    inferred = {}
    for eid, areas in sorted(claims.items()):
        if len(areas) != 1:
            audits.append({"entity_id": eid, "reason": "ambiguous_target_areas", "areas": sorted(areas)})
        elif eid not in mapping:
            area, evidence = next(iter(areas.items()))
            inferred[eid] = {"area_id": area, "origin": "automation_radar_anchor", "evidence": evidence}
    return inferred, audits
