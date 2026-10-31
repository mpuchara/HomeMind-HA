"""U04 shared, action-conditioned neural projection: OBSERVER ONLY.

One fixed-weight-size model consumes a variable number of ActionCandidate rows.
Outputs are uncalibrated random projections until future U05 training; NEVER
dispatch them, choose a winner, or interpret the numbers as confidence/reward.
Existing DeviceAgent guards + ActionIntent + Executor remain authoritative.
"""
from __future__ import annotations

import hashlib
import math
import numpy as np

from universal_actions import ActionSet, ActionCandidate, ACTION_CONTRACT_VERSION, ACTION_KINDS

SCORER_CONTRACT_VERSION = 1
HASH_BUCKETS = 16
NUMERIC_WIDTH = 8
ACTION_FEATURE_WIDTH = HASH_BUCKETS * 3 + len(ACTION_KINDS) + NUMERIC_WIDTH
LAYERS = ("dense1", "dense2")
MAX_BATCH = 32


def _one_hot(label, *, dimension=HASH_BUCKETS):
    vector = np.zeros(dimension, dtype=np.float32)
    digest = hashlib.blake2b(str(label).encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(digest, "big") % dimension
    vector[idx] = 1.0
    return vector


def _dense(rng, input_width, output_width):
    scale = math.sqrt(2.0 / (input_width + output_width))
    return (
        rng.normal(0, scale, (output_width, input_width)).astype(np.float32),
        np.zeros(output_width, dtype=np.float32),
    )


def _vector(data, dimension, label):
    result = np.asarray(data, dtype=np.float32)
    if result.shape != (dimension,) or not np.isfinite(result).all():
        raise ValueError(f"invalid {label} embedding")
    return result


class UniversalActionScorer:
    """One shared set of weights for arbitrary action/device cardinality."""

    def __init__(self, *, room_width=32, hidden_width=24, seed=43, checkpoint=None):
        self.room_width = int(room_width)
        self.hidden_width = int(hidden_width)
        if not (4 <= self.room_width <= 128 and 4 <= self.hidden_width <= 128):
            raise ValueError("invalid scorer dimensions")
        self.input_width = self.room_width * 2 + ACTION_FEATURE_WIDTH
        rng = np.random.default_rng(seed)
        self.layers = {
            "dense1": _dense(rng, self.input_width, self.hidden_width),
            "dense2": _dense(rng, self.hidden_width, 1),
        }
        if checkpoint is not None:
            self._load(checkpoint)

    def _load(self, model):
        if (not isinstance(model, dict)
                or model.get("version") != SCORER_CONTRACT_VERSION
                or model.get("action_contract_version") != ACTION_CONTRACT_VERSION
                or model.get("trained") is not False
                or model.get("room_width") != self.room_width
                or model.get("hidden_width") != self.hidden_width
                or model.get("hash_buckets") != HASH_BUCKETS
                or model.get("numeric_width") != NUMERIC_WIDTH):
            raise ValueError("NEEDS_RETRAIN: incompatible/unverified action scorer")
        raw = model.get("layers")
        if not isinstance(raw, dict) or set(raw) != set(LAYERS):
            raise ValueError("incomplete action scorer checkpoint")
        loaded = {}
        for key in LAYERS:
            row = raw.get(key)
            if not isinstance(row, dict):
                raise ValueError("invalid action scorer layer")
            w = np.asarray(row.get("weights"), dtype=np.float32)
            b = np.asarray(row.get("bias"), dtype=np.float32)
            w0, b0 = self.layers[key]
            if w.shape != w0.shape or b.shape != b0.shape or not np.isfinite(w).all() or not np.isfinite(b).all():
                raise ValueError("NEEDS_RETRAIN: scorer parameter shape/value mismatch")
            loaded[key] = (w.copy(), b.copy())
        self.layers = loaded

    def checkpoint(self):
        return {
            "version": SCORER_CONTRACT_VERSION,
            "action_contract_version": ACTION_CONTRACT_VERSION,
            "trained": False,
            "room_width": self.room_width,
            "hidden_width": self.hidden_width,
            "hash_buckets": HASH_BUCKETS,
            "numeric_width": NUMERIC_WIDTH,
            "layers": {
                key: {"weights": w.tolist(), "bias": b.tolist()}
                for key, (w, b) in self.layers.items()
            },
        }

    def action_features(self, action):
        if not isinstance(action, ActionCandidate):
            raise TypeError("ActionCandidate required")
        domain = action.entity_id.partition(".")[0]
        if not domain or action.kind not in ACTION_KINDS:
            raise ValueError("invalid action semantics")
        lower, upper, value = (float(x) for x in (action.lower, action.upper, action.value))
        if not all(math.isfinite(x) for x in (lower, upper, value)) or upper < lower or not lower-1e-8 <= value <= upper+1e-8:
            raise ValueError("invalid action range/value")
        span = max(upper-lower, 1e-9)
        position = (value-lower)/span if upper > lower else 0.5
        current = action.current_value
        has_current = current is not None and math.isfinite(float(current))
        current_pos = max(0.0, min(1.0, (float(current)-lower)/span)) if has_current and upper > lower else 0.0
        numeric = np.asarray([
            position,
            float(has_current),
            current_pos,
            max(-1.0, min(1.0, (value-float(current))/span)) if has_current else 0.0,
            math.tanh(value / 100.0),
            math.tanh((upper-lower) / 100.0),
            1.0 if action.option_label is not None else 0.0,
            1.0 if action.area_id is not None else 0.0,
        ], dtype=np.float32)
        kind = np.zeros(len(ACTION_KINDS), dtype=np.float32)
        kind[ACTION_KINDS.index(action.kind)] = 1.0
        return np.concatenate((
            _one_hot(domain), _one_hot(action.property_name),
            _one_hot(action.option_label if action.option_label is not None else "", dimension=HASH_BUCKETS)
            if action.option_label is not None else np.zeros(HASH_BUCKETS, dtype=np.float32),
            kind, numeric,
        ))

    def evaluate(self, action_set, neural_home, *, max_batch=MAX_BATCH):
        """Raw projection of valid masked proposals. No action recommendation."""
        if not isinstance(action_set, ActionSet) or action_set.version != ACTION_CONTRACT_VERSION:
            raise ValueError("versioned ActionSet required")
        if type(max_batch) is not int or not 1 <= max_batch <= MAX_BATCH:
            raise ValueError("max_batch must be within 1..32")
        if not action_set.candidates:
            return {
                "evaluated": 0, "fallback": "abstain", "trained": False,
                "control_eligible": False, "projections": [],
            }
        if neural_home.encoder.room_width != self.room_width:
            raise ValueError("encoder and scorer embedding widths differ")
        home = _vector(neural_home.home(), self.room_width, "home")
        # Materialize one immutable snapshot per area, not per action.
        rooms = {}
        for action in action_set.candidates:
            if action.mask_timestamp != action_set.mask_timestamp:
                raise ValueError("candidate mask timestamp mismatch")
            area = action.area_id
            if area not in rooms:
                rooms[area] = _vector(
                    neural_home.room(area) if area is not None else np.zeros(self.room_width, dtype=np.float32),
                    self.room_width, "room",
                )
        output = []
        for start in range(0, len(action_set.candidates), max_batch):
            chunk = action_set.candidates[start:start+max_batch]
            batch = np.stack([
                np.concatenate((home, rooms[action.area_id], self.action_features(action)))
                for action in chunk
            ], axis=0)
            w1, b1 = self.layers["dense1"]
            w2, b2 = self.layers["dense2"]
            hidden = np.maximum(batch @ w1.T + b1, 0.0)
            raw = (hidden @ w2.T + b2).reshape(-1)
            if not np.isfinite(raw).all():
                raise ValueError("non-finite scorer output")
            for action, value in zip(chunk, raw):
                output.append({
                    "entity_id": action.entity_id,
                    "property": action.property_name,
                    "value": action.value,
                    "raw_projection": float(value),
                    "confidence": None,
                    "reward": None,
                    "recommendation": None,
                })
        return {
            "evaluated": len(output),
            "fallback": "observer_only",
            "trained": False,
            "control_eligible": False,
            "projections": output,
        }

    @property
    def parameter_count(self):
        return sum(w.size+b.size for w, b in self.layers.values())
