"""Compare compact fitting with the frozen full-column 0.14.157 reference.

Synthetic component timing only, with exact old normalization and optimizer as an
independent oracle. No speed threshold or home-control quality claim.
"""
from pathlib import Path
import argparse
import copy
import json
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
from binary_state_classifier import BinaryStateClassifier


def legacy_fit(classifier):
    if min(map(len, classifier.rows)) < 4:
        return
    import numpy as np
    records = []
    for action in (0, 1):
        unique = {row["id"]: row for row in list(classifier.rows[action]) + list(classifier.recent[action])}
        records.extend((action, row) for row in unique.values())
    matrix = np.zeros((len(records), classifier.dims), dtype=np.float64)
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
    weights = np.zeros(4 * classifier.dims, dtype=np.float64)
    bias = 0.0
    for _ in range(160):
        logits = np.clip(design @ weights + bias, -30, 30)
        error = (1 / (1 + np.exp(-logits)) - labels) * masses
        weights -= .25 * (design.T @ error + .002 * weights)
        bias -= .35 * float(error.sum())
    classifier.center, classifier.scale, classifier.weights = center.tolist(), scale.tolist(), weights[:classifier.dims].tolist()
    classifier.hinge_weights = [weights[i*classifier.dims:(i+1)*classifier.dims].tolist() for i in (1, 2, 3)]
    classifier._refresh_active()
    classifier.bias = float(bias)
    classifier.ready = True
    classifier.pending = 0
    classifier.fits += 1


def fixture(dims=512, varying=48, seed=158, rows=128):
    rng = random.Random(seed)
    learner = BinaryStateClassifier(dims)
    indices = list(range(1, dims))
    rng.shuffle(indices)
    selected = indices[:varying]
    for action in (0, 1):
        for row in range(rows):
            features = {i: rng.uniform(-2, 2) + action * .3 for i in selected}
            if len(selected) < dims - 1:
                features[indices[-1]] = 7.0
            record = dict(x=features, weight=rng.uniform(.1, 8), id=row+1)
            learner.rows[action].append(record)
            learner.recent[action].append(record)
        learner.seen[action] = rows
    learner.pending = 64
    return learner


def compare(left, right):
    import numpy as np
    for key in ('center', 'scale', 'weights', 'hinge_weights', 'bias'):
        np.testing.assert_allclose(getattr(left, key), getattr(right, key), rtol=1e-10, atol=1e-11)
    for key in ('ready', 'pending', 'fits', 'seen', 'active'):
        assert getattr(left, key) == getattr(right, key), key
    rng = random.Random(158)
    max_error = 0.
    for _ in range(100):
        features = {i: rng.uniform(-8, 8) for i in range(1, left.dims)}
        a, b = left.score(features), right.score(features)
        assert abs(a-b) < 1e-10
        assert (a > 0) == (b > 0)
        max_error = max(max_error, abs(a-b))
    return max_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--passes', type=int, default=8)
    args = parser.parse_args()
    assert args.passes >= 1
    cases = []
    for width in (12, 48, 128, 511):
        left = fixture(varying=width)
        right = copy.deepcopy(left)
        legacy_fit(left); right.fit()
        times = [[], []]
        for _ in range(args.passes):
            for slot, function in enumerate((lambda: legacy_fit(left), right.fit)):
                started = time.perf_counter()
                function()
                times[slot].append((time.perf_counter()-started)*1000)
        error = compare(left, right)
        cases.append(dict(dims=512, varying_features=width, rows=256,
            old_design_columns=2048, new_design_columns=4*width,
            legacy_median_ms=statistics.median(times[0]), compact_median_ms=statistics.median(times[1]),
            score_max_abs_error=error, coefficients_and_decisions_parity=True))
    print(json.dumps(dict(synthetic=True, passes=args.passes, cases=cases), indent=2))


if __name__ == '__main__':
    main()
