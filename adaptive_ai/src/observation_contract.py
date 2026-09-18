"""Observation/feature contract v12 for live, replay and Teach.

Installed once during runtime composition before workers start.  The repository's runtime
is deliberately layered; this module replaces one global feature contract (schema +
extractor + historical view) rather than adding per-agent wrappers.  Executor ownership is
untouched.
"""
from __future__ import annotations

from collections import deque
from contextlib import suppress
import hashlib
import json
import math
import time

import context as context_module
import history as history_module
import policy as policy_module
import replay as replay_module
import teaching as teaching_module
from context import (
    parse_fast_series_lags, is_fast_reactive_agent, target_options_for_state,
    is_electrical_measurement_entity,
)
from home_state import FEATURE_NAMES as LEGACY_HOME_FEATURE_NAMES
from provenance import stable_event_id
from settings import OPTIONS, clamp, iso_now, now_ts, parse_ts
from training_budget import TRAINING_BUDGET

SCHEMA_VERSION = 12
POLICY_VERSION = 11
CONTRACT_VERSION = 1
# Feature semantics are versioned per persisted schema. Contract 1 is the exact v12
# representation already stored in released models. Fresh/rebuilt models use contract 2,
# which removes fast-light photometric own-action leakage without reinterpreting old
# vectors or forcing a global model migration.
LEGACY_FEATURE_CONTRACT_VERSION = 1
FEATURE_CONTRACT_VERSION = 2
HOME_FEATURE_NAMES = tuple(LEGACY_HOME_FEATURE_NAMES) + ("known",)
HOME_TAIL = len(HOME_FEATURE_NAMES)

ENTITY_FEATURES = (
    "value",
    "valid",
    "communication_age",
    "event_age",
    "quality",
    "trend_1",
    "trend_2",
    "trend_3",
    "time_since_edge",
    "category_bit_0",
    "category_bit_1",
    "category_bit_2",
)
ENTITY_WIDTH = len(ENTITY_FEATURES)

INVALID_STATES = {"", "none", "null", "unknown", "unavailable"}
SEMANTIC_VALUES = {
    "off": -1.0, "closed": -1.0, "not_home": -1.0, "away": -1.0,
    "clear": -1.0, "unoccupied": -1.0, "false": -1.0,
    "idle": -0.5, "standby": -0.5, "paused": -0.5,
    "on": 1.0, "open": 1.0, "home": 1.0, "playing": 1.0,
    "detected": 1.0, "occupied": 1.0, "active": 1.0, "true": 1.0,
    "heat": 0.8, "heating": 0.8, "cool": -0.8, "cooling": -0.8,
    "dry": 0.4, "fan_only": 0.2, "auto": 0.1,
}

FAST_RETENTION_HOURS = 24.0
FAST_MAX_EVENTS_PER_ENTITY = 2048
WINDOW_RETENTION_DAYS = 7.0
WINDOW_MAX_PER_AGENT = 512
GLOBAL_EVENT_LIMIT = 50000
WINDOW_BEFORE_SECONDS = 12.0
WINDOW_AFTER_SECONDS = 6.0


def _attrs(state):
    return (state or {}).get("attributes") or {}


def _meta(state, key, default=None):
    if not state:
        return default
    if key in state:
        return state.get(key)
    # v12.0 briefly wrote live transport metadata as top-level ``_hm_*`` fields while
    # replay restored the same fields under attributes. Read both during rolling upgrade,
    # but all new live samples are written to attributes below so one canonical shape is
    # used by live, replay and Teach.
    legacy = "_hm_" + str(key)
    if legacy in state:
        return state.get(legacy)
    return _attrs(state).get("__hm_" + key, default)


def _state_ts(state, field, fallback=None):
    attrs = _attrs(state)
    value = attrs.get("__hm_" + str(field))
    if value is None:
        value = (state or {}).get(field)
    parsed = parse_ts(value)
    return float(parsed) if parsed is not None else fallback


def _normalized_unit(state):
    unit = str(_attrs(state).get("unit_of_measurement") or "").strip().lower()
    return unit.replace(" ", "").replace("μ", "µ")


def _numeric_raw(state):
    if not state:
        return None
    text = str(state.get("state") if state.get("state") is not None else "").strip()
    if text.lower() in INVALID_STATES:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _canonical_numeric(state, value):
    """Return canonical physical value and scale."""
    attrs = _attrs(state)
    unit = _normalized_unit(state)
    dc = str(attrs.get("device_class") or "").lower()
    eid = str((state or {}).get("entity_id") or "").lower()
    token = " ".join((unit, dc, eid))
    value = float(value)

    if dc == "temperature" or "temperature" in token or unit in ("°c", "c", "°f", "f", "k"):
        if unit in ("°f", "f"):
            value = (value - 32.0) * (5.0 / 9.0)
        elif unit == "k":
            value = value - 273.15
        return value, 35.0, "°c"
    if dc == "pressure" or unit in ("pa", "kpa", "hpa", "mbar"):
        if unit == "pa":
            value /= 100.0
        elif unit == "kpa":
            value *= 10.0
        return value, 1000.0, "hpa"
    if unit in ("%", "percent") or any(x in token for x in ("humidity", "battery", "position", "brightness")):
        return value, 100.0, "%"
    if "co2" in token or unit == "ppm":
        return value, 2000.0, "ppm"
    if "illuminance" in token or unit in ("lx", "lux"):
        return value, 1000.0, "lx"
    if unit in ("w", "kw"):
        return (value * 1000.0 if unit == "kw" else value), 5000.0, "w"
    return value, 10.0, unit or "raw"


def _category_bits(text):
    """Non-ordinal equality-oriented bits for open categorical states."""
    digest = hashlib.blake2b(str(text).encode("utf-8"), digest_size=1).digest()[0]
    return tuple(1.0 if digest & (1 << i) else -1.0 for i in range(3))


def observation_value(state):
    """Canonical observation: value/category are separate from validity."""
    if state is None:
        return {"valid": 0.0, "value": 0.0, "category": (0.0, 0.0, 0.0),
                "kind": "missing", "canonical_unit": None}
    text = str(state.get("state") if state.get("state") is not None else "").strip().lower()
    if text in INVALID_STATES:
        return {"valid": 0.0, "value": 0.0, "category": (0.0, 0.0, 0.0),
                "kind": "missing", "canonical_unit": None}
    if text in SEMANTIC_VALUES:
        return {"valid": 1.0, "value": float(SEMANTIC_VALUES[text]),
                "category": (0.0, 0.0, 0.0), "kind": "semantic", "canonical_unit": None}
    raw = _numeric_raw(state)
    if raw is not None:
        canonical, scale, unit = _canonical_numeric(state, raw)
        return {"valid": 1.0, "value": math.tanh(canonical / max(scale, 1e-9)),
                "physical_value": canonical, "category": (0.0, 0.0, 0.0),
                "kind": "numeric", "canonical_unit": unit}
    return {"valid": 1.0, "value": 0.0, "category": _category_bits(text),
            "kind": "category", "canonical_unit": "category"}


def _age_feature(age, horizon=3600.0):
    if age is None or not math.isfinite(float(age)):
        return 1.0
    age = max(0.0, float(age))
    return clamp(math.log1p(age) / math.log1p(max(1.0, float(horizon))), 0.0, 1.0)


def _sample_received(state):
    value = _meta(state, "received_time")
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _sample_event_time(state, fallback=None):
    value = _meta(state, "event_time")
    try:
        if value is not None:
            value = float(value)
            if math.isfinite(value):
                return value
    except (TypeError, ValueError):
        pass
    return _state_ts(state, "last_updated", fallback=fallback)


def _samples(temporal, entity_id):
    dq = getattr(temporal, "samples", {}).get(entity_id)
    return list(dq or ())


def _eligible_sample(state, event_ts, query_ts, knowledge_ts):
    if event_ts is None or event_ts > float(query_ts) + 1e-9:
        return False
    received = _sample_received(state)
    return received is None or received <= float(knowledge_ts) + 1e-9


def _sample_before(temporal, entity_id, query_ts, knowledge_ts):
    for ts, st in reversed(_samples(temporal, entity_id)):
        if _eligible_sample(st, float(ts), query_ts, knowledge_ts):
            return float(ts), st
    return None, None


def _latest_communication(temporal, entity_id, at_ts, current=None):
    latest = None
    for ts, st in _samples(temporal, entity_id):
        if float(ts) > float(at_ts) + 1e-9:
            continue
        received = _sample_received(st)
        if received is not None and received <= float(at_ts) + 1e-9:
            latest = max(latest or received, received)
    received = _sample_received(current)
    if received is not None and received <= float(at_ts) + 1e-9:
        latest = max(latest or received, received)
    return latest


def _same_observation(left, right):
    a, b = observation_value(left), observation_value(right)
    if not a["valid"] or not b["valid"]:
        return None
    if a["kind"] != b["kind"]:
        return False
    if a["kind"] == "category":
        return a["category"] == b["category"]
    return abs(float(a["value"]) - float(b["value"])) <= 1e-9


def _last_edge_time(temporal, entity_id, at_ts, current):
    """Last valid value edge; missing/unavailable samples never create an edge."""
    ordered = [(float(ts), st) for ts, st in _samples(temporal, entity_id)
               if float(ts) <= float(at_ts) + 1e-9]
    if not ordered:
        changed = _state_ts(current, "last_changed")
        return changed if changed is not None and changed <= at_ts else None
    last_valid = None
    first_valid_ts = None
    valid_count = 0
    edge = None
    for ts, st in ordered:
        obs = observation_value(st)
        if not obs["valid"]:
            continue
        valid_count += 1
        if first_valid_ts is None:
            first_valid_ts = ts
        if last_valid is not None:
            same = _same_observation(last_valid[1], st)
            if same is False:
                edge = ts
        last_valid = (ts, st)
    if edge is not None:
        return edge
    if valid_count >= 2:
        return first_valid_ts
    changed = _state_ts(current, "last_changed")
    if changed is not None and changed <= at_ts:
        return changed
    return last_valid[0] if last_valid else None


def _source_quality(state, obs, fast_profile):
    if not obs["valid"]:
        return 0.0, "invalid"
    source = str(_meta(state, "feature_source", "") or "")
    received = _sample_received(state)
    if source in ("ha_state_changed", "ha_poll_confirmation", "feature_event"):
        return 1.0, "confirmed"
    if source.startswith("ha_history_full"):
        return 0.85, "recorder_full"
    if received is not None:
        return 0.95, "received"
    if obs["kind"] in ("semantic", "category"):
        return 0.75, "stateful_sparse"
    if fast_profile and obs["kind"] == "numeric":
        return 0.35, "sparse_numeric"
    return 0.60, "sparse"


class FeatureSchemaV12:
    VERSION = SCHEMA_VERSION

    def __init__(self, dims, entities, feature_contract_version=FEATURE_CONTRACT_VERSION):
        self.dims = int(dims)
        self.feature_contract_version = int(feature_contract_version)
        if self.feature_contract_version not in (
            LEGACY_FEATURE_CONTRACT_VERSION, FEATURE_CONTRACT_VERSION
        ):
            raise ValueError("unsupported feature contract version")
        max_entities = max(1, (self.dims - 5 - HOME_TAIL) // ENTITY_WIDTH)
        self.entities = list(entities)[:max_entities]

    def export(self):
        return {"version": self.VERSION, "dims": self.dims, "entities": list(self.entities),
                "entity_features": list(ENTITY_FEATURES), "home_features": list(HOME_FEATURE_NAMES),
                "feature_contract_version": self.feature_contract_version}

    @classmethod
    def from_export(cls, raw, dims):
        if (not raw or int(raw.get("version", 0)) != cls.VERSION
                or int(raw.get("dims", -1)) != int(dims)):
            return None
        if list(raw.get("entity_features") or []) != list(ENTITY_FEATURES):
            return None
        if list(raw.get("home_features") or []) != list(HOME_FEATURE_NAMES):
            return None
        # Released v12 schemas predate this field. They stay contract 1 forever unless a
        # new model generation is explicitly rebuilt; saving/loading them never changes
        # the meaning of their existing weight columns.
        feature_contract = int(raw.get(
            "feature_contract_version", LEGACY_FEATURE_CONTRACT_VERSION
        ))
        if feature_contract not in (
            LEGACY_FEATURE_CONTRACT_VERSION, FEATURE_CONTRACT_VERSION
        ):
            return None
        return cls(dims, raw.get("entities") or [], feature_contract_version=feature_contract)

    def labels(self):
        labels = {0: ["bias"], 1: ["time:hour_sin"], 2: ["time:hour_cos"],
                  3: ["time:dow_sin"], 4: ["time:dow_cos"]}
        idx = 5
        limit = self.dims - HOME_TAIL
        for eid in self.entities:
            for suffix in ENTITY_FEATURES:
                if idx >= limit:
                    break
                labels[idx] = [f"{eid}:{suffix}"]
                idx += 1
        return labels


def _resolve_current_state(entity_id, state_map, temporal, at_ts):
    ts, from_history = _sample_before(temporal, entity_id, at_ts, at_ts)
    if from_history is not None:
        return ts, from_history
    state = (state_map or {}).get(entity_id)
    if state is None:
        return None, None
    event_ts = _sample_event_time(state, fallback=at_ts)
    received = _sample_received(state)
    if event_ts is not None and event_ts <= at_ts + 1e-9 and (received is None or received <= at_ts + 1e-9):
        return event_ts, state
    return None, None


def _lag_state(entity_id, current, temporal, query_ts, at_ts):
    ts, st = _sample_before(temporal, entity_id, query_ts, at_ts)
    if st is not None:
        return ts, st
    changed = _state_ts(current, "last_changed")
    if changed is not None and changed <= query_ts:
        return changed, current
    return None, None


def _is_illuminance_state(state):
    attrs = _attrs(state)
    unit = _normalized_unit(state)
    dc = str(attrs.get("device_class") or "").strip().lower()
    eid = str((state or {}).get("entity_id") or "").lower()
    return unit in ("lx", "lux") or dc == "illuminance" or "illuminance" in eid or "lux" in eid


def _target_power_at(agent, state_map, temporal, query_ts, knowledge_ts):
    if not agent:
        return None
    target = str(agent.get("target_entity") or "")
    if not target:
        return None
    _, target_state = _sample_before(temporal, target, query_ts, knowledge_ts)
    if target_state is None and abs(float(query_ts) - float(knowledge_ts)) <= 1e-9:
        target_state = (state_map or {}).get(target)
    try:
        value = context_module.target_value(target_state, "power")
    except Exception:
        return None
    if value is None or not math.isfinite(float(value)):
        return None
    return 1.0 if float(value) >= .5 else 0.0


def _current_on_run_start(agent, temporal, query_ts, knowledge_ts):
    """First ON sample of the current causal ON run, or None when it cannot be proven."""
    target = str((agent or {}).get("target_entity") or "")
    rows = []
    for ts, state in _samples(temporal, target):
        ts = float(ts)
        if not _eligible_sample(state, ts, query_ts, knowledge_ts):
            continue
        try:
            value = context_module.target_value(state, "power")
        except Exception:
            value = None
        if value is None:
            continue
        rows.append((ts, 1.0 if float(value) >= .5 else 0.0))
    if not rows or rows[-1][1] < .5:
        return None
    start = rows[-1][0]
    saw_off = False
    for ts, value in reversed(rows[:-1]):
        if value < .5:
            saw_off = True
            break
        start = ts
    # If history starts with the lamp already ON there is no causal pre-action baseline.
    return start if saw_off else None


def _last_valid_illuminance_before(entity_id, temporal, query_ts, knowledge_ts):
    for ts, state in reversed(_samples(temporal, entity_id)):
        ts = float(ts)
        if not _eligible_sample(state, ts, query_ts, knowledge_ts):
            continue
        obs = observation_value(state)
        if (obs.get("valid") and obs.get("canonical_unit") == "lx"
                and obs.get("physical_value") is not None):
            return ts, state
    return None, None


def _darkness_observation(state):
    obs = observation_value(state)
    if not (obs.get("valid") and obs.get("canonical_unit") == "lx"
            and obs.get("physical_value") is not None):
        return {"valid": 0.0, "value": 0.0, "physical_value": None,
                "category": (0.0, 0.0, 0.0), "kind": "missing",
                "canonical_unit": "lx"}
    ambient_lux = max(0.0, float(obs["physical_value"]))
    # Log-scale physical illuminance without embedding a policy/benchmark darkness
    # threshold. The important contract change is causal: emitted lamp light is removed.
    # Log compression simply gives indoor low-light ranges useful numeric resolution.
    normalized = clamp(math.log1p(ambient_lux) / math.log1p(1000.0), 0.0, 1.0)
    return {**obs, "value": normalized,
            "physical_value": ambient_lux, "photometric_mode": "ambient_pre_action_v2"}


def _feature_observation(schema, entity_id, state, agent, state_map, temporal,
                         query_ts, knowledge_ts):
    """Return one feature observation under the persisted model's semantic contract."""
    legacy = observation_value(state)
    feature_contract = int(getattr(
        schema, "feature_contract_version", LEGACY_FEATURE_CONTRACT_VERSION
    ))
    fast_light = bool(
        agent and is_fast_reactive_agent(agent)
        and str(agent.get("target_entity") or "").split(".", 1)[0] == "light"
        and str(agent.get("target_property") or "") == "power"
    )
    if feature_contract < FEATURE_CONTRACT_VERSION or not fast_light or not _is_illuminance_state(state):
        return legacy, None

    target_power = _target_power_at(agent, state_map, temporal, query_ts, knowledge_ts)
    if target_power is None:
        return _darkness_observation(None), {
            "mode": "ambient_pre_action_v2", "source": "target_power_unknown"
        }
    if target_power < .5:
        return _darkness_observation(state), {
            "mode": "ambient_pre_action_v2", "source": "current_light_off"
        }

    on_start = _current_on_run_start(agent, temporal, query_ts, knowledge_ts)
    if on_start is None:
        return _darkness_observation(None), {
            "mode": "ambient_pre_action_v2", "source": "unresolved_light_on"
        }
    _, baseline = _last_valid_illuminance_before(
        entity_id, temporal, float(on_start) - 1e-6, knowledge_ts
    )
    if baseline is None:
        return _darkness_observation(None), {
            "mode": "ambient_pre_action_v2", "source": "pre_action_baseline_missing",
            "on_start": float(on_start),
        }
    return _darkness_observation(baseline), {
        "mode": "ambient_pre_action_v2", "source": "pre_action_baseline",
        "on_start": float(on_start),
    }


def build_observation_features(schema, state_map, temporal, at_ts=None, agent=None, excluded_entities=None):
    from datetime import datetime
    at_ts = float(at_ts if at_ts is not None else now_ts())
    dt = datetime.fromtimestamp(at_ts).astimezone()
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0 + dt.microsecond / 3_600_000_000.0
    dow = dt.weekday()
    fast_profile = bool(agent and is_fast_reactive_agent(agent))
    feature_contract = int(getattr(
        schema, "feature_contract_version", LEGACY_FEATURE_CONTRACT_VERSION
    ))
    clock_weight = clamp(float(OPTIONS.get("fast_clock_context_weight", 0.15)), 0.0, 1.0) if fast_profile else 1.0
    vec = {0: 1.0, 1: clock_weight * math.sin(2 * math.pi * hour / 24),
           2: clock_weight * math.cos(2 * math.pi * hour / 24),
           3: clock_weight * math.sin(2 * math.pi * dow / 7),
           4: clock_weight * math.cos(2 * math.pi * dow / 7)}
    labels = schema.labels()
    excluded = set(excluded_entities or ())
    usable = 0
    reconstructable = True
    reasons = []
    entity_meta = {}
    idx = 5
    limit = schema.dims - HOME_TAIL
    if fast_profile:
        lags = tuple(float(x) for x in parse_fast_series_lags())
    else:
        short = float(OPTIONS.get("temporal_short_seconds", 60))
        long = float(OPTIONS.get("temporal_long_seconds", 300))
        lags = (short, long, max(long * 2.0, short))

    for eid in schema.entities:
        if idx >= limit:
            break
        current_ts, current = (None, None) if eid in excluded else _resolve_current_state(eid, state_map, temporal, at_ts)
        obs, photometric = _feature_observation(
            schema, eid, current, agent, state_map, temporal, at_ts, at_ts
        )
        if obs["valid"]:
            usable += 1
        received = _latest_communication(temporal, eid, at_ts, current=current)
        event_ts = _sample_event_time(current, fallback=current_ts) if current is not None else None
        event_age = None if event_ts is None else max(0.0, at_ts - event_ts)
        communication_age = None if received is None else max(0.0, at_ts - received)
        # Lux edges produced by the controlled lamp are downstream effects, not causal
        # context. Contract 2 therefore neutralizes this one timing slot; contract 1
        # retains the exact historical representation for persisted old models.
        edge_ts = (None if photometric is not None else
                   (_last_edge_time(temporal, eid, at_ts, current) if current is not None else None))
        edge_age = None if edge_ts is None else max(0.0, at_ts - edge_ts)
        quality, reporting_mode = _source_quality(current, obs, fast_profile)
        lag_values = []
        lag_coverage = []
        for lag in lags:
            query_ts = at_ts - lag
            _, previous = _lag_state(eid, current, temporal, query_ts, at_ts)
            pobs, _ = _feature_observation(
                schema, eid, previous, agent, state_map, temporal, query_ts, at_ts
            )
            covered = bool(previous is not None and pobs["valid"])
            lag_coverage.append(covered)
            if obs["valid"] and pobs["valid"] and obs["kind"] != "category" and pobs["kind"] != "category":
                lag_values.append(float(obs["value"]) - float(pobs["value"]))
            else:
                lag_values.append(0.0)
        exact_received = received is not None
        numeric_fast_gap = bool(fast_profile and obs["kind"] == "numeric"
                                and (not exact_received or not all(lag_coverage)))
        if obs["valid"] and numeric_fast_gap:
            reconstructable = False
            reasons.append(f"{eid}:high_resolution_numeric_history_unavailable")
            quality = min(quality, 0.35)
        if not obs["valid"]:
            reconstructable = False
            reasons.append(f"{eid}:value_unavailable")
        if received is None:
            reasons.append(f"{eid}:communication_time_unknown")
        fast_light_v2 = bool(
            feature_contract >= FEATURE_CONTRACT_VERSION and fast_profile and agent
            and str(agent.get("target_entity") or "").split(".", 1)[0] == "light"
            and str(agent.get("target_property") or "") == "power"
        )
        if fast_light_v2:
            # Contract 1 encoded nominal transport metadata as repeated positive
            # predictors for every entity. In a diagonal per-action model those nearly
            # constant columns accumulate a class-frequency bias (normally toward OFF).
            # Contract 2 centers nominal health at zero; only actual degradation/missing
            # evidence occupies these slots. Unknown transport timestamps are neutral,
            # not equivalent to "maximally stale".
            valid_feature = 0.0 if obs["valid"] else -1.0
            communication_feature = (
                0.0 if received is None else _age_feature(communication_age)
            )
            event_feature = 0.0 if not obs["valid"] else _age_feature(event_age)
            if not obs["valid"]:
                quality_feature = -1.0
            elif received is None and reporting_mode in ("stateful_sparse", "sparse_numeric"):
                quality_feature = 0.0
            else:
                quality_feature = float(quality) - 1.0
            edge_feature = 0.0 if edge_ts is None else _age_feature(edge_age)
        else:
            valid_feature = float(obs["valid"])
            communication_feature = _age_feature(communication_age)
            event_feature = _age_feature(event_age)
            quality_feature = float(quality)
            edge_feature = _age_feature(edge_age)

        values = (float(obs["value"]), valid_feature, communication_feature,
                  event_feature, quality_feature, float(lag_values[0]),
                  float(lag_values[1]), float(lag_values[2]), edge_feature,
                  float(obs["category"][0]), float(obs["category"][1]), float(obs["category"][2]))
        for value in values:
            if idx >= limit:
                break
            if abs(value) > 1e-12:
                vec[idx] = value
            idx += 1
        entity_meta[eid] = {"kind": obs["kind"], "canonical_unit": obs.get("canonical_unit"),
                            "valid": bool(obs["valid"]), "event_time": event_ts,
                            "received_time": received, "communication_age_seconds": communication_age,
                            "event_age_seconds": event_age, "time_since_edge_seconds": edge_age,
                            "quality": quality, "reporting_mode": reporting_mode,
                            "lag_seconds": list(lags), "lag_coverage": lag_coverage,
                            "photometric": photometric}
    return vec, labels, {"feature_contract_version": feature_contract,
                         "schema_version": SCHEMA_VERSION,
                         "usable_entities": usable, "selected_entities": len(schema.entities),
                         "dimensions": schema.dims,
                         "excluded_controllable_inputs": len(set(schema.entities) & excluded),
                         "reconstruction_complete": bool(reconstructable),
                         "reconstruction_reasons": sorted(set(reasons)),
                         "entity_observations": entity_meta,
                         "time_axis": "event_time_with_received_time_cutoff"}


def policy_features(self, state_map, temporal, at_ts=None):
    excluded = self.context_engine.excluded if self.context_engine else self.excluded_context_entities
    vector, labels, meta = build_observation_features(self.schema, state_map, temporal, at_ts,
                                                       self.agent, excluded_entities=excluded)
    provider = getattr(temporal, "home_context", None) or self.context_engine
    if provider:
        query_ts = at_ts if at_ts is not None else now_ts()
        forecast = provider.forecast(self.agent["target_entity"], query_ts)
        home_known = bool(forecast.get("known"))
        fast_light_v2 = bool(
            int(getattr(self.schema, "feature_contract_version",
                        LEGACY_FEATURE_CONTRACT_VERSION)) >= FEATURE_CONTRACT_VERSION
            and is_fast_reactive_agent(self.agent)
            and str(self.agent.get("target_entity") or "").split(".", 1)[0] == "light"
            and str(self.agent.get("target_property") or "") == "power"
        )
        occupancy_names = {
            "occupancy_now", "occupancy_in_1s", "occupancy_in_3s", "occupancy_in_5s"
        }
        for offset, name in enumerate(HOME_FEATURE_NAMES):
            index = self.dims - HOME_TAIL + offset
            if fast_light_v2 and name in occupancy_names:
                # A fused probability is evidence about a binary action, not a positive
                # magnitude. Center a *known* room belief so occupancy and absence vote
                # symmetrically for the two per-action heads. Unknown stays neutral.
                probability = clamp(float(forecast.get(name, 0.0) or 0.0), 0.0, 1.0)
                value = (2.0 * probability - 1.0) if home_known else 0.0
            elif fast_light_v2 and name == "known":
                # "Known" is transport/observability metadata. Healthy observability is
                # nominal zero; only unknown context carries a negative diagnostic signal.
                value = 0.0 if home_known else -1.0
            else:
                value = (1.0 if home_known else 0.0) if name == "known" else float(
                    forecast.get(name, 0.0) or 0.0
                )
            if abs(value) > 1e-12:
                vector[index] = value
            else:
                vector.pop(index, None)
            labels[index] = ["home:" + name]
        meta["home_forecast"] = forecast
        meta["home_known"] = home_known
        meta["home_feature_encoding"] = (
            "signed_occupancy_v2" if fast_light_v2 else "legacy_probability_v1"
        )
    return vector, labels, meta


def teaching_signature(policy, states, temporal, timestamp):
    if not list(policy.schema.entities):
        return None
    features, labels, meta = policy.features(states, temporal, at_ts=timestamp)
    if not meta.get("reconstruction_complete", False):
        return None
    result = {" / ".join(labels[i]): float(features.get(i, 0.0))
              for i in sorted(labels) if i > 0 and i < policy.dims}
    if policy.agent.get("target_property") == "option_index":
        options = (states.get(policy.agent["target_entity"], {}).get("attributes") or {}).get("options")
        if not options:
            return None
        result["target_options:" + json.dumps(options)] = 1.0
    return result


def register_live_sample(temporal, entity_id, state, event_time, received_time, source):
    dq = getattr(temporal, "samples", {}).get(entity_id)
    if not dq:
        return False
    event_time = float(event_time)
    received_time = float(received_time)
    ts, sample = dq[-1]
    if abs(float(ts) - event_time) > 1e-6:
        return False
    attrs = sample.get("attributes")
    if not isinstance(attrs, dict):
        attrs = {}
        sample["attributes"] = attrs
    previous_received = attrs.get("__hm_received_time", sample.get("_hm_received_time"))
    try:
        previous_received = float(previous_received)
    except (TypeError, ValueError):
        previous_received = 0.0
    attrs["__hm_event_time"] = event_time
    attrs["__hm_received_time"] = max(previous_received, received_time)
    attrs["__hm_feature_source"] = str(source)
    attrs["__hm_quality"] = 1.0
    return True


class FeatureJournal:
    def __init__(self, store, clock=now_ts, *, retention_hours=FAST_RETENTION_HOURS,
                 max_events_per_entity=FAST_MAX_EVENTS_PER_ENTITY,
                 window_retention_days=WINDOW_RETENTION_DAYS,
                 max_windows_per_agent=WINDOW_MAX_PER_AGENT,
                 global_event_limit=GLOBAL_EVENT_LIMIT):
        self.store = store
        self.clock = clock
        self.retention_hours = float(retention_hours)
        self.max_events_per_entity = int(max_events_per_entity)
        self.window_retention_days = float(window_retention_days)
        self.max_windows_per_agent = int(max_windows_per_agent)
        self.global_event_limit = int(global_event_limit)
        self._writes = 0
        self._migrate()

    def _migrate(self):
        with self.store.lock, self.store.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS feature_observation_events (
                    event_key TEXT PRIMARY KEY, contract_version INTEGER NOT NULL,
                    entity_id TEXT NOT NULL, event_time REAL NOT NULL, received_time REAL NOT NULL,
                    state TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', last_changed TEXT,
                    last_updated TEXT, source TEXT NOT NULL, quality REAL NOT NULL,
                    protected_until REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_feature_obs_entity_time
                    ON feature_observation_events(entity_id,event_time,received_time);
                CREATE INDEX IF NOT EXISTS idx_feature_obs_received
                    ON feature_observation_events(received_time);
                CREATE TABLE IF NOT EXISTS feature_windows (
                    window_id TEXT PRIMARY KEY, contract_version INTEGER NOT NULL,
                    agent_id TEXT NOT NULL, anchor_time REAL NOT NULL, start_time REAL NOT NULL,
                    end_time REAL NOT NULL, kind TEXT NOT NULL, created_time REAL NOT NULL,
                    protected_until REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_feature_windows_agent
                    ON feature_windows(agent_id,anchor_time);
                CREATE TABLE IF NOT EXISTS feature_window_entities (
                    window_id TEXT NOT NULL, entity_id TEXT NOT NULL,
                    PRIMARY KEY(window_id,entity_id));
                CREATE INDEX IF NOT EXISTS idx_feature_window_entity
                    ON feature_window_entities(entity_id,window_id);
            """)

    @staticmethod
    def _compact_attributes(state):
        allowed = {"unit_of_measurement", "device_class", "friendly_name", "state_class", "options"}
        return {k: v for k, v in dict(_attrs(state)).items() if k in allowed}

    def _protection(self, c, entity_id, event_time):
        row = c.execute("""SELECT MAX(w.protected_until) FROM feature_windows w
            JOIN feature_window_entities e ON e.window_id=w.window_id
            WHERE e.entity_id=? AND w.start_time<=? AND w.end_time>=?""",
            (str(entity_id), float(event_time), float(event_time))).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def record(self, entity_id, state, *, event_time, received_time, source, event_key=None, quality=1.0):
        if not entity_id or state is None:
            return None
        event_time, received_time = float(event_time), float(received_time)
        context = (state or {}).get("context") or {}
        if event_key is None:
            if source == "ha_state_changed":
                event_key = stable_event_id(entity_id, event_time, context.get("id"), context.get("parent_id"), state)
            else:
                raw = f"{entity_id}|{event_time:.9f}|{received_time:.9f}|{source}|{state.get('state')}"
                event_key = "obs:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        attrs = json.dumps(self._compact_attributes(state), separators=(",", ":"), ensure_ascii=False)
        with self.store.lock, self.store.conn() as c:
            protected = self._protection(c, entity_id, event_time)
            c.execute("""INSERT INTO feature_observation_events
                (event_key,contract_version,entity_id,event_time,received_time,state,attributes_json,
                 last_changed,last_updated,source,quality,protected_until)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET
                  received_time=MIN(feature_observation_events.received_time,excluded.received_time),
                  protected_until=MAX(feature_observation_events.protected_until,excluded.protected_until),
                  quality=MAX(feature_observation_events.quality,excluded.quality)""",
                (event_key, CONTRACT_VERSION, str(entity_id), event_time, received_time,
                 None if state.get("state") is None else str(state.get("state")), attrs,
                 state.get("last_changed"), state.get("last_updated"), str(source),
                 clamp(float(quality), 0.0, 1.0), protected))
        self._writes += 1
        if self._writes % 128 == 0:
            self.prune(entity_id=entity_id)
        return event_key

    def open_window(self, window_id, agent_id, entities, anchor_time, kind,
                    before=WINDOW_BEFORE_SECONDS, after=WINDOW_AFTER_SECONDS):
        entities = sorted(set(str(e) for e in entities if e))
        if not entities:
            return None
        anchor, created = float(anchor_time), float(self.clock())
        protected_until = created + self.window_retention_days * 86400.0
        start, end = anchor - float(before), anchor + float(after)
        with self.store.lock, self.store.conn() as c:
            c.execute("""INSERT OR IGNORE INTO feature_windows
                (window_id,contract_version,agent_id,anchor_time,start_time,end_time,kind,created_time,protected_until)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(window_id), CONTRACT_VERSION, str(agent_id), anchor, start, end,
                 str(kind), created, protected_until))
            c.executemany("INSERT OR IGNORE INTO feature_window_entities(window_id,entity_id) VALUES(?,?)",
                          [(str(window_id), eid) for eid in entities])
            placeholders = ",".join("?" for _ in entities)
            c.execute(f"""UPDATE feature_observation_events SET protected_until=MAX(protected_until,?)
                        WHERE entity_id IN ({placeholders}) AND event_time>=? AND event_time<=?""",
                      [protected_until, *entities, start, end])
            stale = c.execute("""SELECT window_id FROM feature_windows WHERE agent_id=?
                               ORDER BY anchor_time DESC LIMIT -1 OFFSET ?""",
                              (str(agent_id), self.max_windows_per_agent)).fetchall()
            if stale:
                ids = [r[0] for r in stale]
                marks = ",".join("?" for _ in ids)
                c.execute(f"DELETE FROM feature_window_entities WHERE window_id IN ({marks})", ids)
                c.execute(f"DELETE FROM feature_windows WHERE window_id IN ({marks})", ids)
        return str(window_id)

    @staticmethod
    def normalized_row(row):
        attrs = json.loads(row.get("attributes_json") or "{}")
        attrs.update({"__hm_received_time": float(row["received_time"]),
                      "__hm_event_time": float(row["event_time"]),
                      "__hm_feature_source": str(row.get("source") or "feature_event"),
                      "__hm_quality": float(row.get("quality") or 0.0),
                      "__hm_last_changed": row.get("last_changed"),
                      "__hm_last_updated": row.get("last_updated")})
        return {"id": "feature:" + str(row["event_key"]), "entity_id": row["entity_id"],
                "ts": float(row["event_time"]), "state": row.get("state"),
                "attributes_json": json.dumps(attrs, separators=(",", ":"), ensure_ascii=False),
                "context_user_id": None, "source": "feature_observation_v12",
                "_feature_received_time": float(row["received_time"])}

    def prune(self, entity_id=None):
        now = float(self.clock())
        cutoff = now - self.retention_hours * 3600.0
        with self.store.lock, self.store.conn() as c:
            c.execute("DELETE FROM feature_window_entities WHERE window_id IN "
                      "(SELECT window_id FROM feature_windows WHERE protected_until<?)", (now,))
            c.execute("DELETE FROM feature_windows WHERE protected_until<?", (now,))
            c.execute("DELETE FROM feature_observation_events WHERE received_time<? AND protected_until<?",
                      (cutoff, now))
            if entity_id:
                extra = c.execute("""SELECT event_key FROM feature_observation_events
                    WHERE entity_id=? AND protected_until<? ORDER BY received_time DESC LIMIT -1 OFFSET ?""",
                    (str(entity_id), now, self.max_events_per_entity)).fetchall()
                if extra:
                    c.executemany("DELETE FROM feature_observation_events WHERE event_key=?", [(r[0],) for r in extra])
            total = int(c.execute("SELECT COUNT(*) FROM feature_observation_events").fetchone()[0] or 0)
            if total > self.global_event_limit:
                excess = total - self.global_event_limit
                doomed = c.execute("""SELECT event_key FROM feature_observation_events
                    ORDER BY CASE WHEN protected_until>? THEN 1 ELSE 0 END, received_time ASC LIMIT ?""",
                    (now, excess)).fetchall()
                c.executemany("DELETE FROM feature_observation_events WHERE event_key=?", [(r[0],) for r in doomed])
        return True

    def stats(self):
        with self.store.conn() as c:
            events = int(c.execute("SELECT COUNT(*) FROM feature_observation_events").fetchone()[0] or 0)
            windows = int(c.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[0] or 0)
        return {"contract_version": CONTRACT_VERSION, "events": events, "windows": windows,
                "retention_hours": self.retention_hours,
                "max_events_per_entity": self.max_events_per_entity,
                "window_retention_days": self.window_retention_days,
                "max_windows_per_agent": self.max_windows_per_agent,
                "global_event_limit": self.global_event_limit}


class ObservationSQLiteTemporalTracker(replay_module.SQLiteTemporalTracker):
    """Incremental replay view merging long-term archive with the bounded fast journal.

    A fast observation becomes visible only when both its event_time and received_time are
    <= the replay query time. Forward advancement therefore consumes rows that became
    newly eligible by either time axis; late packets are merged back into the bounded
    per-entity history without rewinding the whole tracker.
    """

    @staticmethod
    def _row_order(row):
        # v12 merged archive/fast-journal rows by event time, received time and string id.
        # Keep that stable even though the base archive-only tracker uses numeric DB ids.
        return (
            float(row.get("ts") or 0.0),
            float(row.get("_feature_received_time") or 0.0),
            str(row.get("id") or ""),
        )

    def _feature_bulk_before(self, entity_ids, ts, count):
        result = []
        count = max(1, int(count))
        for ids in self._chunks(entity_ids):
            parts, params = [], []
            for eid in ids:
                parts.append(
                    "SELECT * FROM ("
                    "SELECT event_key,entity_id,event_time,received_time,state,attributes_json,"
                    "last_changed,last_updated,source,quality "
                    "FROM feature_observation_events "
                    "WHERE entity_id=? AND event_time<=? AND received_time<=? "
                    "ORDER BY event_time DESC,received_time DESC,event_key DESC LIMIT ?)"
                )
                params.extend([eid, float(ts), float(ts), count])
            if not parts:
                continue
            # Each branch is already bounded newest-first. The caller performs the one
            # authoritative causal merge/sort in Python, so an outer SQLite temp sort is
            # pure overhead and can create long CPU bursts on a Raspberry Pi.
            sql = " UNION ALL ".join(parts)
            raw = self._fetch_rows(sql, params)
            TRAINING_BUDGET.checkpoint("temporal_feature_before_query")
            result.extend(FeatureJournal.normalized_row(row) for row in raw)
        result.sort(key=self._row_order)
        return result

    def _feature_interval_rows(self, entity_ids, lo, hi):
        """Rows that became causally visible since the previous replay timestamp."""
        if float(hi) <= float(lo):
            return []
        result = []
        for ids in self._chunks(entity_ids):
            parts, params = [], []
            for eid in ids:
                parts.append(
                    "SELECT * FROM ("
                    "SELECT event_key,entity_id,event_time,received_time,state,attributes_json,"
                    "last_changed,last_updated,source,quality "
                    "FROM feature_observation_events "
                    "WHERE entity_id=? AND event_time<=? AND received_time<=? "
                    "AND (event_time>? OR received_time>?) "
                    "ORDER BY event_time DESC,received_time DESC,event_key DESC LIMIT ?)"
                )
                params.extend([
                    eid, float(hi), float(hi), float(lo), float(lo),
                    self.HISTORY_SAMPLES,
                ])
            if not parts:
                continue
            sql = " UNION ALL ".join(parts)
            raw = self._fetch_rows(sql, params)
            TRAINING_BUDGET.checkpoint("temporal_feature_forward_query")
            result.extend(FeatureJournal.normalized_row(row) for row in raw)
        result.sort(key=self._row_order)
        return result

    def _compact_rows(self, rows, count):
        # Preserve the v12 merge contract: same event-time/state prefers the fast sample
        # with the latest received-time metadata. Different states at the same timestamp
        # remain ordered, and TemporalHistory will expose the final causal row.
        merged = {}
        for row in rows:
            key = (round(float(row["ts"]), 9), str(row.get("state")))
            previous = merged.get(key)
            if previous is None:
                merged[key] = row
                continue
            previous_received = float(previous.get("_feature_received_time") or 0.0)
            current_received = float(row.get("_feature_received_time") or 0.0)
            if current_received > previous_received:
                merged[key] = row
            elif current_received == previous_received and str(row.get("id")) > str(previous.get("id")):
                merged[key] = row
        ordered = sorted(merged.values(), key=self._row_order)
        return ordered[-max(1, int(count)):]

    def _bulk_before(self, entity_ids, ts, count=replay_module.SQLiteTemporalTracker.HISTORY_SAMPLES):
        base = super()._bulk_before(entity_ids, ts, count)
        fast = self._feature_bulk_before(entity_ids, ts, count)
        grouped = {}
        for row in [*base, *fast]:
            grouped.setdefault(row["entity_id"], []).append(row)
        result = []
        for eid in sorted(grouped):
            result.extend(self._compact_rows(grouped[eid], count))
        result.sort(key=self._row_order)
        return result

    def _interval_rows(self, entity_ids, lo, hi):
        base = super()._interval_rows(entity_ids, lo, hi)
        fast = self._feature_interval_rows(entity_ids, lo, hi)
        # Do not truncate here. _set_entity_rows merges this delta with the previous
        # bounded cache and then applies the exact 64-sample cap.
        grouped = {}
        for row in [*base, *fast]:
            grouped.setdefault(row["entity_id"], []).append(row)
        result = []
        for eid in sorted(grouped):
            rows = grouped[eid]
            result.extend(self._compact_rows(rows, max(1, len(rows))))
        result.sort(key=self._row_order)
        return result

    def _home_seed_interval_rows(self, entity_ids, lo, hi):
        base = super()._home_seed_interval_rows(entity_ids, lo, hi)
        fast = self._feature_interval_rows(entity_ids, lo, hi)
        grouped = {}
        for row in [*base, *fast]:
            grouped.setdefault(row["entity_id"], []).append(row)
        out = []
        for eid in sorted(grouped):
            rows = grouped[eid]
            out.extend(self._compact_rows(rows, max(1, len(rows))))
        out.sort(key=self._row_order)
        return out

    def _edges(self, eid, lo, hi):
        # Keep the established v12 edge semantics while routing the as-of reconstruction
        # through the new bulk path. The 512-row cap is unchanged.
        rows = self._before(eid, hi, 512)
        previous = None
        for row in rows:
            if float(row["ts"]) <= float(lo):
                previous = row
                continue
            if float(row["ts"]) > float(hi):
                break
            cur_state = context_module.archived_state(row)
            cur = observation_value(cur_state)
            if previous is not None:
                prev = observation_value(context_module.archived_state(previous))
                if cur["valid"] and prev["valid"] and cur["kind"] != "category" and prev["kind"] != "category":
                    if cur["value"] > .25 and prev["value"] <= .25:
                        yield float(row["ts"]), True
                    elif cur["value"] < -.25 and prev["value"] >= -.25:
                        yield float(row["ts"]), False
            if cur["valid"]:
                previous = row
            TRAINING_BUDGET.checkpoint("temporal_edge_scan")

def _watched_fast_entities(engine, store):
    entities = set()
    for agent in store.list_agent_configs():
        if not agent.get("enabled") or not is_fast_reactive_agent(agent):
            continue
        policy = engine.models.get(agent["id"])
        if policy is not None:
            entities.update(policy.schema.entities)
        else:
            raw = store.get_model(agent["id"]) or {}
            entities.update((raw.get("schema") or {}).get("entities") or [])
    return entities


def _patch_teaching_point_context():
    def point_context(self, engine, agent, timestamp, policy=None):
        policy = policy or self.clone(engine, agent)
        watched = set(policy.schema.entities) | {agent["target_entity"]}
        max_lag = max([0.0, *[float(x) for x in parse_fast_series_lags()],
                       float(OPTIONS.get("temporal_short_seconds", 60)),
                       float(OPTIONS.get("temporal_long_seconds", 300))])
        tracker = ObservationSQLiteTemporalTracker(
            self.store, watched, engine.context,
            float(timestamp) - max(30.0, max_lag * 2.0), float(timestamp))
        try:
            tracker.advance(float(timestamp))
            states = dict(tracker.state_map)
            temporal = tracker.history
        finally:
            tracker.close()
        return states, temporal, policy
    teaching_module.Teaching.point_context = point_context


def _repair_current_contract_state(store, agent):
    """Repair only the 0.14.21 startup false-positive without reviving stale config.

    The old storage bootstrap invalidated every model that wasn't exactly v10/schema11
    before this observation contract was installed. A genuine config change uses
    Store.set_training_state(), which clears benchmark_score/samples; therefore a
    current-contract model with preserved recorded-behaviour benchmark evidence can be
    distinguished from a real needs_retrain state without guessing.
    """
    if str(agent.get("training_state") or "") != "needs_retrain":
        return None
    if agent.get("benchmark_score") is None or int(agent.get("benchmark_samples") or 0) <= 0:
        return None
    if str(agent.get("benchmark_source") or "") != "recorded-behaviour":
        return None

    detail = dict(agent.get("benchmark_detail") or {})
    score = float(agent.get("benchmark_score") or 0.0)
    samples = int(agent.get("benchmark_samples") or 0)
    threshold = float(detail.get("threshold", OPTIONS.get("candidate_benchmark_threshold", 0.78)))
    minimum = int(detail.get("minimum_samples", OPTIONS.get("candidate_benchmark_min_samples", 12)))
    class_coverage = bool(detail.get("class_coverage", False))
    state = "qualified" if samples >= minimum and class_coverage and score > threshold else "paused"

    with store.lock, store.conn() as c:
        c.execute(
            """UPDATE agents SET training_state=?, mode='shadow',
               training_cursor_ts=COALESCE(training_window_end_ts,training_cursor_ts),
               training_progress=CASE WHEN training_window_end_ts IS NOT NULL THEN 1.0 ELSE training_progress END,
               training_updated_at=? WHERE id=?""",
            (state, iso_now(), agent["id"]),
        )
    store.event(
        agent["id"], "info", "current_model_state_repaired",
        "Restored completed current-contract model after legacy startup invalidation",
        {
            "policy_version": POLICY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "training_state": state,
            "mode": "shadow",
            "benchmark_score": score,
            "benchmark_samples": samples,
        },
    )
    return state


def _migrate_models(core):
    store, engine = core.STORE, core.ENGINE
    marker = f"observation_feature_schema:{SCHEMA_VERSION}:policy:{POLICY_VERSION}"
    changed = []
    repaired = []
    for agent in store.list_agent_configs():
        raw = store.get_model(agent["id"]) or {}
        if not raw:
            continue
        schema = raw.get("schema") or {}
        current = int(raw.get("version") or 0) == POLICY_VERSION and int(schema.get("version") or 0) == SCHEMA_VERSION
        if current:
            state = _repair_current_contract_state(store, agent)
            if state is not None:
                repaired.append({"agent_id": agent["id"], "training_state": state})
            continue
        with store.lock, store.conn() as c:
            c.execute("""UPDATE agents SET training_state='needs_retrain',mode='paused',
                       training_progress=0,training_updated_at=? WHERE id=?""",
                      (iso_now(), agent["id"]))
        changed.append(agent["id"])
    if engine is not None:
        engine.models.clear()
        if repaired:
            engine.wake_event.set()
    store.meta_set("observation_feature_contract", marker)
    if changed:
        store.event(None, "warning", "observation_schema_migration",
                    f"Feature schema v{SCHEMA_VERSION} requires explicit retraining for {len(changed)} agent(s); old model rows preserved",
                    {"agents": changed, "schema_version": SCHEMA_VERSION,
                     "policy_version": POLICY_VERSION,
                     "migration": "versioned_no_vector_reinterpretation"})
    if repaired:
        store.event(None, "info", "observation_schema_repair",
                    f"Restored {len(repaired)} current-contract agent(s) incorrectly invalidated by legacy startup migration",
                    {"agents": repaired, "schema_version": SCHEMA_VERSION,
                     "policy_version": POLICY_VERSION})
    return changed


def install(core):
    engine, store = core.ENGINE, core.STORE
    if engine is None or store is None or getattr(engine, "_observation_contract_installed", False):
        return engine
    policy_module.ExplicitFeatureSchema = FeatureSchemaV12
    context_module.ExplicitFeatureSchema = FeatureSchemaV12
    policy_module.build_explicit_features = build_observation_features
    teaching_module.build_explicit_features = build_observation_features
    policy_module.FEATURE_NAMES = HOME_FEATURE_NAMES
    policy_module.MultiHorizonPolicy.VERSION = POLICY_VERSION
    policy_module.MultiHorizonPolicy.features = policy_features
    teaching_module.signature = teaching_signature
    replay_module.SQLiteTemporalTracker = ObservationSQLiteTemporalTracker
    history_module.SQLiteTemporalTracker = ObservationSQLiteTemporalTracker
    _patch_teaching_point_context()

    journal = FeatureJournal(store)
    engine.feature_journal = journal
    engine._observation_contract_installed = True
    engine._observation_watch_cache = (0.0, set())

    def watched():
        ts, values = engine._observation_watch_cache
        now = now_ts()
        if now - ts >= 2.0:
            values = _watched_fast_entities(engine, store)
            engine._observation_watch_cache = (now, values)
        return values

    original_on_state_changed = engine.on_state_changed
    def on_state_changed(data):
        entity_id = (data or {}).get("entity_id")
        state = (data or {}).get("new_state")
        received = now_ts()
        event_time = parse_ts((state or {}).get("last_updated") or (state or {}).get("last_changed")) or received
        # Engine.on_state_changed owns the live temporal acceptance rule, including its
        # monotonic last_updated guard. Persist high-resolution evidence only when that
        # exact event made it into the live temporal buffer; otherwise replay would learn
        # from a stale/out-of-order sample that live inference never observed.
        result = original_on_state_changed(data)
        accepted = False
        if entity_id and state is not None:
            accepted = register_live_sample(engine.temporal_history, entity_id, state, event_time,
                                            received, "ha_state_changed")
        if accepted and entity_id in watched():
            journal.record(entity_id, state, event_time=event_time, received_time=received,
                           source="ha_state_changed")
        return result
    engine.on_state_changed = on_state_changed

    original_refresh_states = engine.refresh_states
    def refresh_states():
        states = original_refresh_states()
        received = now_ts()
        selected = watched()
        for entity_id in selected:
            state = (states or {}).get(entity_id)
            if not state:
                continue
            event_time = parse_ts(state.get("last_updated") or state.get("last_changed")) or received
            journal.record(entity_id, state, event_time=event_time, received_time=received,
                           source="ha_poll_confirmation",
                           event_key=("poll:" + hashlib.sha256(
                               f"{entity_id}|{event_time:.9f}|{received:.3f}".encode("utf-8")
                           ).hexdigest()[:32]))
            register_live_sample(engine.temporal_history, entity_id, state, event_time, received,
                                 "ha_poll_confirmation")
        return states
    engine.refresh_states = refresh_states

    original_submit = engine.executor.submit
    def submit(intent, features=None, action_index=None):
        agent = store.get_agent_config(intent.agent_id)
        policy = engine.models.get(intent.agent_id)
        if agent and policy and is_fast_reactive_agent(agent):
            journal.open_window("decision:" + str(intent.intent_id), intent.agent_id,
                                policy.schema.entities, intent.created_at, "decision")
        return original_submit(intent, features, action_index)
    engine.executor.submit = submit

    original_process_agent = engine.process_agent
    def process_agent(agent, state_map, changed_entities=None):
        if agent.get("target_entity") in set(changed_entities or ()) and is_fast_reactive_agent(agent):
            latest = getattr(engine, "_provenance_latest_events", {}).get(agent["target_entity"])
            if latest and getattr(engine, "provenance", None):
                event = engine.provenance.event(latest[1]) or {}
                if event.get("origin") in {"user", "user_intent"}:
                    policy = engine.models.get(agent["id"])
                    if policy is None:
                        with suppress(Exception):
                            policy = engine.policy(agent)
                    if policy is not None:
                        journal.open_window("correction:" + str(latest[1]) + ":" + str(agent["id"]),
                                            agent["id"], policy.schema.entities,
                                            float(event.get("event_time") or now_ts()), "correction")
        return original_process_agent(agent, state_map, changed_entities)
    engine.process_agent = process_agent

    migrated = _migrate_models(core)
    store.event(None, "info", "observation_feature_contract_ready",
                "Observation feature schema v12 enabled for live, replay and Teach",
                {"contract_version": CONTRACT_VERSION, "schema_version": SCHEMA_VERSION,
                 "policy_version": POLICY_VERSION, "entity_features": list(ENTITY_FEATURES),
                 "home_features": list(HOME_FEATURE_NAMES), "buffer": journal.stats(),
                 "migrated_agents": len(migrated),
                 "install_order": "after_provenance_before_workers"})
    return engine