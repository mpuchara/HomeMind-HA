"""Tiny neural policy backend used for local Shadow inference.

Stage 4 adds *offline supervised* training through policy_tiny_mlp_training.py.  This
backend still has no online/reward-learning API and no physical authority: update()
remains disabled, ActionIntent/Executor are not imported here, and runtime promotion
continues to be owned by the existing Candidate lifecycle.

Weights and normalization vectors use IEEE-754 float32 arrays from the Python standard
library.  The add-on therefore keeps the Stage-3 no-NumPy/TensorFlow/PyTorch footprint.
"""
from __future__ import annotations

from array import array
import hashlib
import json
import math

from policy_backend import (
    PolicyBackend,
    require_backend,
    serialize_backend_model,
    verify_model_checksum,
)


class TinyMLPBackend(PolicyBackend):
    BACKEND = "tiny_mlp"
    VERSION = 1
    DTYPE = "float32"
    DEFAULT_HIDDEN = (32, 16)

    def __init__(
        self,
        *,
        actions,
        horizons,
        feature_ids,
        schema_id,
        mask_id,
        hidden=None,
        init_seed=1482,
        model=None,
    ):
        self.actions = tuple(float(x) for x in actions)
        self.horizons = tuple(int(x) for x in horizons)
        self.feature_ids = tuple(str(x) for x in feature_ids)
        self.schema_id = str(schema_id)
        self.mask_id = str(mask_id)
        self.hidden = tuple(int(x) for x in (hidden or self.DEFAULT_HIDDEN))
        self.init_seed = int(init_seed)
        if not self.actions:
            raise ValueError("TinyMLPBackend requires at least one action")
        if not self.horizons:
            raise ValueError("TinyMLPBackend requires at least one prediction horizon")
        if not self.feature_ids:
            raise ValueError("TinyMLPBackend requires an explicit observation feature mask")
        if not self.hidden or any(width <= 0 or width > 256 for width in self.hidden):
            raise ValueError("TinyMLPBackend hidden layers must be in range 1..256")
        self.input_size = len(self.feature_ids)
        self.output_size = len(self.actions)
        self.architecture = (self.input_size, *self.hidden, self.output_size)
        self.trained = False
        self.training_samples = 0
        self.training_meta = {}
        self.input_mean = array("f", [0.0] * self.input_size)
        self.input_scale = array("f", [1.0] * self.input_size)

        if model is None:
            self.weights, self.biases = self._initialize_parameters()
            self.model_revision = self._initial_revision()
            self.persisted_checksum = None
        else:
            self._load_parameters(model)
            self._load_normalization(model)
            self.model_revision = str(model.get("model_revision") or self._initial_revision())
            self.trained = bool(model.get("trained", False))
            self.training_samples = int(model.get("training_samples") or 0)
            self.training_meta = dict(model.get("training_meta") or {})
            self.persisted_checksum = model.get("model_checksum")

    @staticmethod
    def _canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _initial_revision(self):
        payload = {
            "backend": self.BACKEND,
            "version": self.VERSION,
            "seed": self.init_seed,
            "schema_id": self.schema_id,
            "mask_id": self.mask_id,
            "feature_ids": list(self.feature_ids),
            "actions": list(self.actions),
            "horizons": list(self.horizons),
            "hidden": list(self.hidden),
        }
        digest = hashlib.sha256(self._canonical(payload).encode("utf-8")).hexdigest()
        return "tiny-mlp-init-" + digest[:20]

    def _rng(self):
        state = (self.init_seed & 0xFFFFFFFFFFFFFFFF) ^ 0x9E3779B97F4A7C15
        if state == 0:
            state = 0xD1B54A32D192ED03
        mask = 0xFFFFFFFFFFFFFFFF
        while True:
            state ^= (state >> 12) & mask
            state ^= (state << 25) & mask
            state ^= (state >> 27) & mask
            state &= mask
            value = (state * 2685821657736338717) & mask
            yield float(value >> 11) / float(1 << 53)

    def _initialize_parameters(self):
        random_values = self._rng()
        weights = []
        biases = []
        for fan_in, fan_out in zip(self.architecture, self.architecture[1:]):
            limit = math.sqrt(6.0 / float(fan_in + fan_out))
            weights.append(
                array(
                    "f",
                    (
                        (next(random_values) * 2.0 - 1.0) * limit
                        for _ in range(int(fan_in) * int(fan_out))
                    ),
                )
            )
            biases.append(array("f", [0.0] * int(fan_out)))
        return weights, biases

    def _load_parameters(self, model):
        raw_weights = list(model.get("weights") or ())
        raw_biases = list(model.get("biases") or ())
        expected_layers = len(self.architecture) - 1
        if len(raw_weights) != expected_layers or len(raw_biases) != expected_layers:
            raise ValueError("NEEDS_RETRAIN: tiny MLP tensor layer count mismatch")
        weights = []
        biases = []
        for layer_index, (fan_in, fan_out) in enumerate(
            zip(self.architecture, self.architecture[1:])
        ):
            layer = [float(x) for x in raw_weights[layer_index]]
            bias = [float(x) for x in raw_biases[layer_index]]
            if len(layer) != int(fan_in) * int(fan_out):
                raise ValueError("NEEDS_RETRAIN: tiny MLP weight shape mismatch")
            if len(bias) != int(fan_out):
                raise ValueError("NEEDS_RETRAIN: tiny MLP bias shape mismatch")
            if not all(math.isfinite(x) for x in layer + bias):
                raise ValueError("NEEDS_RETRAIN: tiny MLP contains non-finite parameters")
            weights.append(array("f", layer))
            biases.append(array("f", bias))
        self.weights = weights
        self.biases = biases

    def _load_normalization(self, model):
        raw_mean = list(model.get("input_mean") or [0.0] * self.input_size)
        raw_scale = list(model.get("input_scale") or [1.0] * self.input_size)
        if len(raw_mean) != self.input_size or len(raw_scale) != self.input_size:
            raise ValueError("NEEDS_RETRAIN: tiny MLP normalization width mismatch")
        mean = [float(x) for x in raw_mean]
        scale = [float(x) for x in raw_scale]
        if not all(math.isfinite(x) for x in mean + scale):
            raise ValueError("NEEDS_RETRAIN: tiny MLP normalization contains non-finite values")
        if any(x <= 0.0 for x in scale):
            raise ValueError("NEEDS_RETRAIN: tiny MLP normalization scale must be positive")
        self.input_mean = array("f", mean)
        self.input_scale = array("f", scale)

    def _dense_input(self, features):
        if isinstance(features, dict) and "values" in features:
            ids = tuple(str(x) for x in (features.get("feature_ids") or ()))
            if ids != self.feature_ids:
                raise ValueError("NEEDS_RETRAIN: tiny MLP observation feature order mismatch")
            values = list(features.get("values") or ())
        elif isinstance(features, (list, tuple, array)):
            values = list(features)
        elif isinstance(features, dict):
            values = [features.get(i, 0.0) for i in range(self.input_size)]
        else:
            raise TypeError("TinyMLPBackend expects an observation vector")
        if len(values) != self.input_size:
            raise ValueError("NEEDS_RETRAIN: tiny MLP observation width mismatch")
        dense = [float(x) for x in values]
        if not all(math.isfinite(x) for x in dense):
            raise ValueError("tiny MLP observation contains non-finite values")
        return dense

    def normalized_input(self, features):
        dense = self._dense_input(features)
        return [
            max(-6.0, min(6.0, (value - float(mean)) / float(scale)))
            for value, mean, scale in zip(dense, self.input_mean, self.input_scale)
        ]

    @staticmethod
    def _relu(values):
        return [value if value > 0.0 else 0.0 for value in values]

    def _forward(self, dense, *, already_normalized=False):
        current = list(dense) if already_normalized else [
            max(-6.0, min(6.0, (float(value) - float(mean)) / float(scale)))
            for value, mean, scale in zip(dense, self.input_mean, self.input_scale)
        ]
        for layer_index, (fan_in, fan_out) in enumerate(
            zip(self.architecture, self.architecture[1:])
        ):
            weights = self.weights[layer_index]
            biases = self.biases[layer_index]
            output = []
            for row in range(int(fan_out)):
                offset = row * int(fan_in)
                value = float(biases[row])
                for column in range(int(fan_in)):
                    value += float(weights[offset + column]) * float(current[column])
                output.append(value)
            current = output if layer_index == len(self.weights) - 1 else self._relu(output)
        return current

    @staticmethod
    def softmax(scores):
        if not scores:
            return []
        peak = max(float(x) for x in scores)
        exp = [math.exp(max(-60.0, min(60.0, float(x) - peak))) for x in scores]
        total = sum(exp)
        if total <= 0.0 or not math.isfinite(total):
            return [1.0 / len(scores)] * len(scores)
        return [value / total for value in exp]

    def predict(self, features, allowed_indices=None):
        dense = self._dense_input(features)
        scores = self._forward(dense)
        probabilities = self.softmax(scores)
        if allowed_indices is None:
            allowed = list(range(len(self.actions)))
        else:
            allowed = sorted(
                {int(x) for x in allowed_indices if 0 <= int(x) < len(self.actions)}
            )
        if not allowed:
            raise ValueError("allowed action set is empty")
        chosen_index = min(
            allowed,
            key=lambda idx: (-float(probabilities[idx] if self.trained else scores[idx]), int(idx)),
        )
        support = min(1.0, math.log1p(max(0, self.training_samples)) / math.log(257.0)) if self.trained else 0.0
        arms = [
            {
                "index": int(index),
                "value": float(action),
                "score": float(scores[index]),
                "probability": float(probabilities[index]),
                "mean": float(probabilities[index] if self.trained else scores[index]),
                "uncertainty": float(1.0 - probabilities[index]) if self.trained else 1.0,
                "support": float(support),
                "novelty": float(1.0 - support),
            }
            for index, action in enumerate(self.actions)
        ]
        chosen = dict(arms[chosen_index])
        confidence = float(probabilities[chosen_index]) if self.trained else 0.0
        chosen["trained"] = bool(self.trained)
        chosen["confidence_kind"] = "softmax_uncalibrated" if self.trained else "untrained_zero"
        chosen["structural_confidence"] = confidence
        chosen["validation_accuracy"] = float(
            (self.training_meta.get("tournament") or {}).get("mlp_score") or 0.0
        )
        chosen["validation_lower_bound"] = 0.0
        chosen["validation_samples"] = int(
            (self.training_meta.get("tournament") or {}).get("samples") or 0
        )
        horizon = int(self.horizons[0])
        return chosen, confidence, arms, horizon, support, 1.0 - support

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        raise RuntimeError(
            "tiny MLP online/reward update is disabled; Stage 4 trains only in the offline supervised trainer"
        )

    def decay(self, now=None):
        return None

    @property
    def parameter_count(self):
        return (
            sum(len(layer) for layer in self.weights)
            + sum(len(layer) for layer in self.biases)
        )

    def serialize(self):
        raw = {
            "format": "homemind-tiny-mlp-v1",
            "backend": self.BACKEND,
            "version": self.VERSION,
            "model_revision": self.model_revision,
            "dtype": self.DTYPE,
            "input_size": self.input_size,
            "hidden": list(self.hidden),
            "output_size": self.output_size,
            "actions": list(self.actions),
            "horizons": list(self.horizons),
            "feature_ids": list(self.feature_ids),
            "observation_schema_id": self.schema_id,
            "observation_mask_id": self.mask_id,
            "init_seed": self.init_seed,
            "trained": bool(self.trained),
            "training_samples": int(self.training_samples),
            "training_meta": dict(self.training_meta or {}),
            "input_mean": list(self.input_mean),
            "input_scale": list(self.input_scale),
            "weights": [list(layer) for layer in self.weights],
            "biases": [list(layer) for layer in self.biases],
        }
        packed = serialize_backend_model(
            raw,
            policy_backend=self.BACKEND,
            backend_version=self.VERSION,
            schema_id=self.schema_id,
            mask_id=self.mask_id,
        )
        self.persisted_checksum = packed.get("model_checksum")
        return packed

    @classmethod
    def deserialize(
        cls,
        raw,
        *,
        expected_schema_id=None,
        expected_mask_id=None,
        expected_feature_ids=None,
        expected_actions=None,
        expected_horizons=None,
        **kwargs,
    ):
        require_backend(raw, expected=cls.BACKEND)
        if not verify_model_checksum(raw):
            raise ValueError("NEEDS_RETRAIN: tiny MLP model checksum mismatch")
        if int(raw.get("version", 0)) != cls.VERSION:
            raise ValueError("NEEDS_RETRAIN: incompatible tiny MLP backend version")
        if str(raw.get("dtype") or "") != cls.DTYPE:
            raise ValueError("NEEDS_RETRAIN: incompatible tiny MLP dtype")
        schema_id = str(
            raw.get("observation_schema_id") or raw.get("feature_schema_id") or ""
        )
        mask_id = str(raw.get("observation_mask_id") or raw.get("feature_mask_id") or "")
        feature_ids = tuple(str(x) for x in (raw.get("feature_ids") or ()))
        actions = tuple(float(x) for x in (raw.get("actions") or ()))
        horizons = tuple(int(x) for x in (raw.get("horizons") or ()))
        if expected_schema_id is not None and schema_id != str(expected_schema_id):
            raise ValueError("NEEDS_RETRAIN: tiny MLP observation schema mismatch")
        if expected_mask_id is not None and mask_id != str(expected_mask_id):
            raise ValueError("NEEDS_RETRAIN: tiny MLP observation mask mismatch")
        if expected_feature_ids is not None and feature_ids != tuple(
            str(x) for x in expected_feature_ids
        ):
            raise ValueError("NEEDS_RETRAIN: tiny MLP observation feature order mismatch")
        if expected_actions is not None and actions != tuple(
            float(x) for x in expected_actions
        ):
            raise ValueError("NEEDS_RETRAIN: tiny MLP action space mismatch")
        if expected_horizons is not None and horizons != tuple(
            int(x) for x in expected_horizons
        ):
            raise ValueError("NEEDS_RETRAIN: tiny MLP horizon set mismatch")
        hidden = tuple(int(x) for x in (raw.get("hidden") or cls.DEFAULT_HIDDEN))
        obj = cls(
            actions=actions,
            horizons=horizons,
            feature_ids=feature_ids,
            schema_id=schema_id,
            mask_id=mask_id,
            hidden=hidden,
            init_seed=int(raw.get("init_seed") or 0),
            model=raw,
        )
        if int(raw.get("input_size") or -1) != obj.input_size:
            raise ValueError("NEEDS_RETRAIN: tiny MLP persisted input width mismatch")
        if int(raw.get("output_size") or -1) != obj.output_size:
            raise ValueError("NEEDS_RETRAIN: tiny MLP persisted output width mismatch")
        if obj.trained and obj.training_samples <= 0:
            raise ValueError("NEEDS_RETRAIN: trained tiny MLP has no training sample count")
        return obj

    def diagnostics(self):
        raw = self.serialize()
        encoded = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return {
            "backend": self.BACKEND,
            "backend_version": self.VERSION,
            "model_revision": self.model_revision,
            "model_checksum": raw.get("model_checksum"),
            "feature_schema_id": self.schema_id,
            "feature_mask_id": self.mask_id,
            "feature_count": self.input_size,
            "architecture": list(self.architecture),
            "hidden": list(self.hidden),
            "output_count": self.output_size,
            "parameter_count": self.parameter_count,
            "dtype": self.DTYPE,
            "serialized_bytes": len(encoded.encode("utf-8")),
            "trained": bool(self.trained),
            "training_samples": int(self.training_samples),
            "historical_training": bool(self.trained),
            "training_meta": dict(self.training_meta or {}),
            "shadow_only": True,
            "dispatch_capability": False,
            "physical_authority": False,
        }
