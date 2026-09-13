"""Control qualification helpers.

Historical training may mark a policy as usable for Shadow, but Control requires a
stronger statistical contract: enough held-out samples and a conservative Wilson lower
confidence bound. This module is intentionally pure so it can be reused by HTTP, the
Executor and tests without touching Home Assistant.
"""
from math import sqrt
from settings import OPTIONS, clamp


def wilson_lower_bound(correct, samples, z=1.96):
    samples = int(samples or 0)
    correct = max(0, min(samples, int(correct or 0)))
    if samples <= 0:
        return 0.0
    p = correct / samples
    z = max(0.0, float(z))
    z2 = z * z
    denominator = 1.0 + z2 / samples
    centre = p + z2 / (2.0 * samples)
    margin = z * sqrt((p * (1.0 - p) + z2 / (4.0 * samples)) / samples)
    return clamp((centre - margin) / denominator, 0.0, 1.0)


def assess_control_qualification(agent):
    """Return the stronger qualification required before Control is allowed.

    Shadow can still run for a historically qualified policy. For binary targets, ON and
    OFF are treated as separate safety obligations: each action must have enough held-out
    evidence and each action's 95% Wilson lower bound must clear the configured threshold.
    Non-binary targets use an aggregate Wilson bound until a richer continuous metric is
    introduced.
    """
    detail = dict(agent.get("benchmark_detail") or {})
    counts = dict(detail.get("counts") or {})
    per_action = dict(counts.get("per_action") or {})
    threshold = clamp(float(OPTIONS.get("candidate_benchmark_threshold", 0.78)), 0.0, 1.0)
    min_total = max(1, int(OPTIONS.get("candidate_control_min_samples", 30)))
    min_per_action = max(1, int(OPTIONS.get("candidate_control_min_samples_per_action", 20)))
    z = max(0.0, float(OPTIONS.get("candidate_control_wilson_z", 1.96)))
    samples = int(counts.get("samples") or agent.get("benchmark_samples") or 0)
    correct = int(counts.get("correct") or 0)
    binary = bool(detail.get("balanced")) or agent.get("target_property") == "power"

    action_stats = {}
    for key, raw in per_action.items():
        n = int((raw or {}).get("samples") or 0)
        c = int((raw or {}).get("correct") or 0)
        action_stats[str(key)] = {
            "samples": n,
            "correct": c,
            "accuracy": (c / n) if n else 0.0,
            "lower_bound": wilson_lower_bound(c, n, z),
        }

    if binary:
        populated = [x for x in action_stats.values() if x["samples"] > 0]
        observed = sum(x["accuracy"] for x in populated) / len(populated) if populated else 0.0
        lower = min((x["lower_bound"] for x in populated), default=0.0)
        enough = len(populated) >= 2 and all(x["samples"] >= min_per_action for x in populated)
        passed = bool(enough and all(x["lower_bound"] > threshold for x in populated))
        if len(populated) < 2:
            reason = "Need held-out evidence for both binary actions"
        elif not enough:
            least = min((x["samples"] for x in populated), default=0)
            reason = f"Need at least {min_per_action} held-out samples per action; least-covered action has {least}"
        elif not passed:
            reason = f"95% lower confidence bound {lower:.1%} must exceed {threshold:.0%} for every action"
        else:
            reason = f"Control-qualified: every action has a 95% lower bound above {threshold:.0%}"
    else:
        observed = (correct / samples) if samples else 0.0
        lower = wilson_lower_bound(correct, samples, z)
        enough = samples >= min_total
        passed = bool(enough and lower > threshold)
        if not enough:
            reason = f"Need at least {min_total} held-out samples; have {samples}"
        elif not passed:
            reason = f"95% lower confidence bound {lower:.1%} must exceed {threshold:.0%}"
        else:
            reason = f"Control-qualified: 95% lower bound exceeds {threshold:.0%}"

    return {
        "passed": passed,
        "binary": binary,
        "threshold": threshold,
        "observed_score": observed,
        "lower_bound": lower,
        "samples": samples,
        "minimum_samples": min_total,
        "minimum_samples_per_action": min_per_action if binary else None,
        "confidence_z": z,
        "per_action": action_stats,
        "reason": reason,
    }
