"""Semantic roles for LD24xx measurements; numeric signal is never occupancy."""
import re
from functools import lru_cache


def radar_role(entity_id, state=None):
    attrs = (state or {}).get("attributes") or {}
    return _radar_role(str(entity_id), str(attrs.get("friendly_name") or ""))


@lru_cache(maxsize=2048)
def _radar_role(entity_id, friendly_name):
    text = re.sub(r"[_\-]+", " ", entity_id + " " + friendly_name).lower()
    stationary = any(term in text for term in ("still", "static", "stationary"))
    moving = any(term in text for term in ("moving", "move", "motion"))
    radar = stationary or moving
    if entity_id.startswith("binary_sensor."):
        if stationary and "target" in text:
            return "still"
        if moving and "target" in text:
            return "moving"
        if "has target" in text or ("ld2410" in text and "presence" in text):
            return "presence"
    if entity_id.startswith("sensor.") and (radar or "detection distance" in text):
        if "energy" in text:
            if re.search(r"\b(?:g[0-8]|gate [0-8])\b", text):
                return "gate_energy"
            return "energy"
        if "distance" in text:
            return "distance"
    return None


def retain_observed_on_dwell(snapshot):
    """A radar motion timeout is not a verified exit. Use the observed ON label.

    Unknown signals preserve uncertainty; only complete persistent absence can
    shorten evidence. This function is for historical labels, never dispatch.
    """
    reliable = set(snapshot.get("reliable") or ())
    return bool(snapshot.get("radar")) and not (
        reliable and reliable <= set(snapshot.get("absent") or ())
        and not snapshot.get("active")
    )


def radar_context_entities(agent, baseline, states, registry, limit=8):
    """Bounded siblings of the automation radar, or radar channels in its room."""
    anchors = [eid for eid in baseline if radar_role(eid, states.get(eid))]
    devices = {(registry.get(eid) or {}).get("device_id") for eid in anchors} - {None, ""}
    # ESPHome REST/imported entities can lack a device/area registry entry.
    # Match the complete role suffix, never a loose common word such as "presence".
    def family(eid):
        name = eid.split(".", 1)[-1]
        match = re.match(r"^(.+)_(?:stationary|still|static|moving|move|motion)_(?:energy|target_distance|distance|target)$", name)
        return match.group(1) if match else None
    families = {family(eid) for eid in anchors if not (registry.get(eid) or {}).get("device_id")} - {None}
    areas = {(registry.get(eid) or {}).get("area_id") for eid in anchors} - {None, ""}
    if not areas:
        area = (registry.get(agent.get("target_entity")) or {}).get("area_id")
        if area:
            areas.add(area)
    order = {"presence": 0, "still": 1, "moving": 2, "energy": 3, "distance": 4, "gate_energy": 5}
    candidates = []
    for eid, state in states.items():
        role = radar_role(eid, state)
        reg = registry.get(eid) or {}
        local = reg.get("device_id") in devices if devices else reg.get("area_id") in areas
        if families and family(eid) in families and not reg.get("device_id"):
            # Explicitly conflicting areas must still win over the fallback.
            local = not reg.get("area_id") or not areas or reg.get("area_id") in areas
        if role and local and eid not in baseline:
            candidates.append((order[role], eid))
    return [eid for _, eid in sorted(candidates)[:limit]]
