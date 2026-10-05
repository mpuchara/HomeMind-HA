"""Shared training-evidence primitives for Agent Training vNext.

Stage 2 introduces a per-dwell sample budget without changing reward semantics.
Later stages may extend this module with provenance reliability, but sample mass remains
the independent geometric budget assigned to correlated samples from one dwell.
"""
from __future__ import annotations

import math


DEFAULT_ONSET_SAMPLE_MASS = 1.0
DEFAULT_PERSISTENCE_DWELL_BUDGET = 1.0


def bounded_mass(value, default=1.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(0.0, value)


def normalized_dwell_sample_mass(
    sample_count,
    total_budget=DEFAULT_PERSISTENCE_DWELL_BUDGET,
):
    """Return equal per-sample mass whose total is bounded by one dwell budget."""
    try:
        count = int(sample_count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return 0.0
    return bounded_mass(total_budget) / float(count)


USER_EVIDENCE_ORIGINS = frozenset({
    "user", "user_intent", "manual", "manual_feedback", "manual_demonstration",
})


def evidence_weight_for(origin, source):
    """Reliability of one historical observation, independent of reward utility.

    A repeated automatic state is a demonstration, not proof of preference. Explicit
    user evidence stays strong, recognized automation is weaker, and missing provenance
    is weaker still. Upstream cues cannot define OFF/occupancy persistence. These are
    provisional priors, separate from reward and the per-dwell sample budget.
    """
    origin = str(origin or "unknown")
    source = str(source or "onset")
    if origin == "own_command":
        return 0.0
    if origin in USER_EVIDENCE_ORIGINS:
        reliability = 1.0
    elif origin in {"automation", "automation_assisted"}:
        reliability = 0.5
    else:
        reliability = 0.25
    return reliability * (0.35 if source == "upstream" else 1.0)
