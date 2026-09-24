"""Versioned global observation space and deterministic per-agent feature masks.

Stage 2 does not replace the active DiagonalLinUCB feature vector. This module defines
the semantic observation contract for future backends while reusing established
HomeMind entity/context selection. Historical reconstruction is causal by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math

from context import (
    context_scalar,
    controllable_context_exclusions,
    electrical_context_exclusions,
    is_context_candidate_entity,
    select_context_entities,
)
from home_state import FEATURE_NAMES
from settings import OPTIONS, parse_ts

OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_CONTRACT_VERSION = 1
ENTITY_DESCRIPTORS = (
    ("value", 0.0),
    ("delta_1s", 1.0),
    ("delta_10s", 10.0),
    ("delta_60s", 60.0),
    ("freshness", 0.0),
    ("available", 0.0),
)
GLOBAL_FEATURES = (
    "time:hour_sin",
    "time:hour_cos",
    "time:dow_sin",
    "time:dow_cos",
)
HOME_FEATURES = tuple("home:" + name for name in FEATURE_NAMES)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def observation_schema_id():
    payload = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "entity_descriptors": ENTITY_DESCRIPTORS,
        "global_features": GLOBAL_FEATURES,
        "home_features": HOME_FEATURES,
    }
    return "obs-v%s:%s" % (OBSERVATION_SCHEMA_VERSION, _digest(payload)[:16])


def _area_for(registry, entity_id):
    return dict((registry or {}).get(entity_id) or {}).get("area_id")


def _friendly_name(state, entity_id):
    attrs = dict((state or {}).get("attributes") or {})
    return str(attrs.get("friendly_name") or entity_id)


def global_observation_catalog(state_map, registry):
    """Installation-wide possible feature catalog after hard safety exclusions."""
    excluded_control, control_meta = controllable_context_exclusions(state_map, registry)
    excluded_electrical, electrical_meta = electrical_context_exclusions(state_map, registry)
    excluded = set(excluded_control) | set(excluded_electrical)
    entities = []
    features = []
    for entity_id in sorted((state_map or {}).keys()):
        state = (state_map or {}).get(entity_id)
        if not is_context_candidate_entity(entity_id, state, excluded):
            continue
        area_id = _area_for(registry, entity_id)
        entities.append(entity_id)
        for descriptor, lag_seconds in ENTITY_DESCRIPTORS:
            feature_id = "entity:%s:%s" % (entity_id, descriptor)
            features.append({
                "id": feature_id,
                "name": "%s · %s" % (_friendly_name(state, entity_id), descriptor),
                "kind": "entity",
                "entity_id": entity_id,
                "area_id": area_id,
                "descriptor": descriptor,
                "lag_seconds": lag_seconds,
            })
    for feature_id in GLOBAL_FEATURES:
        features.append({
            "id": feature_id, "name": feature_id, "kind": "global",
            "entity_id": None, "area_id": None,
            "descriptor": feature_id.split(":", 1)[1], "lag_seconds": 0.0,
        })
    for feature_id in HOME_FEATURES:
        features.append({
            "id": feature_id, "name": feature_id, "kind": "home",
            "entity_id": None, "area_id": None,
            "descriptor": feature_id.split(":", 1)[1], "lag_seconds": 0.0,
        })
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "schema_id": observation_schema_id(),
        "eligible_entities": entities,
        "eligible_entity_count": len(entities),
        "feature_count": len(features),
        "features": features,
        **control_meta,
        **electrical_meta,
    }


@dataclass(frozen=True)
class ObservationMask:
    schema_id: str
    mask_version: int
    feature_ids: tuple
    features: tuple
    selected_entities: tuple
    global_feature_count: int
    missing_feature_count: int

    @property
    def mask_id(self):
        return _digest({
            "schema_id": self.schema_id,
            "mask_version": self.mask_version,
            "feature_ids": list(self.feature_ids),
        })

    def export(self):
        return {
            "schema_id": self.schema_id,
            "mask_version": self.mask_version,
            "mask_id": self.mask_id,
            "feature_ids": list(self.feature_ids),
            "features": [dict(row) for row in self.features],
            "selected_entities": list(self.selected_entities),
            "selected_feature_count": len(self.feature_ids),
            "global_feature_count": int(self.global_feature_count),
            "missing_feature_count": int(self.missing_feature_count),
        }

    @classmethod
    def from_export(cls, raw):
        raw = dict(raw or {})
        expected_schema = observation_schema_id()
        if raw.get("schema_id") != expected_schema:
            raise ValueError("NEEDS_RETRAIN: incompatible observation feature schema")
        version = int(raw.get("mask_version") or 0)
        if version != 1:
            raise ValueError("NEEDS_RETRAIN: incompatible observation feature mask")
        features = tuple(dict(x) for x in (raw.get("features") or ()))
        feature_ids = tuple(str(x) for x in (raw.get("feature_ids") or ()))
        if tuple(str(x.get("id")) for x in features) != feature_ids:
            raise ValueError("NEEDS_RETRAIN: observation feature mask payload mismatch")
        obj = cls(
            schema_id=expected_schema,
            mask_version=version,
            feature_ids=feature_ids,
            features=features,
            selected_entities=tuple(raw.get("selected_entities") or ()),
            global_feature_count=int(raw.get("global_feature_count") or 0),
            missing_feature_count=int(raw.get("missing_feature_count") or 0),
        )
        if raw.get("mask_id") and raw.get("mask_id") != obj.mask_id:
            raise ValueError("NEEDS_RETRAIN: observation feature mask checksum mismatch")
        return obj


_DESCRIPTOR_BIAS = {
    "value": 60.0, "delta_1s": 52.0, "delta_10s": 46.0,
    "delta_60s": 38.0, "freshness": 24.0, "available": 18.0,
}


def _missing_entity(state, entity_id, agent):
    return state is None or context_scalar(entity_id, state, agent) is None


def _selection_reference_ts(state_map):
    """Stable snapshot watermark used only by the Stage-2 feature-mask selector."""
    stamps = []
    for state in (state_map or {}).values():
        if not state:
            continue
        stamp = parse_ts(state.get("last_updated")) or parse_ts(state.get("last_changed"))
        if stamp is not None:
            stamps.append(float(stamp))
    return max(stamps) if stamps else 0.0


def select_observation_mask(agent, state_map, registry, hint_entities, relevance_scores=None, *, max_features=None):
    """Build deterministic feature-level mask by reusing established entity ranking."""
    catalog = global_observation_catalog(state_map, registry)
    max_features = int(max_features if max_features is not None else OPTIONS.get("observation_selected_features", 96))
    max_features = max(16, min(128, max_features))
    reserved = len(GLOBAL_FEATURES) + len(HOME_FEATURES)
    entity_budget = max(1, (max_features - reserved) // len(ENTITY_DESCRIPTORS))
    selection_reference_ts = _selection_reference_ts(state_map)
    selected_entities, meta = select_context_entities(
        agent, state_map, registry, hint_entities,
        max_entities=entity_budget, relevance_scores=relevance_scores,
        reference_ts=selection_reference_ts,
    )
    catalog_by_id = {row["id"]: row for row in catalog["features"]}
    rows = []
    for feature_id in GLOBAL_FEATURES:
        row = dict(catalog_by_id[feature_id])
        row.update({"score": 1000000.0, "selection_reason": ["global-time"], "entity_rank": 0})
        rows.append(row)
    target_area = _area_for(registry, agent.get("target_entity"))
    for feature_id in HOME_FEATURES:
        row = dict(catalog_by_id[feature_id])
        row.update({
            "score": 999000.0,
            "selection_reason": ["target-area-home-context"],
            "entity_rank": 0,
            "area_id": target_area,
            "target_entity": agent.get("target_entity"),
        })
        rows.append(row)
    scores = dict(meta.get("selection_scores") or {})
    ranks = dict(meta.get("selection_rank") or {})
    reasons = dict(meta.get("selection_reasons") or {})
    for entity_id in selected_entities:
        base_score = float(scores.get(entity_id, 0.0))
        entity_rank = int(ranks.get(entity_id, len(selected_entities) + 1))
        for descriptor, _lag in ENTITY_DESCRIPTORS:
            feature_id = "entity:%s:%s" % (entity_id, descriptor)
            source = catalog_by_id.get(feature_id)
            if source is None:
                continue
            row = dict(source)
            row.update({
                "score": round(base_score + _DESCRIPTOR_BIAS[descriptor], 6),
                "selection_reason": list(reasons.get(entity_id) or ()) + ["descriptor:" + descriptor],
                "entity_rank": entity_rank,
            })
            rows.append(row)
    globals_len = reserved
    entity_rows = rows[globals_len:]
    descriptor_order = {name: idx for idx, (name, _lag) in enumerate(ENTITY_DESCRIPTORS)}
    entity_rows.sort(key=lambda row: (
        int(row.get("entity_rank") or 999999),
        descriptor_order.get(row.get("descriptor"), 999),
        str(row["id"]),
    ))
    rows = (rows[:globals_len] + entity_rows)[:max_features]
    selected_ids = tuple(row["id"] for row in rows)
    selected_entity_ids = tuple(dict.fromkeys(row["entity_id"] for row in rows if row.get("entity_id")))
    missing_entities = {
        eid for eid in selected_entity_ids
        if _missing_entity((state_map or {}).get(eid), eid, agent)
    }
    missing_feature_count = sum(
        1 for row in rows
        if row.get("entity_id") in missing_entities and row.get("descriptor") != "available"
    )
    mask = ObservationMask(
        schema_id=catalog["schema_id"], mask_version=1,
        feature_ids=selected_ids, features=tuple(rows),
        selected_entities=selected_entity_ids,
        global_feature_count=int(catalog["feature_count"]),
        missing_feature_count=missing_feature_count,
    )
    diagnostics = {
        **mask.export(),
        "eligible_entity_count": catalog["eligible_entity_count"],
        "considered_entities": int(meta.get("considered_entities") or 0),
        "selection_profile": "feature-level-v1",
        "selection_target_range": [32, 128],
        "selection_reference_ts": selection_reference_ts,
        "entity_selection": meta,
        "hot_path_active": False,
    }
    return mask, diagnostics


def _state_at(entity_id, state_map, temporal, at_ts):
    if temporal is not None and callable(getattr(temporal, "previous", None)):
        previous = temporal.previous(entity_id, float(at_ts))
        if previous is not None:
            return previous
    state = (state_map or {}).get(entity_id)
    if state is None:
        return None
    stamp = parse_ts(state.get("last_updated")) or parse_ts(state.get("last_changed"))
    if stamp is not None and float(stamp) > float(at_ts) + 1e-9:
        return None
    if temporal is not None and stamp is None:
        return None
    return state


def _scalar_at(entity_id, state_map, temporal, at_ts, agent):
    state = _state_at(entity_id, state_map, temporal, at_ts)
    if state is None:
        return None, None
    raw_state = str(state.get("state") or "").strip().lower()
    if raw_state in ("unavailable", "unknown", "none", ""):
        return state, None
    value = context_scalar(entity_id, state, agent)
    return state, value


def _entity_descriptor_snapshot(entity_id, state_map, temporal, at_ts, agent):
    state, current = _scalar_at(entity_id, state_map, temporal, at_ts, agent)
    available = current is not None
    current = float(current or 0.0)
    deltas = {}
    for name, lag_seconds in ENTITY_DESCRIPTORS:
        if not name.startswith("delta_"):
            continue
        _old_state, previous = _scalar_at(entity_id, state_map, temporal, float(at_ts) - float(lag_seconds), agent)
        previous = current if previous is None else float(previous)
        deltas[name] = current - previous
    changed = None
    if state is not None:
        changed = parse_ts(state.get("last_changed")) or parse_ts(state.get("last_updated"))
    if changed is None or float(changed) > float(at_ts):
        freshness = 0.0
    else:
        tau = max(1.0, float(OPTIONS.get("observation_freshness_tau_seconds", 300)))
        freshness = math.exp(-max(0.0, float(at_ts) - float(changed)) / tau)
    return {
        "value": current,
        "delta_1s": float(deltas.get("delta_1s", 0.0)),
        "delta_10s": float(deltas.get("delta_10s", 0.0)),
        "delta_60s": float(deltas.get("delta_60s", 0.0)),
        "freshness": float(freshness),
        "available": 1.0 if available else 0.0,
    }, available


def observation_as_of(mask, state_map, temporal, at_ts, agent, *, home_provider=None):
    """Reconstruct one selected dense observation exactly as of a historical timestamp."""
    if not isinstance(mask, ObservationMask):
        mask = ObservationMask.from_export(mask)
    if mask.schema_id != observation_schema_id():
        raise ValueError("NEEDS_RETRAIN: incompatible observation schema")
    at_ts = float(at_ts)
    advance = getattr(temporal, "advance", None)
    if callable(advance):
        # Historical replay/Correct trackers own their exact as-of state, including
        # RoomBelief. Reposition before reading so future tracker state cannot leak.
        advance(at_ts)
    # SQLiteTemporalTracker/HistoricalTemporalTracker expose the causal values through
    # their state_map + TemporalHistory pair. Live callers may pass TemporalHistory
    # directly, so normalize both forms here.
    causal_state_map = getattr(temporal, "state_map", None) or state_map
    causal_temporal = getattr(temporal, "history", None) or temporal
    dt = datetime.fromtimestamp(at_ts).astimezone()
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    dow = dt.weekday()
    global_values = {
        "time:hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "time:hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "time:dow_sin": math.sin(2 * math.pi * dow / 7.0),
        "time:dow_cos": math.cos(2 * math.pi * dow / 7.0),
    }
    provider = (
        home_provider
        or getattr(causal_temporal, "home_context", None)
        or getattr(temporal, "home_context", None)
    )
    forecast = {}
    if provider is not None and callable(getattr(provider, "forecast", None)):
        forecast = provider.forecast(agent["target_entity"], at_ts) or {}
    home_values = {"home:" + name: float(forecast.get(name, 0.0) or 0.0) for name in FEATURE_NAMES}
    entity_cache = {}
    entity_available = {}
    values = []
    missing_ids = []
    for row in mask.features:
        feature_id = row["id"]
        kind = row.get("kind")
        if kind == "global":
            value = float(global_values.get(feature_id, 0.0))
        elif kind == "home":
            value = float(home_values.get(feature_id, 0.0))
            if not forecast:
                missing_ids.append(feature_id)
        else:
            entity_id = row["entity_id"]
            if entity_id not in entity_cache:
                snapshot, available = _entity_descriptor_snapshot(
                    entity_id, causal_state_map, causal_temporal, at_ts, agent
                )
                entity_cache[entity_id] = snapshot
                entity_available[entity_id] = available
            value = float(entity_cache[entity_id].get(row["descriptor"], 0.0))
            if not entity_available[entity_id] and row["descriptor"] != "available":
                missing_ids.append(feature_id)
        values.append(value)
    return {
        "schema_id": mask.schema_id,
        "mask_id": mask.mask_id,
        "timestamp": at_ts,
        "feature_ids": list(mask.feature_ids),
        "values": values,
        "sparse": {fid: value for fid, value in zip(mask.feature_ids, values) if abs(float(value)) > 1e-12},
        "missing_feature_ids": missing_ids,
        "missing_feature_count": len(missing_ids),
        "computed_entity_descriptors": len(entity_cache),
        "home_forecast_available": bool(forecast),
    }
