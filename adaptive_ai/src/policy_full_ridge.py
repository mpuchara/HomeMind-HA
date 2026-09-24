"""Numerically stable full-ridge LinUCB backend for small semantic feature sets.

The production default remains DiagonalLinUCB.  This backend is deliberately bounded to a
small, explicit set of feature indices so it can model correlations without paying for a
128x128 matrix on every fast-device decision.
"""
from __future__ import annotations

import json
import math
import uuid

from policy_backend import (
    PolicyBackend, require_backend, serialize_backend_model, verify_model_checksum,
)
from settings import OPTIONS, clamp, now_ts


def _identity(n, value):
    return [[float(value) if i == j else 0.0 for j in range(n)] for i in range(n)]


def _dot(a, b):
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _cholesky(matrix, jitter=0.0):
    n = len(matrix)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = 0.5 * (float(matrix[i][j]) + float(matrix[j][i]))
            if i == j:
                value += jitter
            value -= sum(out[i][k] * out[j][k] for k in range(j))
            if i == j:
                if not math.isfinite(value) or value <= 1e-12:
                    raise ValueError("matrix is not positive definite")
                out[i][j] = math.sqrt(value)
            else:
                out[i][j] = value / out[j][j]
    return out


def _solve_spd(matrix, rhs):
    """Solve Ax=b with Cholesky and bounded diagonal jitter."""
    if not rhs:
        return []
    last = None
    for jitter in (0.0, 1e-10, 1e-8, 1e-6, 1e-4):
        try:
            chol = _cholesky(matrix, jitter)
            n = len(rhs)
            y = [0.0] * n
            for i in range(n):
                y[i] = (float(rhs[i]) - sum(chol[i][k] * y[k] for k in range(i))) / chol[i][i]
            x = [0.0] * n
            for i in range(n - 1, -1, -1):
                x[i] = (y[i] - sum(chol[k][i] * x[k] for k in range(i + 1, n))) / chol[i][i]
            if all(math.isfinite(v) for v in x):
                return x
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            last = exc
    raise ValueError(f"ridge solve failed after bounded jitter: {last}")


class FullRidgeLinUCBHead:
    VERSION = 1

    def __init__(self, feature_indices, actions, alpha=0.65, ridge=1.0, model=None):
        self.feature_indices = [int(x) for x in feature_indices]
        self.actions = [float(x) for x in actions]
        self.alpha = float(alpha)
        self.ridge = max(1e-6, float(ridge))
        self.dim = len(self.feature_indices)
        n = len(self.actions)
        valid = bool(
            model and int(model.get("version", 0)) == self.VERSION
            and [int(x) for x in model.get("feature_indices", [])] == self.feature_indices
            and len(model.get("a", [])) == n
        )
        if valid:
            self.a = [[[float(v) for v in row] for row in matrix] for matrix in model["a"]]
            self.b = [[float(v) for v in row] for row in model["b"]]
            self.counts = [float(v) for v in model.get("counts", [0.0] * n)]
            self.reward_sums = [float(v) for v in model.get("reward_sums", [0.0] * n)]
            self.total_updates = float(model.get("total_updates", sum(self.counts)))
            self.validation_pred_weight = [float(v) for v in model.get("validation_pred_weight", [0.0] * n)]
            self.validation_pred_correct_weight = [float(v) for v in model.get("validation_pred_correct_weight", [0.0] * n)]
        else:
            self.a = [_identity(self.dim, self.ridge) for _ in range(n)]
            self.b = [[0.0] * self.dim for _ in range(n)]
            self.counts = [0.0] * n
            self.reward_sums = [0.0] * n
            self.total_updates = 0.0
            self.validation_pred_weight = [0.0] * n
            self.validation_pred_correct_weight = [0.0] * n
        self.last_decay_ts = float((model or {}).get("last_decay_ts", now_ts()))
        self.half_life_days = float(OPTIONS.get("policy_half_life_days", 30))

    def dense(self, features):
        return [float(features.get(idx, 0.0)) for idx in self.feature_indices]

    def decay(self, now=None):
        now = now_ts() if now is None else float(now)
        elapsed = max(0.0, now - self.last_decay_ts)
        if elapsed < 60.0:
            return
        factor = math.exp(-math.log(2) * elapsed / (max(1.0, self.half_life_days) * 86400.0))
        for arm in range(len(self.actions)):
            for i in range(self.dim):
                for j in range(self.dim):
                    prior = self.ridge if i == j else 0.0
                    self.a[arm][i][j] = prior + (self.a[arm][i][j] - prior) * factor
                self.b[arm][i] *= factor
            self.counts[arm] *= factor
            self.reward_sums[arm] *= factor
            self.validation_pred_weight[arm] *= factor
            self.validation_pred_correct_weight[arm] *= factor
        self.total_updates *= factor
        self.last_decay_ts = now

    def sample_weight(self, sample_ts=None):
        if sample_ts is None:
            return 1.0
        return math.exp(-math.log(2) * max(0.0, self.last_decay_ts - float(sample_ts)) /
                        (max(1.0, self.half_life_days) * 86400.0))

    def _arm(self, action_idx, x):
        theta = _solve_spd(self.a[action_idx], self.b[action_idx])
        inverse_x = _solve_spd(self.a[action_idx], x)
        mean = _dot(theta, x)
        uncertainty = math.sqrt(max(0.0, _dot(x, inverse_x)) / max(1, self.dim))
        return mean, uncertainty

    def _global_uncertainty(self, x):
        matrix = _identity(self.dim, self.ridge)
        for arm in range(len(self.actions)):
            for i in range(self.dim):
                for j in range(self.dim):
                    prior = self.ridge if i == j else 0.0
                    matrix[i][j] += self.a[arm][i][j] - prior
        inv_x = _solve_spd(matrix, x)
        return math.sqrt(max(0.0, _dot(x, inv_x)) / max(1, self.dim))

    def evaluate(self, features):
        self.decay()
        x = self.dense(features)
        global_uncertainty = self._global_uncertainty(x)
        novelty = clamp(1.0 - math.exp(-1.6 * global_uncertainty), 0.0, 1.0)
        global_coverage = 1.0 - math.exp(-max(0.0, self.total_updates) / 20.0)
        arms = []
        for index, value in enumerate(self.actions):
            mean, uncertainty = self._arm(index, x)
            local_coverage = 1.0 - math.exp(-self.counts[index] / 3.0)
            support = clamp(global_coverage * (1.0 - novelty) * (0.55 + 0.45 * local_coverage), 0.0, 1.0)
            arms.append({
                "index": index, "value": value, "mean": mean, "uncertainty": uncertainty,
                "ucb": mean + self.alpha * uncertainty, "count": int(self.counts[index]),
                "support": support, "novelty": novelty,
            })
        return arms

    @staticmethod
    def _wilson_lower(correct, total, z=1.0):
        if total <= 0:
            return 0.0
        p = clamp(float(correct) / float(total), 0.0, 1.0)
        den = 1.0 + z * z / total
        centre = p + z * z / (2.0 * total)
        spread = z * math.sqrt(max(0.0, p * (1.0 - p) / total + z * z / (4.0 * total * total)))
        return clamp((centre - spread) / den, 0.0, 1.0)

    def calibration(self, predicted_idx):
        minimum = max(4, int(OPTIONS.get("confidence_min_validation_samples", 12)))
        total = self.validation_pred_weight[predicted_idx]
        correct = self.validation_pred_correct_weight[predicted_idx]
        accuracy = correct / total if total > 0 else 0.0
        if total < minimum:
            ceiling = 0.25 + 0.35 * clamp(total / float(minimum), 0.0, 1.0)
        else:
            ceiling = self._wilson_lower(correct, total)
        return {"accuracy": accuracy, "ceiling": clamp(ceiling, 0.0, 0.995), "samples": int(round(total))}

    def structural_confidence(self, arms, chosen_idx):
        if self.total_updates <= 0:
            return 0.0
        chosen = arms[chosen_idx]
        ranked = sorted((a["mean"] for a in arms), reverse=True)
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else abs(ranked[0])
        margin_score = 2.0 * abs(1.0 / (1.0 + math.exp(-3.5 * margin)) - 0.5)
        uncertainty_score = math.exp(-1.6 * chosen["uncertainty"])
        coverage = 1.0 - math.exp(-self.total_updates / max(18.0, len(self.actions) * 1.8))
        local = 1.0 - math.exp(-chosen["count"] / 4.0)
        return clamp(coverage * (0.38 * uncertainty_score + 0.27 * margin_score +
                                 0.18 * local + 0.17 * chosen["support"]), 0.0, 0.995)

    def choose(self, features, explore=False, allowed_indices=None):
        arms = self.evaluate(features)
        allowed = set(int(x) for x in allowed_indices) if allowed_indices is not None else set(range(len(arms)))
        candidates = [a for a in arms if a["index"] in allowed]
        if not candidates:
            raise ValueError("allowed action set is empty")
        key = "ucb" if explore else "mean"
        best = max(a[key] for a in candidates)
        tied = [a for a in candidates if abs(a[key] - best) < 1e-12]
        chosen = dict(min(tied, key=lambda a: a["index"]))
        structural = self.structural_confidence(arms, chosen["index"])
        calibration = self.calibration(chosen["index"])
        chosen["structural_confidence"] = structural
        chosen["validation_accuracy"] = calibration["accuracy"]
        chosen["validation_lower_bound"] = calibration["ceiling"]
        chosen["validation_samples"] = calibration["samples"]
        return chosen, min(structural, calibration["ceiling"]), arms

    def validate(self, action_idx, features, reward, sample_ts=None):
        reward = clamp(float(reward), -1.0, 1.0)
        if 0.0 <= reward < 0.15:
            return
        arms = self.evaluate(features)
        predicted = max(arms, key=lambda a: a["mean"])["index"]
        weight = max(0.25, abs(reward)) * self.sample_weight(sample_ts)
        if reward < 0.0:
            if predicted == int(action_idx):
                self.validation_pred_weight[predicted] += weight
            return
        self.validation_pred_weight[predicted] += weight
        if predicted == int(action_idx):
            self.validation_pred_correct_weight[predicted] += weight

    def update(self, action_idx, features, reward, sample_ts=None):
        self.decay()
        action_idx = int(action_idx)
        x = self.dense(features)
        weight = self.sample_weight(sample_ts)
        reward = clamp(float(reward), -1.0, 1.0)
        for i in range(self.dim):
            self.b[action_idx][i] += weight * reward * x[i]
            for j in range(self.dim):
                self.a[action_idx][i][j] += weight * x[i] * x[j]
        self.counts[action_idx] += weight
        self.reward_sums[action_idx] += weight * reward
        self.total_updates += weight

    def export(self):
        return {
            "version": self.VERSION, "feature_indices": self.feature_indices,
            "actions": self.actions, "alpha": self.alpha, "ridge": self.ridge,
            "a": self.a, "b": self.b, "counts": self.counts, "reward_sums": self.reward_sums,
            "total_updates": self.total_updates, "last_decay_ts": self.last_decay_ts,
            "validation_pred_weight": self.validation_pred_weight,
            "validation_pred_correct_weight": self.validation_pred_correct_weight,
        }


class FullRidgeLinUCBBackend(PolicyBackend):
    """PolicyBackend-compatible multi-horizon full-ridge challenger."""
    BACKEND = "full_ridge_linucb"
    VERSION = 1

    def __init__(self, actions, horizons, feature_indices, alpha=0.65, ridge=1.0, model=None):
        self.actions = [float(x) for x in actions]
        self.horizons = [int(x) for x in horizons]
        self.feature_indices = [int(x) for x in feature_indices]
        if not self.feature_indices:
            raise ValueError("FullRidgeLinUCBBackend requires an explicit semantic feature set")
        self.alpha = float(alpha)
        self.ridge = float(ridge)
        self.model_revision = (model or {}).get("model_revision") or str(uuid.uuid4())
        valid = bool(model and model.get("backend") == self.BACKEND and int(model.get("version", 0)) == self.VERSION)
        raw_heads = (model or {}).get("heads", {}) if valid else {}
        self.heads = {
            h: FullRidgeLinUCBHead(self.feature_indices, self.actions, self.alpha, self.ridge, raw_heads.get(str(h)))
            for h in self.horizons
        }

    @property
    def total_updates(self):
        return max([head.total_updates for head in self.heads.values()] or [0.0])

    def predict(self, features, allowed_indices=None):
        candidates = []
        for horizon, head in self.heads.items():
            chosen, confidence, arms = head.choose(features, explore=False, allowed_indices=allowed_indices)
            utility = chosen["mean"] + 0.20 * confidence + 0.18 * chosen["support"] - 0.12 * chosen["novelty"]
            candidates.append((utility, horizon, chosen, confidence, arms))
        candidates.sort(key=lambda row: row[0], reverse=True)
        _, horizon, chosen, confidence, arms = candidates[0]
        return chosen, confidence, arms, horizon, chosen.get("support", 0.0), chosen.get("novelty", 1.0)

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        self.heads[int(horizon)].update(action_idx, features, reward, sample_ts)
        self.model_revision = str(uuid.uuid4())

    def validate(self, horizon, action_idx, features, reward, sample_ts=None):
        self.heads[int(horizon)].validate(action_idx, features, reward, sample_ts)

    def serialize(self):
        raw = json.loads(json.dumps({
            "format": "homemind-policy-backend-v1", "backend": self.BACKEND,
            "version": self.VERSION, "model_revision": self.model_revision,
            "actions": self.actions, "horizons": self.horizons,
            "feature_indices": self.feature_indices, "alpha": self.alpha, "ridge": self.ridge,
            "heads": {str(h): head.export() for h, head in self.heads.items()},
        }, allow_nan=False))
        return serialize_backend_model(
            raw, policy_backend=self.BACKEND, backend_version=self.VERSION
        )

    @classmethod
    def deserialize(cls, raw, **kwargs):
        require_backend(raw, expected=cls.BACKEND)
        if not verify_model_checksum(raw):
            raise ValueError("NEEDS_RETRAIN: full-ridge model checksum mismatch")
        if int(raw.get("version", 0)) != cls.VERSION:
            raise ValueError("NEEDS_RETRAIN: incompatible full-ridge backend version")
        return cls(actions=raw["actions"], horizons=raw["horizons"],
                   feature_indices=raw["feature_indices"], alpha=raw.get("alpha", 0.65),
                   ridge=raw.get("ridge", 1.0), model=raw, **kwargs)

    def decay(self, now=None):
        before = [head.last_decay_ts for head in self.heads.values()]
        for head in self.heads.values():
            head.decay(now)
        if before != [head.last_decay_ts for head in self.heads.values()]:
            self.model_revision = str(uuid.uuid4())

    def diagnostics(self):
        raw = self.serialize()
        return {
            "backend": self.BACKEND, "backend_version": self.VERSION,
            "model_revision": self.model_revision, "effective_updates": self.total_updates,
            "feature_count": len(self.feature_indices), "ridge": self.ridge, "alpha": self.alpha,
            "serialized_bytes": len(json.dumps(raw, separators=(",", ":"))),
            "correlation_model": "full_ridge_matrix", "numerical_solver": "cholesky_bounded_jitter",
        }
