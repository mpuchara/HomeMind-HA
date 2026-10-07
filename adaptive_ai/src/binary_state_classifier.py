"""Bounded supervised desired-state learner for accepted binary demonstrations.

The utility-only diagonal head cannot reliably separate two positive numeric
contexts: every accepted action has a positive reward. This small logistic
learner learns the state label, including an intercept, from accepted evidence.
No physical action or unobserved reward is produced here.
"""
from collections import deque
import hashlib
import math


class BinaryStateClassifier:
    CONTRACT = 1
    ROWS_PER_ACTION = 128
    FIT_INTERVAL = 64

    def __init__(self, dims, raw=None):
        self.dims = int(dims)
        self.rows = [deque(maxlen=self.ROWS_PER_ACTION), deque(maxlen=self.ROWS_PER_ACTION)]
        self.recent = [deque(maxlen=8), deque(maxlen=8)]
        self.seen = [0, 0]
        self.pending = 0
        self.fits = 0
        self.ready = False
        self.center = [0.0] * self.dims
        self.scale = [1.0] * self.dims
        self.weights = [0.0] * self.dims
        self.hinge_weights = [[0.0] * self.dims for _ in range(3)]
        self.active = ()
        self.bias = 0.0
        if raw:
            if raw.get("contract") != self.CONTRACT or raw.get("dims") != self.dims:
                raise ValueError("NEEDS_RETRAIN: binary desired-state contract mismatch")
            for name in ("center", "scale", "weights"):
                values = [float(v) for v in raw.get(name, [])]
                if len(values) != self.dims or not all(math.isfinite(v) for v in values):
                    raise ValueError("NEEDS_RETRAIN: invalid binary desired-state coefficients")
                setattr(self, name, values)
            if any(v <= 0 for v in self.scale):
                raise ValueError("NEEDS_RETRAIN: invalid binary desired-state scale")
            hinges = raw.get("hinge_weights") or [[0.0] * self.dims for _ in range(3)]
            self.hinge_weights = [[float(v) for v in row] for row in hinges]
            if len(self.hinge_weights) != 3 or any(len(row) != self.dims or
                    any(not math.isfinite(v) for v in row) for row in self.hinge_weights):
                raise ValueError("NEEDS_RETRAIN: invalid binary desired-state hinges")
            self.bias = float(raw.get("bias", 0))
            if not math.isfinite(self.bias):
                raise ValueError("NEEDS_RETRAIN: invalid binary desired-state bias")
            for action, rows in enumerate(raw.get("rows", [])[:2]):
                for row in rows[-self.ROWS_PER_ACTION:]:
                    x = {int(k): float(v) for k, v in row["x"].items()
                         if 0 < int(k) < self.dims and math.isfinite(float(v))}
                    weight = float(row["weight"])
                    if math.isfinite(weight) and weight > 0:
                        self.rows[action].append({"x": x, "weight": weight, "id": int(row.get("id", len(self.rows[action]) + 1))})
            self.seen = [max(len(self.rows[i]), int((raw.get("seen") or [0, 0])[i])) for i in (0, 1)]
            for action, rows in enumerate(raw.get("recent", [])[:2]):
                for row in rows[-8:]:
                    x = {int(k): float(v) for k, v in row["x"].items()
                         if 0 < int(k) < self.dims and math.isfinite(float(v))}
                    weight = float(row["weight"])
                    if math.isfinite(weight) and weight > 0:
                        self.recent[action].append({"x": x, "weight": weight, "id": int(row["id"])})
            for action in (0, 1):
                self.seen[action] = max([self.seen[action]] + [row["id"] for row in
                    list(self.rows[action]) + list(self.recent[action])])
            self.pending = max(0, min(self.FIT_INTERVAL, int(raw.get("pending", 0))))
            self.fits = max(0, int(raw.get("fits", 0)))
            self.ready = bool(raw.get("ready"))
            self._refresh_active()

    def _refresh_active(self):
        self.active = tuple(i for i in range(self.dims) if self.weights[i] or
                            any(row[i] for row in self.hinge_weights))

    def remap(self, mapping):
        """Preserve the same feature identities during an explicit schema migration."""
        for name, default in (("center", 0.0), ("scale", 1.0), ("weights", 0.0)):
            old = getattr(self, name)
            setattr(self, name, [old[mapping[i]] if i in mapping else default for i in range(self.dims)])
        self.hinge_weights = [[row[mapping[i]] if i in mapping else 0.0
                               for i in range(self.dims)] for row in self.hinge_weights]
        for action in (0, 1):
            visited = set()
            for record in list(self.rows[action]) + list(self.recent[action]):
                if id(record) in visited:
                    continue
                visited.add(id(record))
                old = record["x"]
                record["x"] = {i: old[j] for i, j in mapping.items() if i > 0 and j in old}
        self._refresh_active()

    def add(self, action, features, weight):
        if not math.isfinite(float(weight)) or weight <= 0:
            return
        x = {int(k): float(v) for k, v in features.items()
             if 0 < int(k) < self.dims and math.isfinite(float(v))}
        action = int(action)
        self.seen[action] += 1
        record = {"x": x, "weight": min(8.0, float(weight)), "id": self.seen[action]}
        self.recent[action].append(record)
        if len(self.rows[action]) < self.ROWS_PER_ACTION:
            self.rows[action].append(record)
        else:
            # A deterministic uniform reservoir covers the entire accepted history;
            # the small recent tail makes new manual corrections visible immediately.
            digest = hashlib.blake2b(f"{action}:{self.seen[action]}".encode(), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.seen[action]
            if index < self.ROWS_PER_ACTION:
                self.rows[action][index] = record
        self.pending += 1
        if self.pending >= self.FIT_INTERVAL or not self.ready or weight >= 1.0:
            self.fit()

    def fit(self):
        if min(map(len, self.rows)) < 4:
            return
        import numpy as np
        records = []
        for action in (0, 1):
            unique = {row["id"]: row for row in list(self.rows[action]) + list(self.recent[action])}
            records.extend((action, row) for row in unique.values())
        matrix = np.zeros((len(records), self.dims), dtype=np.float64)
        labels = np.array([action for action, _ in records], dtype=np.float64)
        masses = np.array([row["weight"] for _, row in records], dtype=np.float64)
        # Balance ON/OFF classes, retaining manual/outcome weights within each class.
        for action in (0, 1):
            mask = labels == action
            masses[mask] *= .5 / max(float(masses[mask].sum()), 1e-12)
        for i, (_, row) in enumerate(records):
            for index, value in row["x"].items():
                matrix[i, index] = value
        center = (matrix * masses[:, None]).sum(axis=0)
        variance = ((matrix - center) ** 2 * masses[:, None]).sum(axis=0)
        scale = np.maximum(np.sqrt(variance), .05)
        varying = variance > 1e-10
        z = np.clip((matrix - center) / scale, -6, 6) * varying
        # Data-relative bounded hinges allow several ON signal levels to share a
        # plateau despite noisy labels. A single linear boundary can incorrectly
        # push the weaker stationary level into OFF when entry pulses are stronger.
        design = np.concatenate([z] + [np.clip(z - knot, 0, 1) * varying for knot in (-1, 0, 1)], axis=1)
        weights = np.zeros(4 * self.dims, dtype=np.float64)
        bias = 0.0
        for _ in range(160):
            logits = np.clip(design @ weights + bias, -30, 30)
            error = (1 / (1 + np.exp(-logits)) - labels) * masses
            weights -= .25 * (design.T @ error + .002 * weights)
            bias -= .35 * float(error.sum())
        self.center, self.scale, self.weights = center.tolist(), scale.tolist(), weights[:self.dims].tolist()
        self.hinge_weights = [weights[i*self.dims:(i+1)*self.dims].tolist() for i in (1, 2, 3)]
        self._refresh_active()
        self.bias = float(bias)
        self.ready = True
        self.pending = 0
        self.fits += 1

    def score(self, features):
        logit = self.bias
        for i in self.active:
            coefficient = self.weights[i]
            hinges = [row[i] for row in self.hinge_weights]
            if coefficient or any(hinges):
                value = (float(features.get(i, 0.0)) - self.center[i]) / self.scale[i]
                z = max(-6.0, min(6.0, value))
                logit += coefficient * z
                logit += sum(w * max(0.0, min(1.0, z - knot)) for w, knot in zip(hinges, (-1, 0, 1)))
        return math.tanh(logit / 2)

    def decay(self, factor):
        for action in (0, 1):
            visited = set()
            for row in list(self.rows[action]) + list(self.recent[action]):
                if id(row) not in visited:
                    row["weight"] *= factor
                    visited.add(id(row))

    def export(self):
        return {"contract": self.CONTRACT, "dims": self.dims, "ready": self.ready,
                "center": self.center, "scale": self.scale, "weights": self.weights,
                "hinge_weights": self.hinge_weights,
                "bias": self.bias, "pending": self.pending, "fits": self.fits,
                "rows": [list(rows) for rows in self.rows], "seen": self.seen,
                "recent": [list(rows) for rows in self.recent]}
