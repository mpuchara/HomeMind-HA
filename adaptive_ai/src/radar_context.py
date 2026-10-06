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
