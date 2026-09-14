"""Similarity metric for explicit historical Teach labels."""
import math


def distance(left, right):
    if left.keys() != right.keys():
        return None
    sq = 0.0
    total = 0.0
    for key, rv in right.items():
        diff = abs(float(left[key]) - float(rv))
        if key.startswith("target_options:"):
            if diff > 1e-9:
                return None
            continue
        if key.startswith("time:"):
            weight = 0.05
        elif key.startswith("interaction:"):
            weight = 0.25
        elif "lag_delta" in key:
            weight = 0.70
        else:
            weight = 1.0
        # Presence/motion ON↔OFF is roughly a 2.0 jump and must remain a hard mismatch.
        if ":value" in key and diff > 1.25:
            return None
        if not key.startswith("time:") and diff > 0.80:
            return None
        sq += weight * diff * diff
        total += weight
    rms = math.sqrt(sq / max(total, 1e-9))
    return rms if rms <= 0.20 else None
