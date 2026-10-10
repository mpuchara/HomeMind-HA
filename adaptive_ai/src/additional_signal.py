"""User-selected illumination context and an explicit, versioned ON preference.

A preference is not an observed OFF demonstration. Recorded actions retain their
labels; only the separate objective score and a new ON proposal use the preference.
No physical probe, early OFF, or inferred room-to-room threshold is created here.
"""
import json
import math
import re

from settings import now_ts, parse_ts


def normalize(value):
    if value is None:
        return None
    keys = {"version", "entity_id", "purpose", "threshold", "hysteresis", "max_age_seconds", "unit"}
    if not isinstance(value, dict) or set(value) - keys:
        raise ValueError("Invalid additional signal settings")
    eid = value.get("entity_id")
    if not isinstance(eid, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", eid):
        raise ValueError("Select an illumination sensor entity")
    purpose = value.get("purpose", "context")
    if (purpose not in ("context", "avoid_bright_on") or value.get("version", 1) != 1
            or type(value.get("version", 1)) is not int):
        raise ValueError("Unsupported additional signal purpose/version")
    out = {"version": 1, "entity_id": eid, "purpose": purpose}
    unit = value.get("unit", "raw")
    if unit not in ("raw", "lx", "lux"):
        raise ValueError("Unsupported illumination reference unit")
    out["unit"] = "lx" if unit == "lux" else unit
    for key, default, lo, hi in (("max_age_seconds", 900, 1, 86400),
                                ("hysteresis", 0, 0, 1000000)):
        raw = value.get(key, default)
        if isinstance(raw, bool):
            raise ValueError(key + " must be numeric")
        number = float(raw)
        if not math.isfinite(number) or not lo <= number <= hi:
            raise ValueError(key + " is outside its allowed range")
        out[key] = number
    if purpose == "avoid_bright_on":
        raw = value.get("threshold")
        if isinstance(raw, bool) or raw is None:
            raise ValueError("An explicit brightness threshold is required")
        number = float(raw)
        if not math.isfinite(number) or not 0 <= number <= 1000000 or out["hysteresis"] > number:
            raise ValueError("Invalid brightness threshold/hysteresis")
        out["threshold"] = number
    return out


def entities(agent):
    config = (agent or {}).get("additional_signal")
    return [config["entity_id"]] if config else []


def evaluate(agent, states, at_ts=None, temporal=None):
    config = (agent or {}).get("additional_signal")
    if not config:
        return {"configured": False, "need": True, "reason": "not_configured"}
    at = now_ts() if at_ts is None else float(at_ts)
    eid = config["entity_id"]
    state = (states or {}).get(eid) or {}
    # Use exactly the causal resolver also used by model features. A late/future
    # packet must not become evidence for a historical or earlier live decision.
    if temporal is not None:
        from observation_contract import _resolve_current_state
        _, state = _resolve_current_state(eid, states, temporal, at)
        state = state or {}
    attrs = state.get("attributes") or {}
    stamp = (attrs.get("__hm_received_time") or state.get("_feature_received_time")
             or state.get("last_updated") or attrs.get("__hm_last_updated"))
    event = attrs.get("__hm_event_time") or state.get("last_updated")
    stamp, event = parse_ts(stamp), parse_ts(event)
    stamp = stamp if stamp is not None and math.isfinite(stamp) else None
    event = event if event is not None and math.isfinite(event) else None
    unit = str(attrs.get("unit_of_measurement") or "raw").lower()
    unit = "lx" if unit == "lux" else unit
    try:
        value = float(state.get("state"))
        if not math.isfinite(value) or value < 0:
            value = None
    except (TypeError, ValueError):
        value = None
    reason = ("value_unavailable" if value is None else "timestamp_unknown" if stamp is None
              else "future_observation" if stamp > at + 1e-6 or (event is not None and event > at + 1e-6)
              else "stale_observation" if at - stamp > config["max_age_seconds"] else None)
    if reason is None and unit != config.get("unit", "raw"):
        reason = "unit_changed"
    out = {"configured": True, "entity_id": eid, "purpose": config["purpose"],
           "evaluated_at": at, "valid_until": None if stamp is None else stamp + config["max_age_seconds"],
           "value": value, "age_seconds": None if stamp is None else max(0, at-stamp),
           "unit": attrs.get("unit_of_measurement") or "raw", "need": None,
           "reason": reason or "context_only", "threshold": config.get("threshold"),
           "hysteresis": config["hysteresis"]}
    if reason or config["purpose"] == "context":
        return out
    if value >= config["threshold"] + config["hysteresis"]:
        out.update(need=False, reason="bright_enough")
    elif value < config["threshold"] - config["hysteresis"]:
        out.update(need=True, reason="dark")
    else:
        out["reason"] = "hysteresis_band"
    return out


def apply(agent, current, desired, evidence, source="policy"):
    """Suppress only a new ON; unknown/band retains the ordinary policy decision."""
    config = (agent or {}).get("additional_signal") or {}
    if (config.get("purpose") == "avoid_bright_on" and evidence.get("need") is False
            and current is not None and float(current) < .5 and float(desired) >= .5
            and source not in ("user_instruction", "explicit_preference", "legacy_teaching",
                               "manual", "experiment")):
        return 0.0, True
    return float(desired), False


def stabilize(evidence, previous=None):
    """Hysteresis holds a known decision only while current measurements stay valid.

    Missing/stale/unit-changed readings clear the latch. Replay without a previous
    observation remains unknown in the band and cannot fabricate objective evidence.
    """
    if evidence.get("reason") == "hysteresis_band" and (previous or {}).get("need") in (True, False):
        return {**evidence, "need": previous["need"], "reason": "hysteresis_hold"}
    return evidence


def shadow_prediction(manager, generation, states, at, result, temporal, agent=None):
    if result is None:
        return None
    if agent is None:
        cached = (getattr(getattr(manager, "engine", None), "models", {}) or {}).get(generation["agent_id"])
        agent = getattr(cached, "agent", None)
    if agent is None:
        agent = manager.store.get_agent_config(generation["agent_id"])
    if not agent or not agent.get("additional_signal"):
        return result
    from context import target_value
    current = target_value((states or {}).get(agent["target_entity"]), "power")
    target = (states or {}).get(agent["target_entity"]) or {}
    manual = bool((target.get("context") or {}).get("user_id") or target.get("context_user_id"))
    cache = getattr(manager, "_additional_signal_virtual", None)
    if cache is None:
        cache = manager._additional_signal_virtual = {}
    key = (generation["generation_id"], result.get("model_revision"))
    previous = cache.get(key) or {}
    virtual = current if manual else previous.get("power", current)
    evidence = stabilize(evaluate(agent, states, at, temporal), previous.get("evidence"))
    desired, changed = apply(agent, virtual, result["desired"], evidence, "manual" if manual else "policy")
    cache[key] = {"power": desired, "evidence": evidence}
    if len(cache) > 128:
        cache.pop(next(iter(cache)))
    return {**result, "raw_desired": result["desired"], "desired": desired,
            "confidence": None if changed else result.get("confidence"),
            "additional_signal": {**evidence, "applied": changed}}


def goal_transition(evidence, actual, *, manual=False, own=False):
    """Separate configured objective from recorded action and from user/agent echoes."""
    if (not manual and not own and evidence.get("purpose") == "avoid_bright_on"
            and evidence.get("need") is False and float(actual) >= .5):
        return 0.0, "configured_daylight_preference"
    return float(actual), "observed_action"


def child_config_matches(store, parent, candidate):
    """A goal child may differ only by the exact config requested for that child.

    The captured parent signature prevents edits during replay from reusing old proof.
    Ordinary children still require exactly matching configs.
    """
    from agent_candidate_config_guard import config_signature
    if config_signature(parent) == config_signature(candidate):
        return True
    try:
        with store.conn() as conn:
            row = conn.execute("""SELECT requested_config_json FROM agent_explore_sessions s
                JOIN agent_candidate_generations g ON g.generation_id=s.child_generation_id
                WHERE g.agent_id=? AND s.mode='additional_signal' ORDER BY s.created_ts DESC LIMIT 1""",
                (candidate["id"],)).fetchone()
    except Exception:
        return False
    if not row:
        return False
    request = json.loads(row[0])
    expected = {**parent, "additional_signal": request.get("additional_signal")}
    return (request.get("parent_signature") == config_signature(parent)
            and config_signature(candidate) == config_signature(expected))


def root_signal_matches(store, root, child_generation):
    """Find the first goal change on this lineage, not another retired branch."""
    from agent_candidate_config_guard import config_signature
    generation = child_generation
    for _ in range(64):
        if not generation or not generation.get("parent_generation_id"):
            return True
        with store.conn() as conn:
            parent = conn.execute("SELECT * FROM agent_candidate_generations WHERE generation_id=?",
                                  (generation["parent_generation_id"],)).fetchone()
            request = conn.execute("SELECT requested_config_json FROM agent_explore_sessions WHERE child_generation_id=? AND mode='additional_signal'",
                                   (generation["generation_id"],)).fetchone()
        if parent is None:
            return False
        if parent["generation_type"] == "live":
            if request:
                return json.loads(request[0]).get("parent_signature") == config_signature(root)
            return True
        generation = dict(parent)
    return False
