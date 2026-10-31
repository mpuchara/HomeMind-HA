"""U03 shared, permutation-invariant neural set encoder (observer-only).

Architecture:
  typed SensorToken -> shared 2-layer token MLP -> weighted Deep Sets pool
  -> room MLP -> mean room set -> home MLP

The network's parameter shapes depend only on token semantics and configured
embedding widths, NEVER on the number of sensors, rooms, or devices. The
observations are updated incrementally through UniversalObservationIndex's
single-writer change journal. No physical action or policy preference exists
in this module. Randomly initialized vectors are *not trained decisions*.

Only the sensor update, expiration and affected room are recomputed, while
home inference is O(room_embedding_width) after the affected room update.
"""
from __future__ import annotations

import math

import numpy as np

from universal_observation import SensorToken, UniversalObservationIndex


MODEL_CONTRACT_VERSION = 1
KINDS = (
    "binary", "temperature", "humidity", "illuminance",
    "power", "co2", "generic",
)
INPUT_WIDTH = 4 + len(KINDS)
KIND_OFFSET = 4
MODEL_KEYS = ("token1", "token2", "room1", "room2", "home1", "home2")


def _relu(linear):
    return np.maximum(linear, 0.0)


def _layer(rng, input_width, output_width):
    scale = math.sqrt(2.0 / (input_width + output_width))
    weights = rng.normal(0.0, scale, (output_width, input_width)).astype(np.float32)
    bias = np.zeros((output_width,), dtype=np.float32)
    return (weights, bias)


class SharedSetEncoder:
    """Shared float32 neural weights; no per-entity trained parameters."""

    def __init__(self, *, token_width=16, room_width=32, hidden_width=24,
                 seed=41, checkpoint=None):
        self.token_width = int(token_width)
        self.room_width = int(room_width)
        self.hidden_width = int(hidden_width)
        if not all(4 <= width <= 128 for width in (
            self.token_width, self.room_width, self.hidden_width
        )):
            raise ValueError("encoder widths must be within 4..128")
        rng = np.random.default_rng(seed)
        self.layers = {
            "token1": _layer(rng, INPUT_WIDTH, self.hidden_width),
            "token2": _layer(rng, self.hidden_width, self.token_width),
            "room1": _layer(rng, self.token_width + 3, self.hidden_width),
            "room2": _layer(rng, self.hidden_width, self.room_width),
            "home1": _layer(rng, self.room_width + 3, self.hidden_width),
            "home2": _layer(rng, self.hidden_width, self.room_width),
        }
        if checkpoint is not None:
            self._load(checkpoint)

    def _layer(self, key, vector):
        weights, bias = self.layers[key]
        return _relu(weights @ vector + bias)

    def _load(self, checkpoint):
        if not isinstance(checkpoint, dict) or checkpoint.get("contract_version") != MODEL_CONTRACT_VERSION:
            raise ValueError("NEEDS_RETRAIN: invalid shared set encoder checkpoint")
        if checkpoint.get("kinds") != list(KINDS):
            raise ValueError("NEEDS_RETRAIN: incompatible sensor kinds")
        if (checkpoint.get("token_width") != self.token_width or
                checkpoint.get("room_width") != self.room_width or
                checkpoint.get("hidden_width") != self.hidden_width):
            raise ValueError("NEEDS_RETRAIN: incompatible neural widths")
        raw = checkpoint.get("layers")
        if not isinstance(raw, dict) or set(raw) != set(MODEL_KEYS):
            raise ValueError("NEEDS_RETRAIN: incomplete checkpoint layers")
        restored = {}
        for key in MODEL_KEYS:
            item = raw[key]
            if not isinstance(item, dict):
                raise ValueError("invalid layer checkpoint")
            weights = np.asarray(item.get("weights"), dtype=np.float32)
            bias = np.asarray(item.get("bias"), dtype=np.float32)
            expected_weights, expected_bias = self.layers[key]
            if weights.shape != expected_weights.shape or bias.shape != expected_bias.shape:
                raise ValueError("NEEDS_RETRAIN: incompatible neural layer shape")
            if not np.isfinite(weights).all() or not np.isfinite(bias).all():
                raise ValueError("NEEDS_RETRAIN: nonfinite neural weights")
            restored[key] = (weights.copy(), bias.copy())
        self.layers = restored

    def checkpoint(self):
        """JSON-safe, versioned weights; never Python pickle."""
        return {
            "contract_version": MODEL_CONTRACT_VERSION,
            "token_width": self.token_width,
            "room_width": self.room_width,
            "hidden_width": self.hidden_width,
            "kinds": list(KINDS),
            "trained": False,  # U03 checkpoint is structural only; no optimizer yet.
            "layers": {
                key: {"weights": w.tolist(), "bias": b.tolist()}
                for key, (w, b) in self.layers.items()
            },
        }

    def token_features(self, token):
        if not isinstance(token, SensorToken):
            raise TypeError("SharedSetEncoder requires typed SensorToken")
        vector = np.zeros((INPUT_WIDTH,), dtype=np.float32)
        valid = bool(token.available)
        quality = float(token.quality) if valid else 0.0
        if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
            raise ValueError("invalid token quality")
        value = float(token.value) if valid else 0.0
        if not math.isfinite(value):
            raise ValueError("invalid token value")
        # Keep explicit availability and quality: a zero lux / OFF is not missing.
        vector[0] = max(-1.0, min(1.0, value / 6.0))
        vector[1] = float(valid)
        vector[2] = quality
        vector[3] = max(-1.0, min(1.0, value * quality / 6.0))
        try:
            position = KINDS.index(token.kind)
        except ValueError as exc:
            raise ValueError("unknown sensor type: extend the versioned contract") from exc
        vector[KIND_OFFSET + position] = 1.0
        return vector

    def token_embedding(self, token):
        return self._layer("token2", self._layer("token1", self.token_features(token)))

    def room_embedding(self, pooled_token_embedding, *, count, available, weight, quality_sum):
        if not count:
            return np.zeros((self.room_width,), dtype=np.float32)
        mean = np.asarray(pooled_token_embedding, dtype=np.float64) / max(float(weight), 1e-12)
        features = np.concatenate((
            np.asarray(mean, dtype=np.float32),
            np.asarray([
                min(4.0, math.log1p(count) / math.log1p(1000.0)),
                available / count,
                quality_sum / available if available else 0.0,
            ], dtype=np.float32),
        ))
        return self._layer("room2", self._layer("room1", features))

    def home_embedding(self, room_sum, *, rooms, sensors, available):
        if not rooms:
            return np.zeros((self.room_width,), dtype=np.float32)
        features = np.concatenate((
            np.asarray(room_sum / rooms, dtype=np.float32),
            np.asarray([
                min(4.0, math.log1p(rooms) / math.log1p(100.0)),
                min(4.0, math.log1p(sensors) / math.log1p(1000.0)),
                available / sensors if sensors else 0.0,
            ], dtype=np.float32),
        ))
        return self._layer("home2", self._layer("home1", features))

    @property
    def parameter_count(self):
        return sum(w.size + b.size for w, b in self.layers.values())


class _RoomAccum:
    """Uses float64 sums for stable increment/decrement under many updates."""
    __slots__ = ("count", "available", "weight", "quality_sum", "token_sum")

    def __init__(self, token_width):
        self.count = 0
        self.available = 0
        self.weight = 0.0
        self.quality_sum = 0.0
        self.token_sum = np.zeros((token_width,), dtype=np.float64)

    def apply(self, token, embedding, sign):
        self.count += sign
        if token.available:
            self.available += sign
            weight = float(token.quality)
            self.weight += sign * weight
            self.quality_sum += sign * weight
            self.token_sum += sign * weight * embedding.astype(np.float64)


class IncrementalNeuralHome:
    """Single-writer cache that performs O(changed-tokens) neural work.

    Each operation drains *all* pending index changes (including expiry).
    External callers may inspect .index, but must not mutate it directly:
    use upsert/remove/advance to maintain neural cache coherence.
    """

    def __init__(self, encoder=None):
        self.encoder = encoder or SharedSetEncoder()
        self.index = UniversalObservationIndex()
        self._tokens = {}
        self._rooms = {}
        self._room_vectors = {}
        self._room_sum = np.zeros((self.encoder.room_width,), dtype=np.float64)
        self._count = 0
        self._available = 0
        self._cached_home = None
        self._token_evals = 0
        self._room_evals = 0
        self._home_evals = 0

    def _recompute_room(self, area_id):
        previous = self._room_vectors.pop(area_id, None)
        if previous is not None:
            self._room_sum -= previous.astype(np.float64)
        accumulator = self._rooms.get(area_id)
        if accumulator is None:
            return
        embedding = self.encoder.room_embedding(
            accumulator.token_sum, count=accumulator.count,
            available=accumulator.available, weight=accumulator.weight,
            quality_sum=accumulator.quality_sum,
        )
        self._room_vectors[area_id] = embedding
        self._room_sum += embedding.astype(np.float64)
        self._room_evals += 1

    def _sync(self):
        changed = self.index.drain_changes()
        if not changed:
            return
        affected = set()
        for entity_id in changed:
            previous = self._tokens.pop(entity_id, None)
            if previous is not None:
                old, old_embedding = previous
                room = self._rooms[old.area_id]
                room.apply(old, old_embedding, -1)
                self._count -= 1
                self._available -= int(old.available)
                affected.add(old.area_id)
                if room.count == 0:
                    del self._rooms[old.area_id]
            current = self.index.get(entity_id)
            if current is not None:
                embedding = self.encoder.token_embedding(current)
                self._token_evals += 1
                room = self._rooms.setdefault(
                    current.area_id, _RoomAccum(self.encoder.token_width)
                )
                room.apply(current, embedding, 1)
                self._tokens[entity_id] = (current, embedding)
                self._count += 1
                self._available += int(current.available)
                affected.add(current.area_id)
        for area in sorted(affected):
            self._recompute_room(area)
        self._cached_home = None

    def upsert(self, token, *, now):
        changed = self.index.upsert(token, now=now)
        self._sync()
        return changed

    def advance(self, now):
        self.index.advance(now)
        self._sync()

    def remove(self, entity_id):
        changed = self.index.remove(entity_id)
        self._sync()
        return changed

    def room(self, area_id):
        value = self._room_vectors.get(str(area_id))
        if value is None:
            return np.zeros((self.encoder.room_width,), dtype=np.float32)
        return value.copy()

    def home(self):
        if self._cached_home is None:
            self._cached_home = self.encoder.home_embedding(
                self._room_sum, rooms=len(self._room_vectors),
                sensors=self._count, available=self._available,
            )
            self._home_evals += 1
        return self._cached_home.copy()

    def diagnostics(self):
        return {
            "model_contract_version": MODEL_CONTRACT_VERSION,
            "sensor_count": self._count,
            "available": self._available,
            "room_count": len(self._rooms),
            "token_encoder_calls": self._token_evals,
            "room_encoder_calls": self._room_evals,
            "home_encoder_calls": self._home_evals,
            "parameters": self.encoder.parameter_count,
            "index": self.index.diagnostics(),
            "physical_actions": 0,
        }
