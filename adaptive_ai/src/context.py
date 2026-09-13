from bisect import bisect_left
from bisect import bisect_right
from datetime import datetime
from collections import deque
import hashlib
import json
import math
import re
from settings import (OPTIONS, SENSOR_NEEDS, SUPPORTED_TARGETS, clamp, iso_from_ts, now_ts, parse_ts)

def target_value(state, property_name):
    if not state or str(state.get("state")).lower() in ("unavailable", "unknown"):
        return None
    attrs = state.get("attributes") or {}
    domain = state.get("entity_id", "").split(".", 1)[0]
    try:
        if property_name == "power" and domain in ("light", "switch", "input_boolean", "fan", "media_player"):
            low = str(state.get("state")).lower()
            return 0.0 if low in ("off", "unavailable", "unknown", "idle") else 1.0
        if domain == "light" and property_name == "brightness_pct":
            if str(state.get("state")).lower() == "off":
                return 0.0
            b = attrs.get("brightness")
            if b is None:
                return 0.0 if str(state.get("state")).lower() == "off" else None
            return float(b) * 100.0 / 255.0
        if domain == "climate" and property_name == "temperature":
            v = attrs.get("temperature")
            return None if v is None else float(v)
        if domain == "cover" and property_name == "position":
            v = attrs.get("current_position")
            return None if v is None else float(v)
        if domain == "fan" and property_name == "percentage":
            v = attrs.get("percentage")
            return None if v is None else float(v)
        if domain in ("number", "input_number") and property_name == "value":
            return float(state.get("state"))
        if domain == "media_player" and property_name == "volume_pct":
            v = attrs.get("volume_level")
            return None if v is None else float(v) * 100.0
        if domain == "humidifier" and property_name == "humidity":
            v = attrs.get("humidity")
            return None if v is None else float(v)
        if domain == "water_heater" and property_name == "temperature":
            v = attrs.get("temperature")
            return None if v is None else float(v)
        if domain in ("select", "input_select") and property_name == "option_index":
            opts = list(attrs.get("options") or [])
            try:
                return float(opts.index(str(state.get("state"))))
            except (ValueError, TypeError):
                return None
        v = attrs.get(property_name)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def target_call(entity_id, property_name, value, state=None):
    domain = entity_id.split(".", 1)[0]
    if property_name == "power" and domain in ("light", "switch", "input_boolean", "fan", "media_player"):
        return domain, "turn_on" if value >= 0.5 else "turn_off", {"entity_id": entity_id}
    if domain == "light" and property_name == "brightness_pct":
        # Brightness is the complete light policy for auto-discovered dimmable lights.
        # Treat the 0% arm as a real OFF command rather than turn_on(brightness=0),
        # which is interpreted inconsistently by different light integrations.
        if float(value) <= 0.5:
            return domain, "turn_off", {"entity_id": entity_id}
        return domain, "turn_on", {"entity_id": entity_id, "brightness_pct": int(round(value))}
    if domain == "climate" and property_name == "temperature":
        return domain, "set_temperature", {"entity_id": entity_id, "temperature": round(value, 2)}
    if domain == "cover" and property_name == "position":
        return domain, "set_cover_position", {"entity_id": entity_id, "position": int(round(value))}
    if domain == "fan" and property_name == "percentage":
        return domain, "set_percentage", {"entity_id": entity_id, "percentage": int(round(value))}
    if domain in ("number", "input_number") and property_name == "value":
        return domain, "set_value", {"entity_id": entity_id, "value": value}
    if domain == "media_player" and property_name == "volume_pct":
        return domain, "volume_set", {"entity_id": entity_id, "volume_level": clamp(value / 100.0, 0, 1)}
    if domain == "humidifier" and property_name == "humidity":
        return domain, "set_humidity", {"entity_id": entity_id, "humidity": int(round(value))}
    if domain == "water_heater" and property_name == "temperature":
        return domain, "set_temperature", {"entity_id": entity_id, "temperature": round(value, 2)}
    if domain in ("select", "input_select") and property_name == "option_index":
        options = list(((state or {}).get("attributes") or {}).get("options") or [])
        if not options:
            raise ValueError(f"No options available for {entity_id}")
        idx = int(clamp(round(value), 0, len(options) - 1))
        return domain, "select_option", {"entity_id": entity_id, "option": options[idx]}
    raise ValueError(f"Unsupported target {domain}.{property_name}")


def stable_hash(text):
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def numeric_scale(state, attr_name=None):
    attrs = state.get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    device_class = str(attrs.get("device_class") or "").lower()
    entity_id = state.get("entity_id", "").lower()
    token = " ".join((unit, device_class, entity_id, str(attr_name or "").lower()))
    if "temperature" in token or "°c" in token or "°f" in token:
        return 35.0
    if "%" in token or "humidity" in token or "battery" in token or "position" in token or "brightness" in token:
        return 100.0
    if "co2" in token or "ppm" in token:
        return 2000.0
    if "illuminance" in token or "lux" in token:
        return 1000.0
    if "power" in token or unit in ("w", "kw"):
        return 5000.0 if unit != "kw" else 5.0
    if "pressure" in token or "hpa" in token:
        return 1000.0
    if "energy" in token or "kwh" in token:
        return 100.0
    return 10.0



COMMON_STATE_VALUES = {
    "off": -1.0, "closed": -1.0, "not_home": -1.0, "clear": -1.0, "unoccupied": -1.0, "idle": -0.5, "standby": -0.5,
    "on": 1.0, "open": 1.0, "home": 1.0, "playing": 1.0, "detected": 1.0, "occupied": 1.0, "active": 1.0, "heat": 0.8, "cool": -0.8,
    "heating": 0.8, "cooling": -0.8, "dry": 0.4, "fan_only": 0.2, "auto": 0.1,
    "unavailable": 0.0, "unknown": 0.0,
}

CONTEXT_DOMAINS = {
    "sensor", "binary_sensor", "person", "device_tracker", "sun", "weather", "light", "switch",
    "climate", "cover", "fan", "media_player", "input_boolean", "input_number", "number", "select",
    "input_select", "humidifier", "water_heater", "vacuum", "alarm_control_panel", "lock",
}


def parse_horizons(agent=None):
    raw = str(OPTIONS.get("prediction_horizons_seconds", "1"))
    vals = []
    for part in re.split(r"[,; ]+", raw.strip()):
        if not part:
            continue
        try:
            v = int(float(part))
            if 1 <= v <= 3600:
                vals.append(v)
        except Exception:
            pass
    # v0.6 is intentionally reactive rather than long-horizon predictive.
    # The default uses one reactive head. The value 1 represents the product goal
    # (beat a typical manual action by about a second), not a t-1s extrapolation of
    # the world. A precursor event wakes inference immediately.
    vals = sorted(set(vals))[:4]
    return vals or [max(1, int(OPTIONS.get("prediction_lead_seconds", 1)))]


def state_scalar(state):
    """Convert one HA state into a bounded scalar without hashing entity identity.

    Discrete HA states are handled *before* numeric coercion. This matters for presence,
    motion and on/off entities: v0.5 accidentally converted ON/OFF to 1/0 and then scaled
    them as generic numbers, producing roughly +0.1/0.0. v0.6 gives categorical state
    transitions a strong centred representation such as ON=+1 and OFF=-1.
    """
    if state is None:
        return None
    text = str(state.get("state") or "").strip().lower()
    if text in COMMON_STATE_VALUES:
        return COMMON_STATE_VALUES[text]
    raw = parse_state_value(state)
    if raw is None:
        return None
    if is_number(raw):
        return math.tanh(float(raw) / max(numeric_scale(state), 1e-6))
    text = str(raw).strip().lower()
    # Stable category embedding in [-0.85, 0.85]. It is local to this entity's slot,
    # therefore collisions between different HA entities are impossible.
    h = stable_hash(text)
    return ((h % 2001) / 1000.0 - 1.0) * 0.85



ELECTRICAL_UNITS = {
    # Voltage
    "v", "mv", "kv", "µv", "uv",
    # Current
    "a", "ma", "ka", "µa", "ua",
    # Active / apparent / reactive power
    "w", "mw", "kw", "gw", "va", "mva", "kva", "mva",
    "var", "mvar", "kvar", "mvar", "varh", "kvarh",
    # Electrical energy / charge
    "wh", "mwh", "kwh", "gwh", "ah", "mah", "kah",
    # Grid/electrical quantities
    "hz", "khz", "mhz", "ohm", "kohm", "mohm", "ω", "kω", "mω",
}


def _normalized_unit(state):
    attrs = (state or {}).get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
    unit = unit.replace(" ", "").replace("μ", "µ").replace("Ω", "ω")
    return unit


def is_electrical_measurement_entity(entity_id, state):
    """Return True *only* when the entity reports an electrical engineering unit.

    v0.7.12 deliberately removes name-, domain-, device-class- and whole-device
    blacklists.  Home Assistant can expose useful behavioural context under almost any
    domain/name: phone sensors, cars, people, weather, template/virtual entities, camera
    scores and ESPHome radar channels are all valid candidates.  We exclude an entity
    only when its ``unit_of_measurement`` itself is unambiguously electrical (W, V, A,
    VA, var, Wh/kWh, Hz, ohm, etc.).

    This means a sensor named ``Still Energy`` with unit ``%`` remains eligible, as does
    a ``power`` device_class with no electrical unit.  Conversely ``sensor.foo`` with
    unit ``W`` is excluded regardless of its name or device class.
    """
    return _normalized_unit(state) in ELECTRICAL_UNITS


def electrical_context_exclusions(state_map, registry):
    """Entity-level electrical-unit exclusion only.

    Do not blacklist physical devices.  If a Shelly/ESPHome/phone exposes both an
    electrical measurement and useful non-electrical context, only the W/V/A/... entity
    is removed.  Every sibling without an electrical unit remains a learning candidate.
    """
    excluded = {
        eid for eid, st in (state_map or {}).items()
        if is_electrical_measurement_entity(eid, st)
    }
    return excluded, {
        "detected_electrical_entities": len(excluded),
        "detected_electrical_devices": 0,
        "excluded_electrical_context_entities": len(excluded),
    }


def is_context_candidate_entity(entity_id, state, excluded_entities=None):
    """Broad candidate gate: everything parseable is eligible unless explicitly blocked.

    Feature selection happens later from historical relevance.  Keeping this gate broad
    is intentional: an unusual phone/car/template/camera entity must be allowed to prove
    that it predicts the target rather than being rejected by a hand-written whitelist.
    """
    if entity_id in set(excluded_entities or ()):
        return False
    return parse_state_value(state) is not None


def parse_fast_series_lags():
    raw = str(OPTIONS.get("fast_series_lags_seconds", "1,3,10"))
    vals = []
    for part in re.split(r"[,; ]+", raw.strip()):
        if not part:
            continue
        try:
            v = float(part)
            if 0.25 <= v <= 60:
                vals.append(v)
        except Exception:
            pass
    vals = sorted(set(vals))[:3]
    while len(vals) < 3:
        vals.append((1.0, 3.0, 10.0)[len(vals)])
    return vals[:3]


def context_scalar(entity_id, state, agent=None):
    """Scalar used in an explicit entity slot.

    Directly controllable Home Assistant entities and their device siblings are never
    fed into an agent. Electrical telemetry is blocked only by explicit electrical units;
    all other parseable HA state is allowed to compete during historical feature selection.
    """
    if state is None:
        return None
    try:
        if target_options_for_state(state):
            return None
    except Exception:
        pass
    if is_electrical_measurement_entity(entity_id, state):
        return None
    return state_scalar(state)


def entity_capability_tags(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    dc = str(attrs.get("device_class") or "").lower()
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    name = str(attrs.get("friendly_name") or "").lower()
    text = f"{entity_id.lower()} {dc} {unit} {name}".replace('_', ' ')
    domain = entity_id.split(".", 1)[0]
    caps = set()
    if (domain in ("person", "device_tracker") or dc in ("occupancy", "motion", "presence")
            or any(x in text for x in ("occupancy", "presence", "motion", "obecno"))):
        caps.add("occupancy")
    # ESPHome mmWave/camera helpers often expose the actual causal signal as a numeric
    # percentage or score rather than a binary_sensor. Examples from real installations:
    # "Still Energy" (%), "Move Energy" (%), and "AI detection" (points).
    activity_terms = (
        "ai detection", "aidetection", "detection score", "camera score",
        "still energy", "move energy", "moving energy", "radar energy",
        "still target", "moving target", "move target",
    )
    if any(x in text for x in activity_terms):
        caps.add("activity")
    if "illuminance" in text or "lux" in text or unit == "lx": caps.add("illuminance")
    if domain == "sun" or "sun elevation" in text or "solar elevation" in text: caps.add("sun")
    if "temperature" in text or "°c" in unit or "°f" in unit:
        caps.add("temperature")
        if any(x in text for x in ("outdoor", "outside", "external", "zewn")): caps.add("outdoor_temperature")
    if "humidity" in text: caps.add("humidity")
    if "co2" in text or "carbon dioxide" in text: caps.add("co2")
    if "voc" in text or "air quality" in text: caps.add("voc")
    if dc in ("window", "door", "opening") or any(x in text for x in ("window", "door", "okno", "drzwi")): caps.add("window")
    if "power" in text or unit in ("w", "kw"): caps.add("power")
    if "noise" in text or "sound" in text: caps.add("ambient_noise")
    return caps


def _name_tokens(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    text = f"{entity_id} {attrs.get('friendly_name') or ''}".lower()
    return {x for x in re.split(r"[^a-z0-9ąćęłńóśźż]+", text) if len(x) >= 3}


def is_fast_reactive_agent(agent):
    """Fast binary targets should follow local sensor edges, not minute-scale context."""
    domain = str(agent.get("target_entity") or "").split(".", 1)[0]
    return agent.get("target_property") == "power" and domain in ("light", "switch", "input_boolean")


def _context_locality(agent, entity_id, state, registry, target_state=None):
    target = agent["target_entity"]
    target_state = target_state or {}
    target_reg = registry.get(target) or {}
    reg = registry.get(entity_id) or {}
    overlap = _name_tokens(target, target_state) & _name_tokens(entity_id, state)
    same_device = bool(target_reg.get("device_id") and reg.get("device_id") == target_reg.get("device_id"))
    same_area = bool(target_reg.get("area_id") and reg.get("area_id") == target_reg.get("area_id"))
    semantic = bool(overlap)
    caps = entity_capability_tags(entity_id, state)
    occupancy = "occupancy" in caps
    activity = "activity" in caps
    local = same_device or same_area or semantic
    return {
        "same_device": same_device, "same_area": same_area, "semantic": semantic,
        "occupancy": occupancy, "activity": activity, "local": local, "overlap": sorted(overlap),
    }



def occupancy_state_bool(state):
    """Return a robust boolean for an occupancy/presence entity.

    Home Assistant binary_sensors normally expose on/off, while some ESPHome/template
    sensors can expose detected/clear, occupied/unoccupied, or numeric 0/1.  Driver
    discovery must treat all of those representations identically.
    """
    if state is None:
        return None
    text = str(state.get("state") or "").strip().lower()
    if text in ("on", "home", "open", "detected", "occupied", "active", "true", "yes"):
        return True
    if text in ("off", "not_home", "closed", "clear", "unoccupied", "inactive", "false", "no"):
        return False
    raw = parse_state_value(state)
    if is_number(raw):
        return float(raw) >= 0.5
    return None


def transition_edges(rows, value_fn):
    """Return False/True edge timestamps, skipping the first observed state."""
    out = {False: [], True: []}
    have_prev = False
    prev = None
    for row in rows or []:
        value = value_fn(archived_state(row))
        if value is None:
            continue
        value = bool(value)
        if not have_prev:
            prev = value
            have_prev = True
            continue
        if value != prev:
            out[value].append(float(row["ts"]))
            prev = value
    return out


def edge_association_f1(sensor_times, target_times, window_before, post_slop=1.0):
    """Bidirectional edge association score for one transition direction.

    Recall asks whether each target transition had a matching sensor edge shortly before
    it. Precision asks whether each sensor edge was actually followed by the matching
    target transition.  The second term prevents a noisy/high-rate radar from winning just
    because one of its many edges happens to be close to every light action.
    """
    sensor_times = list(sensor_times or [])
    target_times = list(target_times or [])
    if not sensor_times or not target_times:
        return 0.0

    def has_between(times, lo, hi):
        i = bisect_left(times, float(lo))
        return i < len(times) and times[i] <= float(hi)

    recall_hits = sum(
        1 for t in target_times
        if has_between(sensor_times, float(t) - float(window_before), float(t) + float(post_slop))
    )
    precision_hits = sum(
        1 for t in sensor_times
        if has_between(target_times, float(t) - float(post_slop), float(t) + float(window_before))
    )
    recall = recall_hits / max(1, len(target_times))
    precision = precision_hits / max(1, len(sensor_times))
    if recall + precision <= 1e-12:
        return 0.0
    return 2.0 * recall * precision / (recall + precision)


def balanced_presence_driver_score(sensor_edges, target_edges):
    """Balanced ON/OFF causal score used to discover the real occupancy driver.

    ON is expected to be close to the presence edge. OFF deliberately allows the wider
    historical window used by legacy HA automations, because the purpose is to discover
    the sensor that *caused* a delayed OFF and then anchor desired-state learning back to
    that sensor edge.
    """
    directional = []
    for positive, window in (
        (True, float(OPTIONS.get("fast_precursor_on_seconds", 8))),
        (False, float(OPTIONS.get("fast_precursor_off_seconds", 120))),
    ):
        target_times = list((target_edges or {}).get(positive) or [])
        if not target_times:
            continue
        directional.append(edge_association_f1(
            (sensor_edges or {}).get(positive) or [], target_times, window, post_slop=1.0
        ))
    return sum(directional) / len(directional) if directional else 0.0



def _recent_numeric_before(rows, ts, window_before, post_slop=1.0):
    """Most recent numeric sensor value near a target transition."""
    lo = float(ts) - float(window_before)
    hi = float(ts) + float(post_slop)
    best = None
    for row in rows or []:
        rts = float(row.get("ts") or 0.0)
        if rts < lo or rts > hi:
            continue
        raw = parse_state_value(archived_state(row))
        if not is_number(raw):
            continue
        if best is None or rts > best[0]:
            best = (rts, float(raw))
    return None if best is None else best[1]


def numeric_activity_driver_score(sensor_rows, target_edges):
    """Balanced separability score for fast numeric behavioural sensors.

    This catches ESPHome signals such as LD2411 ``Still Energy`` (%) and camera
    ``AI detection`` scores. A useful sensor should be systematically different near
    target ON versus OFF transitions. The score is intentionally bounded and only used
    for entities already classified as behavioural/activity sensors.
    """
    on_vals = []
    off_vals = []
    for t in (target_edges or {}).get(True, []) or []:
        v = _recent_numeric_before(sensor_rows, t, float(OPTIONS.get("fast_precursor_on_seconds", 8)), 1.0)
        if v is not None:
            on_vals.append(v)
    for t in (target_edges or {}).get(False, []) or []:
        v = _recent_numeric_before(sensor_rows, t, float(OPTIONS.get("fast_precursor_off_seconds", 120)), 1.0)
        if v is not None:
            off_vals.append(v)
    if len(on_vals) < 2 or len(off_vals) < 2:
        return 0.0
    all_vals = sorted(on_vals + off_vals)
    lo, hi = all_vals[0], all_vals[-1]
    if abs(hi - lo) < 1e-9:
        return 0.0
    def median(xs):
        ys = sorted(xs); n = len(ys)
        return ys[n//2] if n % 2 else 0.5 * (ys[n//2-1] + ys[n//2])
    mon, moff = median(on_vals), median(off_vals)
    threshold = 0.5 * (mon + moff)
    if mon >= moff:
        on_ok = sum(v >= threshold for v in on_vals) / len(on_vals)
        off_ok = sum(v < threshold for v in off_vals) / len(off_vals)
    else:
        on_ok = sum(v <= threshold for v in on_vals) / len(on_vals)
        off_ok = sum(v > threshold for v in off_vals) / len(off_vals)
    balanced = 0.5 * (on_ok + off_ok)
    separation = min(1.0, abs(mon - moff) / max(1e-9, hi - lo))
    # Require better-than-chance class separation; sample coverage keeps tiny histories
    # from immediately becoming a dominant driver.
    skill = clamp((balanced - 0.5) * 2.0, 0.0, 1.0)
    coverage = clamp(min(len(on_vals), len(off_vals)) / 6.0, 0.0, 1.0)
    return clamp((0.75 * skill + 0.25 * separation) * coverage, 0.0, 1.0)

def is_esphome_sensor_entity(entity_id, registry):
    """True for ESPHome sensor/binary_sensor Entity Registry entries.

    ESPHome devices frequently expose configuration number/select/switch entities next
    to the actual LD24xx presence/radar sensors. Those configuration controls must never
    make the physical sensor channels disappear from the learning universe.
    """
    reg = (registry or {}).get(entity_id) or {}
    platform = str(reg.get("platform") or reg.get("integration") or "").strip().lower()
    domain = str(entity_id or "").split(".", 1)[0]
    return platform == "esphome" and domain in ("sensor", "binary_sensor")


def controllable_context_exclusions(state_map, registry):
    """Exclude actuators without blacklisting useful ESPHome sensing siblings.

    Every directly controllable entity is excluded from every agent input. Device-wide
    sibling exclusion is reserved for *real actuator* domains (light/switch/climate/etc.).
    Configuration-like number/select entities are deliberately NOT allowed to blacklist
    their whole physical ESPHome device. Even on a real ESPHome actuator device,
    sensor/binary_sensor siblings remain eligible; explicit electrical-unit filtering is
    applied separately afterwards.

    This fixes LD2411/LD24xx layouts such as ``binary_sensor.kitchen_presence_presence``
    sharing one ESPHome device with threshold numbers, engineering-mode selects/switches
    and radar diagnostics.
    """
    registry = registry or {}
    controllable_entities = set()
    actuator_devices = set()
    strong_actuator_domains = {
        "light", "switch", "climate", "cover", "fan", "media_player",
        "humidifier", "water_heater",
    }
    for eid, st in (state_map or {}).items():
        try:
            is_controllable = bool(target_options_for_state(st))
        except Exception:
            is_controllable = False
        if not is_controllable:
            continue
        controllable_entities.add(eid)
        domain = str(eid).split(".", 1)[0]
        device_id = (registry.get(eid) or {}).get("device_id")
        if device_id and domain in strong_actuator_domains:
            actuator_devices.add(device_id)

    excluded = set(controllable_entities)
    rescued_esphome_sensors = 0
    if actuator_devices:
        for eid, reg in registry.items():
            if (reg or {}).get("device_id") not in actuator_devices:
                continue
            if eid in controllable_entities:
                continue
            if is_esphome_sensor_entity(eid, registry):
                rescued_esphome_sensors += 1
                continue
            excluded.add(eid)
    return excluded, {
        "detected_controllable_entities": len(controllable_entities),
        "detected_controllable_devices": len(actuator_devices),
        "excluded_controllable_context_entities": len(excluded),
        "esphome_sensor_sibling_overrides": rescued_esphome_sensors,
    }

def select_context_entities(agent, state_map, registry, hint_entities, max_entities=None, relevance_scores=None):
    """Select a compact model from the broad all-entity candidate universe.

    v0.7.12 screens every parseable HA entity except controllable-device inputs and
    entities carrying explicitly electrical units. Historical relevance then chooses a
    compact subset for live inference. This keeps CPU low without hand-written semantic
    blacklists that can accidentally remove phone, car, weather, virtual, camera-score or
    custom ESPHome context. Local/causal occupancy still receives priority for fast lights.
    """
    target = agent["target_entity"]
    domain = target.split(".", 1)[0]
    target_state = state_map.get(target) or {}
    target_tokens = _name_tokens(target, target_state)
    need_caps = {x[0] for x in SENSOR_NEEDS.get(domain, [])}
    hints = set(hint_entities or ())
    requested = set(agent.get("input_entities") or ["*"])
    excluded_control_entities, exclusion_meta = controllable_context_exclusions(state_map, registry)
    excluded_electrical_entities, electrical_meta = electrical_context_exclusions(state_map, registry)
    excluded_context_entities = excluded_control_entities | excluded_electrical_entities
    limit_by_dims = max(4, (int(OPTIONS.get("feature_dimensions", 128)) - 9) // 4)
    limit = min(int(max_entities or OPTIONS.get("max_context_entities", 28)), limit_by_dims)
    fast = is_fast_reactive_agent(agent)
    if fast:
        # Fast binary behaviour is normally driven by a handful of causal sensors.
        # Keeping dozens of weak features makes a simple occupancy rule harder to clone
        # and costs CPU.  Prefer a compact short-series schema.
        limit = min(limit, max(2, int(OPTIONS.get("fast_max_context_entities", 8))))
    ranked = []
    considered = 0
    esphome_candidates = 0
    for eid, st in state_map.items():
        if "*" not in requested and eid not in requested:
            continue
        edomain = eid.split(".", 1)[0]
        if not is_context_candidate_entity(eid, st, excluded_context_entities):
            continue
        considered += 1
        if is_esphome_sensor_entity(eid, registry):
            esphome_candidates += 1
        if eid == target:
            continue
        loc = _context_locality(agent, eid, st, registry, target_state)
        score = 0.0
        reasons = []
        rel = float((relevance_scores or {}).get(eid, 0.0))
        causal_min = float(OPTIONS.get("fast_causal_driver_min_score", 0.50))
        if fast and (loc["occupancy"] or loc.get("activity")) and rel >= causal_min:
            # A historically proven behavioural driver outranks geography/name heuristics.
            # This includes binary presence and numeric ESPHome radar/AI activity scores.
            score += 4800.0 * clamp(rel, 0.0, 1.0); reasons.append("causal-behaviour")
        if fast and loc["occupancy"] and loc["local"]:
            # Primary room occupancy is the most important structural cue for fast lights.
            score += 5200; reasons.append("local-primary")
        elif fast and loc["local"]:
            score += 2200; reasons.append("local-context")
        if eid in hints:
            score += 1500 if fast else 3500
            reasons.append("automation")
        if rel > 0:
            score += (1800.0 if fast else 1400.0) * clamp(rel, 0.0, 1.0)
            reasons.append("historical-precursor")
        if loc["same_device"]:
            score += 1200 if fast else 900; reasons.append("same-device")
        if loc["same_area"]:
            score += 1100 if fast else 700; reasons.append("same-area")
        caps = entity_capability_tags(eid, st)
        matching = need_caps & caps
        if matching:
            score += 620 + 90 * len(matching); reasons.append("sensor-fit")
        if edomain in ("person", "device_tracker", "sun", "weather"):
            score += 180
        elif edomain in ("sensor", "binary_sensor"):
            score += 120
        overlap = target_tokens & _name_tokens(eid, st)
        if overlap:
            score += min(420 if fast else 220, (100 if fast else 55) * len(overlap)); reasons.append("semantic")
        changed = parse_ts((st or {}).get("last_changed"))
        if changed:
            age = max(0.0, now_ts() - changed)
            score += 80.0 * math.exp(-age / 21600.0)
        ranked.append((score, eid, reasons, loc))
    ranked.sort(key=lambda x: (-x[0], x[1]))

    # First reserve historically proven occupancy drivers, regardless of HA area/name.
    # A radar that repeatedly precedes the lamp's ON and OFF transitions is more causal
    # than a merely co-located temperature/presence entity.
    selected = []
    if fast:
        causal_min = float(OPTIONS.get("fast_causal_driver_min_score", 0.50))
        causal_reserve = min(limit, max(0, int(OPTIONS.get("fast_causal_driver_reserve", 2))))
        causal_ranked = [
            x for x in ranked
            if (x[3]["occupancy"] or x[3].get("activity")) and float((relevance_scores or {}).get(x[1], 0.0)) >= causal_min
        ]
        causal_ranked.sort(key=lambda x: (-float((relevance_scores or {}).get(x[1], 0.0)), -x[0], x[1]))
        selected.extend([eid for _, eid, _, _ in causal_ranked[:causal_reserve]])

    # Then reserve structurally local occupancy/context before filling the remainder with
    # upstream/global features. Causal history wins if HA area metadata is wrong/missing.
    reserve = min(limit, max(0, int(OPTIONS.get("primary_local_sensor_reserve", 4)))) if fast else 0
    local_ranked = [x for x in ranked if x[3]["local"]]
    local_occupancy_ranked = [x for x in local_ranked if x[3]["occupancy"]]
    local_target_count = min(limit, len(selected) + reserve)
    for _, eid, _, _ in local_occupancy_ranked + local_ranked:
        if eid not in selected:
            selected.append(eid)
        if len(selected) >= local_target_count:
            break

    # Existing HA automations are the behavioural benchmark. Preserve a bounded number
    # of their trigger/condition entities in the explicit schema (unless the entity is
    # an actuator/electrical input filtered above) so the RL policy has access to the
    # same causal signals the old rules used. Local occupancy still wins the first slots
    # for fast lights; automation inputs fill the next reserved slots.
    automation_reserve = min(
        max(0, limit - len(selected)),
        max(0, int(OPTIONS.get("automation_context_reserve", 8))),
    )
    automation_ranked = [x for x in ranked if "automation" in x[2]]
    added_automation = 0
    for _, eid, _, _ in automation_ranked:
        if eid in selected:
            continue
        selected.append(eid)
        added_automation += 1
        if added_automation >= automation_reserve or len(selected) >= limit:
            break

    if fast:
        # Do not fill the remaining slots with arbitrary whole-home context.  After local
        # and automation-reserved signals, only keep historically specific precursors or
        # sensors that match the target's declared needs.  This is intentionally sparse:
        # a lamp controlled by one presence sensor should look like a one-sensor problem.
        for _, eid, reasons, loc in ranked:
            if eid in selected:
                continue
            rel = float((relevance_scores or {}).get(eid, 0.0))
            caps = entity_capability_tags(eid, state_map.get(eid) or {})
            useful = ("*" not in requested) or loc["local"] or bool(need_caps & caps) or rel >= 0.25 or "automation" in reasons
            if useful:
                selected.append(eid)
            if len(selected) >= limit:
                break
    else:
        for _, eid, _, _ in ranked:
            if eid not in selected:
                selected.append(eid)
            if len(selected) >= limit:
                break
    selected_set = set(selected)
    rationale = {eid: reasons for _, eid, reasons, _ in ranked if eid in selected_set}
    primary_local = [eid for _, eid, _, loc in ranked if eid in selected_set and loc["local"] and loc["occupancy"]]
    occupancy_selected = [x for x in ranked if x[1] in selected_set and x[3]["occupancy"]]
    occupancy_selected.sort(key=lambda x: (
        -float((relevance_scores or {}).get(x[1], 0.0)),
        0 if "causal-behaviour" in x[2] else 1,
        0 if x[3]["local"] else 1,
        -x[0], x[1],
    ))
    primary_occupancy = occupancy_selected[0][1] if occupancy_selected else (primary_local[0] if primary_local else None)
    causal_scores = {
        eid: round(float((relevance_scores or {}).get(eid, 0.0)), 4)
        for _, eid, reasons, loc in occupancy_selected[:6]
        if float((relevance_scores or {}).get(eid, 0.0)) > 0
    }
    behavioural_selected = [x for x in ranked if x[1] in selected_set and (x[3]["occupancy"] or x[3].get("activity"))]
    behavioural_selected.sort(key=lambda x: (-float((relevance_scores or {}).get(x[1], 0.0)), -x[0], x[1]))
    behavioural_scores = {
        eid: round(float((relevance_scores or {}).get(eid, 0.0)), 4)
        for _, eid, _, _ in behavioural_selected[:8]
        if float((relevance_scores or {}).get(eid, 0.0)) > 0
    }
    upstream = [eid for _, eid, reasons, loc in ranked if eid in selected_set and not loc["local"] and ("automation" in reasons or "historical-precursor" in reasons or "causal-behaviour" in reasons)]
    esphome_selected = sum(1 for eid in selected if is_esphome_sensor_entity(eid, registry))
    return selected, {
        "considered_entities": considered, "selected_entities": len(selected),
        "selection_reasons": rationale,
        "primary_local_sensors": primary_local[:4],
        "primary_local_sensor": primary_local[0] if primary_local else None,
        "primary_occupancy_sensor": primary_occupancy,
        "causal_presence_scores": causal_scores,
        "causal_behaviour_scores": behavioural_scores,
        "primary_behavioural_drivers": [eid for _, eid, _, _ in behavioural_selected[:4]],
        "upstream_sensors": upstream[:8],
        "automation_reserved_entities": min(added_automation, automation_reserve),
        "fast_local_profile": bool(fast),
        "esphome_context_candidates": esphome_candidates,
        "esphome_selected_context": esphome_selected,
        **exclusion_meta, **electrical_meta,
    }

class ExplicitFeatureSchema:
    VERSION = 11
    def __init__(self, dims, entities):
        self.dims = int(dims)
        self.entities = list(entities)
        max_entities = max(1, (self.dims - 16) // 4)
        self.entities = self.entities[:max_entities]

    def export(self):
        return {"version": self.VERSION, "dims": self.dims, "entities": self.entities}

    @classmethod
    def from_export(cls, raw, dims):
        if not raw or int(raw.get("version", 0)) != cls.VERSION or int(raw.get("dims", -1)) != int(dims):
            return None
        return cls(dims, raw.get("entities") or [])

    def labels(self):
        out = {0: ["bias"], 1: ["time:hour_sin"], 2: ["time:hour_cos"], 3: ["time:dow_sin"], 4: ["time:dow_cos"]}
        idx = 5
        for eid in self.entities:
            for suffix in ("value", "lag_delta_1", "lag_delta_2", "lag_delta_3"):
                if idx >= self.dims - 7: break
                out[idx] = [f"{eid}:{suffix}"]; idx += 1
        # Use every remaining slot for deterministic pairwise interactions between the
        # highest-ranked entities. This adds a small non-linear residual without a heavy
        # neural runtime on Raspberry Pi.
        vals = self.entities[:8]
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                if idx >= self.dims - 7:
                    break
                out[idx] = [f"interaction:{vals[i]}×{vals[j]}"]
                idx += 1
            if idx >= self.dims - 7:
                break
        return out


class TemporalHistory:
    """Small in-memory timeline used by live inference and historical replay."""
    def __init__(self, maxlen=96):
        self.samples = {}
        self.maxlen = maxlen

    def add(self, entity_id, ts, state):
        if not entity_id or state is None: return
        dq = self.samples.setdefault(entity_id, deque(maxlen=self.maxlen))
        t = float(ts)
        if dq and abs(dq[-1][0] - t) < 1e-6:
            dq[-1] = (t, state)
        elif not dq or t >= dq[-1][0]:
            dq.append((t, state))

    def previous(self, entity_id, at_ts):
        dq = self.samples.get(entity_id)
        if not dq: return None
        target = float(at_ts)
        for ts, st in reversed(dq):
            if ts <= target:
                return st
        return None

    def last_change_ts(self, entity_id, fallback_state=None):
        dq = self.samples.get(entity_id)
        if dq:
            return dq[-1][0]
        return parse_ts((fallback_state or {}).get("last_changed"))


class HistoricalTemporalTracker:
    """Indexed as-of snapshots; callers may request times in any order.

    Dwells of different agents overlap. A shared forward-only cursor silently
    exposed future context to earlier samples. Binary search prevents that leak.
    """
    def __init__(self, rows, watched_entities=None):
        watched = set(watched_entities or ())
        self.index = {}
        for row in rows:
            eid = row["entity_id"]
            if watched and eid not in watched:
                continue
            times, states = self.index.setdefault(eid, ([], []))
            times.append(float(row["ts"]))
            states.append(archived_state(row))
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)

    def advance(self, ts):
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)
        for eid, (times, states) in self.index.items():
            end = bisect_right(times, float(ts))
            if end:
                self.state_map[eid] = states[end - 1]
                self.history.samples[eid] = deque(zip(times[max(0,end-64):end], states[max(0,end-64):end]), maxlen=64)

    def directional_transition_before(self, entity_id, at_ts, positive, window):
        data = self.index.get(entity_id)
        if not data:
            return None
        times, states = data
        end = bisect_right(times, float(at_ts))
        start = max(1, bisect_right(times, float(at_ts) - float(window)) - 1)
        for i in range(end - 1, start - 1, -1):
            cur = state_scalar(states[i]); prev = state_scalar(states[i - 1])
            if cur is None or prev is None:
                continue
            if positive and cur > 0.25 and prev <= 0.25:
                return times[i]
            if not positive and cur < -0.25 and prev >= -0.25:
                return times[i]
        return None

    def first_directional_transition_after(self, entity_id, start_ts, end_ts, positive):
        data = self.index.get(entity_id)
        if not data:
            return None
        times, states = data
        start = max(1, bisect_right(times, float(start_ts)))
        end = bisect_right(times, float(end_ts))
        for i in range(start, end):
            cur = state_scalar(states[i]); prev = state_scalar(states[i - 1])
            if cur is None or prev is None:
                continue
            if positive and cur > 0.25 and prev <= 0.25:
                return times[i]
            if not positive and cur < -0.25 and prev >= -0.25:
                return times[i]
        return None


def build_explicit_features(schema, state_map, temporal, at_ts=None, agent=None, excluded_entities=None):
    at_ts = float(at_ts if at_ts is not None else now_ts())
    dt = datetime.fromtimestamp(at_ts).astimezone()
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    dow = dt.weekday()
    fast_profile = bool(agent and is_fast_reactive_agent(agent))
    clock_weight = clamp(float(OPTIONS.get("fast_clock_context_weight", 0.15)), 0.0, 1.0) if fast_profile else 1.0
    vec = {
        0: 1.0,
        1: clock_weight * math.sin(2 * math.pi * hour / 24),
        2: clock_weight * math.cos(2 * math.pi * hour / 24),
        3: clock_weight * math.sin(2 * math.pi * dow / 7),
        4: clock_weight * math.cos(2 * math.pi * dow / 7),
    }
    labels = schema.labels()
    default_short_s = float(OPTIONS.get("temporal_short_seconds", 60))
    default_long_s = float(OPTIONS.get("temporal_long_seconds", 300))
    base_values = {}
    idx = 5
    usable = 0
    excluded_entities = set(excluded_entities or ())
    for eid in schema.entities:
        st = state_map.get(eid)
        cur = None if eid in excluded_entities else (context_scalar(eid, st, agent) if st else None)
        if cur is None:
            cur = 0.0
        else:
            usable += 1
        sharp = fast_profile
        if sharp:
            # Fast targets learn from a compact causal time series rather than a static
            # whole-home snapshot. Current value plus 1/3/10 s deltas captures occupancy
            # edges, direction and very short trends without carrying minute-scale memory.
            lags = parse_fast_series_lags()
            lag_values = []
            for lag_s in lags:
                lag_st = None if eid in excluded_entities else (temporal.previous(eid, at_ts - lag_s) if temporal else None)
                lag_v = context_scalar(eid, lag_st, agent) if lag_st else cur
                lag_values.append(cur - (lag_v if lag_v is not None else cur))
            vals = (cur, lag_values[0], lag_values[1], lag_values[2])
        else:
            short_s = default_short_s
            long_s = default_long_s
            recent_tau = max(30.0, long_s)
            if eid in excluded_entities:
                short_st = long_st = None
                short_v = long_v = cur
                changed_ts = None
                recent = 0.0
            else:
                short_st = temporal.previous(eid, at_ts - short_s) if temporal else None
                long_st = temporal.previous(eid, at_ts - long_s) if temporal else None
                short_v = context_scalar(eid, short_st, agent) if short_st else cur
                long_v = context_scalar(eid, long_st, agent) if long_st else cur
                changed_ts = temporal.last_change_ts(eid, st) if temporal else parse_ts((st or {}).get("last_changed"))
                age = max(0.0, at_ts - float(changed_ts)) if changed_ts else 86400.0
                recent = math.exp(-age / max(0.25, recent_tau))
            vals = (cur, cur - (short_v if short_v is not None else cur), cur - (long_v if long_v is not None else cur), recent)
        base_values[eid] = cur
        if agent and eid == agent.get("target_entity"):
            target_name = str(agent.get("target_property") or "target")
            for off, suffix in enumerate((target_name, f"{target_name}_delta_short", f"{target_name}_delta_long", "recent_change")):
                if idx + off < schema.dims - 7:
                    labels[idx + off] = [f"{eid}:{suffix}"]
        for v in vals:
            if idx >= schema.dims - 7: break
            if abs(float(v)) > 1e-12: vec[idx] = float(v)
            idx += 1
    top = schema.entities[:8]
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            if idx >= schema.dims - 7:
                break
            v = base_values.get(top[i], 0.0) * base_values.get(top[j], 0.0)
            if abs(v) > 1e-12:
                vec[idx] = v
            idx += 1
        if idx >= schema.dims - 7:
            break
    return vec, labels, {
        "usable_entities": usable, "selected_entities": len(schema.entities), "dimensions": schema.dims,
        "excluded_controllable_inputs": len(set(schema.entities) & excluded_entities),
    }


def action_values(agent):
    lo, hi = float(agent["min_value"]), float(agent["max_value"])
    if agent["target_property"] == "option_index":
        return [float(i) for i in range(int(round(lo)), int(round(hi)) + 1)]
    if agent["target_property"] == "power" or hi - lo <= 1.01:
        return [lo, hi]
    bins = max(5, int(OPTIONS["action_bins"]))
    step = (hi - lo) / (bins - 1)
    return [lo + i * step for i in range(bins)]


def capability_inventory(state_map, entity_ids=None):
    caps = set()
    allowed = set(entity_ids) if entity_ids is not None else None
    for entity_id, state in state_map.items():
        if allowed is not None and entity_id not in allowed:
            continue
        if is_electrical_measurement_entity(entity_id, state):
            continue
        attrs = state.get("attributes") or {}
        dc = str(attrs.get("device_class") or "").lower()
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        text = f"{entity_id.lower()} {dc} {unit} {str(attrs.get('friendly_name') or '').lower()}"
        domain = entity_id.split(".", 1)[0]
        if domain in ("person", "device_tracker") or any(t in text for t in ("occupancy", "presence", "motion")):
            caps.add("occupancy")
        if "illuminance" in text or "lux" in text or unit == "lx":
            caps.add("illuminance")
        if domain == "sun" or "solar elevation" in text or "sun elevation" in text:
            caps.add("sun")
        if "temperature" in text or "°c" in unit or "°f" in unit:
            caps.add("temperature")
            if any(t in text for t in ("outdoor", "outside", "zewn", "external")):
                caps.add("outdoor_temperature")
        if "humidity" in text or unit == "%" and "humidity" in entity_id.lower():
            caps.add("humidity")
        if "co2" in text or "carbon dioxide" in text:
            caps.add("co2")
        if "voc" in text or "volatile organic" in text or "air quality" in text:
            caps.add("voc")
        if dc in ("door", "window", "opening") or any(t in text for t in ("window", "door", "okno", "drzwi")):
            caps.add("window")
        if "power" in text or unit in ("w", "kw"):
            caps.add("power")
        if any(t in text for t in ("noise", "sound level", "decibel", "db")):
            caps.add("ambient_noise")
    return caps


def sensor_recommendations(agent, state_map, policy_confidence, selected_entities=None):
    domain = agent["target_entity"].split(".", 1)[0]
    # Recommendations should describe the context this agent can actually use, not just
    # whether a sensor of that class exists somewhere else in the house.
    present = capability_inventory(state_map, selected_entities) if selected_entities else capability_inventory(state_map)
    recs = []
    for cap, label, reason in SENSOR_NEEDS.get(domain, []):
        if cap not in present:
            recs.append({"capability": cap, "label": label, "reason": reason, "priority": "high" if policy_confidence < 0.65 else "medium"})
    # Always make the uncertainty limitation explicit: this is a heuristic, not causal discovery.
    if not recs and policy_confidence < 0.45:
        recs.append({
            "capability": "more_feedback", "label": "More RL feedback",
            "reason": "The expected sensor classes are already present; low confidence currently comes mainly from too few rewarded interactions.",
            "priority": "medium",
        })
    return recs[:4], sorted(present)




def archived_state(row):
    try:
        attrs = json.loads(row.get("attributes_json") or "{}")
    except Exception:
        attrs = {}
    return {
        "entity_id": row["entity_id"],
        "state": row.get("state"),
        "attributes": attrs,
        "context": {"user_id": row.get("context_user_id")},
        "last_changed": iso_from_ts(row["ts"]),
        "last_updated": iso_from_ts(row["ts"]),
    }


def target_options_for_state(state):
    if not state:
        return []
    domain = state.get("entity_id", "").split(".", 1)[0]
    attrs = state.get("attributes") or {}
    out = []
    for opt in SUPPORTED_TARGETS.get(domain, []):
        item = dict(opt)
        if domain == "light" and item["property"] == "brightness_pct":
            modes = attrs.get("supported_color_modes") or []
            if attrs.get("brightness") is None and not any(m not in ("onoff", None) for m in modes):
                continue
        if domain == "climate" and item["property"] == "temperature" and attrs.get("temperature") is None:
            continue
        if domain == "cover" and item["property"] == "position" and attrs.get("current_position") is None:
            continue
        if domain == "fan" and item["property"] == "percentage" and attrs.get("percentage") is None:
            continue
        if domain == "media_player" and item["property"] == "volume_pct" and attrs.get("volume_level") is None:
            continue
        if domain == "humidifier" and item["property"] == "humidity" and attrs.get("humidity") is None:
            continue
        if domain == "water_heater" and item["property"] == "temperature" and attrs.get("temperature") is None:
            continue
        if domain in ("select", "input_select") and item["property"] == "option_index":
            options = list(attrs.get("options") or [])
            if len(options) < 2:
                continue
            item["min"] = 0
            item["max"] = len(options) - 1
            item["deadband"] = 0.5
            item["exploration_step"] = 1
            item["option_labels"] = options
        if domain in ("number", "input_number"):
            if attrs.get("min") is not None:
                item["min"] = float(attrs["min"])
            if attrs.get("max") is not None:
                item["max"] = float(attrs["max"])
            if attrs.get("step") is not None:
                item["deadband"] = max(float(attrs["step"]), 1e-6)
                item["exploration_step"] = max(float(attrs["step"]), (item["max"] - item["min"]) * 0.02)
        if domain in ("climate", "water_heater"):
            item["min"] = float(attrs.get("min_temp", item["min"]))
            item["max"] = float(attrs.get("max_temp", item["max"]))
            step = float(attrs.get("target_temp_step") or .5)
            item["deadband"] = max(.01, step * .5)
            item["exploration_step"] = step
        if domain == "humidifier":
            item["min"] = float(attrs.get("min_humidity", item["min"]))
            item["max"] = float(attrs.get("max_humidity", item["max"]))
        out.append(item)
    return out


def historical_acceptance_seconds(agent):
    domain = agent["target_entity"].split(".", 1)[0]
    return {
        "light": 300, "switch": 600, "input_boolean": 600,
        "climate": 1800, "cover": 900, "fan": 600,
        "number": 600, "input_number": 600, "media_player": 300,
        "humidifier": 900, "water_heater": 1800, "select": 600, "input_select": 600,
    }.get(domain, 600)


def historical_reward(agent, dwell_seconds, user_id=None, next_user_id=None):
    """Infer a conservative offline reward from how long a desired state persisted.

    v0.6 deliberately does *not* use the live 90 s correction window as a historical
    rejection rule. A hallway light that is ON for 15 seconds can be exactly right.
    Strong negative historical evidence is reserved for a rapid user correction of a
    non-user/automatic action (or an almost immediate user re-correction).
    """
    dwell = max(0.0, float(dwell_seconds))
    prop = str(agent.get("target_property") or "")
    domain = str(agent.get("target_entity") or "").split(".", 1)[0]

    if prop in ("power", "option_index"):
        tau = 10.0
        correction_window = 8.0
    elif prop in ("brightness_pct", "position", "percentage", "volume_pct", "value"):
        tau = 30.0
        correction_window = 12.0
    elif domain in ("climate", "humidifier", "water_heater"):
        tau = 300.0
        correction_window = 45.0
    else:
        tau = 60.0
        correction_window = 15.0

    # A quick explicit human override of an automatic/external action is the clearest
    # negative preference signal we can recover from Recorder history.
    if next_user_id and not user_id and dwell <= correction_window:
        severity = 1.0 - 0.45 * (dwell / max(correction_window, 1.0))
        return -clamp(severity, 0.45, 1.0)
    # If the same user changes a setting almost immediately, treat it as a likely
    # correction; after that, a short dwell is allowed to be intentional.
    if next_user_id and user_id and dwell <= min(2.0, correction_window):
        return -clamp(1.0 - 0.25 * dwell, 0.5, 1.0)

    # Persistence itself is positive desired-state evidence. Saturation is domain aware:
    # binary lights become informative within seconds; HVAC setpoints need minutes.
    reward = 0.15 + 0.85 * (1.0 - math.exp(-dwell / max(tau, 1.0)))
    if dwell < 0.5:
        reward *= 0.35
    # Automatic/external actions are useful (especially existing HA automations), but
    # explicit user-originated states remain slightly more authoritative.
    if not user_id:
        reward *= 0.80
    return clamp(reward, 0.02, 1.0)


def default_action_interval(entity_id, target_property):
    domain = str(entity_id or "").split(".", 1)[0]
    if domain in ("light", "switch", "input_boolean", "media_player", "select", "input_select"):
        return 1.0
    if domain in ("fan", "cover"):
        return 2.0
    if domain in ("number", "input_number"):
        return 2.0
    if domain in ("climate", "humidifier", "water_heater"):
        return 10.0
    return 5.0



def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def parse_state_value(state):
    if state is None:
        return None
    s = state.get("state")
    if s in (None, "unknown", "unavailable", "none", "None", ""):
        return None
    low = str(s).lower()
    if low in ("on", "home", "open", "detected", "occupied", "active", "playing", "heat", "cool"):
        return 1.0
    if low in ("off", "not_home", "closed", "clear", "unoccupied", "idle", "paused"):
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return str(s)
