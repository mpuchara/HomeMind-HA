"""U04 read-only, dynamically sized device/action contract for a universal policy.

This module never generates HA service calls and NEVER substitutes for
DeviceAgent.legal_action_mask or Executor. It accepts a *fresh caller-supplied*
mask from the existing arbiter, validates its shape, and exposes only legal
candidate values for observation. The mask itself is not a durable authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

ACTION_CONTRACT_VERSION = 1
ACTION_KINDS = ("binary", "continuous", "categorical")


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class ActionCandidate:
    """One caller-authorized numeric proposal, NOT a command."""
    entity_id: str
    logical_device_id: str
    area_id: str | None
    property_name: str
    kind: str
    value: float
    lower: float
    upper: float
    current_value: float | None
    option_label: str | None
    resources: tuple[str, ...]
    mask_timestamp: float


@dataclass(frozen=True)
class ActionSet:
    """Variable-cardinality proposals, fail-closed if no authoritative mask."""
    version: int
    agent_id: str
    mask_timestamp: float
    candidates: tuple[ActionCandidate, ...]
    rejected: int
    fallback: str
    source: str = "caller_supplied_DeviceAgent.legal_action_mask"

    @property
    def count(self):
        return len(self.candidates)


def _kind(property_name):
    if property_name == "power":
        return "binary"
    if property_name == "option_index":
        return "categorical"
    return "continuous"


def from_device_agent_mask(agent, descriptor, legal_mask, state, *, mask_timestamp):
    """Consume one DeviceAgent descriptor + legal_action_mask result.

    Do not invoke this with a fabricated all-legal mask. The caller is
    responsible for obtaining the mask from the trusted arbiter at
    the current decision time. Staleness/revalidation remain the
    Executor's responsibility. This output is strictly diagnostic.
    """
    if not isinstance(agent, dict) or not isinstance(descriptor, dict) or not isinstance(legal_mask, dict):
        raise TypeError("agent, DeviceAgent descriptor and legal mask required")
    if not isinstance(state, dict):
        raise TypeError("state snapshot required")
    when = _finite(mask_timestamp, "mask timestamp")
    agent_id = str(agent.get("id") or "")
    entity_id = str(agent.get("target_entity") or "")
    prop = str(agent.get("target_property") or "")
    if not agent_id or "." not in entity_id or not prop:
        raise ValueError("invalid target identity")
    if (str(descriptor.get("physical_target_property") or "") != prop
            or not descriptor.get("device_property")
            or not descriptor.get("logical_device_id")):
        raise ValueError("DeviceAgent identity/property mismatch")
    if state.get("entity_id") != entity_id:
        raise ValueError("HA state entity_id mismatch")
    if str(state.get("state") or "").lower() in ("unknown", "unavailable", ""):
        raise ValueError("not a usable target snapshot")
    if not descriptor.get("autonomy_allowed", False):
        return ActionSet(ACTION_CONTRACT_VERSION, agent_id, when, (), 0, "abstain")
    props = descriptor.get("properties") or ()
    if not any(p.get("entity_id") == entity_id and p.get("property") == prop for p in props):
        raise ValueError("target property not advertised by DeviceAgent")
    resources = descriptor.get("resource_keys")
    if not isinstance(resources, list) or not resources or any(not isinstance(key, str) or not key for key in resources):
        raise ValueError("missing DeviceAgent resource keys")
    actions = legal_mask.get("actions")
    if not isinstance(actions, list):
        raise ValueError("missing DeviceAgent legal-action result")
    lo, hi = _finite(agent.get("min_value"), "agent minimum"), _finite(agent.get("max_value"), "agent maximum")
    if lo > hi:
        raise ValueError("invalid agent action range")
    if prop in ("power", "brightness_pct", "position", "percentage", "volume_pct", "humidity"):
        lo, hi = max(0.0, lo), min(1.0 if prop == "power" else 100.0, hi)
    attrs = state.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise ValueError("invalid HA state attributes")
    lower_key, upper_key = ("min_temp", "max_temp") if prop == "temperature" else ("min", "max")
    if prop == "humidity":
        lower_key, upper_key = "min_humidity", "max_humidity"
    lo = max(lo, _finite(attrs.get(lower_key, lo), "device minimum"))
    hi = min(hi, _finite(attrs.get(upper_key, hi), "device maximum"))
    options = ()
    if prop == "option_index":
        options = attrs.get("options") or ()
        if not isinstance(options, list):
            raise ValueError("HA select options must be a list")
        hi = min(hi, float(len(options) - 1))
        lo = max(lo, 0.0)
    if lo > hi:
        return ActionSet(ACTION_CONTRACT_VERSION, agent_id, when, (), len(actions), "abstain")
    current = None
    try:
        from context import target_value
        current = target_value(state, prop)
        current = None if current is None else _finite(current, "current value")
    except (ValueError, TypeError):
        current = None
    result = []
    rejected = 0
    seen = set()
    for row in actions:
        if not isinstance(row, dict) or type(row.get("legal")) is not bool:
            raise ValueError("malformed legal-action mask row")
        if not row["legal"]:
            rejected += 1
            continue
        value = _finite(row.get("value"), "legal action value")
        _finite(row.get("requested"), "requested action value")
        if not lo - 1e-8 <= value <= hi + 1e-8:
            raise ValueError("arbiter value falls outside current capability bounds")
        if value in seen:
            continue
        seen.add(value)
        option_label = None
        if prop == "option_index":
            if abs(value - round(value)) > 1e-8 or not 0 <= int(round(value)) < len(options):
                raise ValueError("invalid select option_index")
            option_label = str(options[int(round(value))])
        if prop == "power" and value not in (0.0, 1.0):
            raise ValueError("binary power arm must be exact ON/OFF")
        result.append(ActionCandidate(
            entity_id, str(descriptor["logical_device_id"]),
            str(descriptor["area_id"]) if descriptor.get("area_id") else None,
            prop, _kind(prop), value, lo, hi, current, option_label,
            tuple(sorted(set(resources))), when,
        ))
    return ActionSet(
        ACTION_CONTRACT_VERSION, agent_id, when, tuple(result), rejected,
        "abstain" if not result else "observe_only",
    )
