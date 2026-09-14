"""Protect primary context sensors from marginal Sensor Tournament replacements.

Primary sensors are structurally important inputs selected by the existing context
selection logic (primary occupancy/local sensors and behavioural drivers).  They may still
be replaced, but only when the challenger clears a larger hysteresis margin than an
ordinary feature.

The normal tournament margin remains ``context_tournament_min_gain``.  Replacing a
primary feature requires the stricter of that value and
``context_primary_replacement_gain`` (0.07 by default):

    challenger_score > baseline_score + required_margin

This extension changes only schema-slot selection after all ordinary future-only
promotion gates have passed.  It never creates ActionIntent, calls Executor, or touches a
Home Assistant service.
"""
import math

import context_tournament_promotion as promotion
from context import action_values
from context_tournament_metrics import metric_row
from settings import OPTIONS


_ACTIVE_SERVICE = None
_BASE_CHOOSER = None
_PATCHED = False
_MARGIN_EPSILON = 1e-12


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def primary_feature_ids(policy):
    """Return active features that context selection marked as primary/behavioural."""
    meta = dict(getattr(policy, "selection_meta", {}) or {})
    primary = set()
    for key in ("primary_occupancy_sensor", "primary_local_sensor"):
        value = meta.get(key)
        if value:
            primary.add(str(value))
    for key in ("primary_local_sensors", "primary_behavioural_drivers"):
        for value in meta.get(key) or []:
            if value:
                primary.add(str(value))
    active = set(getattr(getattr(policy, "schema", None), "entities", []) or [])
    return primary & {str(x) for x in active}


def primary_replacement_gain(options=None):
    opts = options or OPTIONS
    base = max(0.0, min(1.0, float(opts.get("context_tournament_min_gain", 0.03))))
    primary = max(0.0, min(1.0, float(opts.get("context_primary_replacement_gain", 0.07))))
    # A primary sensor can never have a weaker threshold than an ordinary feature.
    return max(base, primary)


def beats_margin(baseline_score, challenger_score, margin):
    old = _finite(baseline_score)
    new = _finite(challenger_score)
    required = max(0.0, _finite(margin) or 0.0)
    # Compare the delta, with a tiny numerical guard, so an exact decimal boundary such
    # as 0.91 - 0.84 == 0.07 cannot pass merely because of binary float representation.
    return old is not None and new is not None and (new - old) > required + _MARGIN_EPSILON


def replacement_plan(agent, policy, challenger, tournament, baseline_score,
                     challenger_score, options=None):
    """Choose a schema slot while applying a larger margin to primary features.

    Discovery scores only rank which active slot would be cheapest to give up.  The
    actual permission to replace it comes from future-only predictive scores.
    """
    opts = options or OPTIONS
    active = [str(x) for x in (getattr(policy.schema, "entities", []) or [])]
    challenger = str(challenger)
    base_margin = max(0.0, min(1.0, float(opts.get("context_tournament_min_gain", 0.03))))
    primary_margin = primary_replacement_gain(opts)
    primary = primary_feature_ids(policy)

    if challenger in active:
        return {
            "action": "unchanged", "entities": list(active), "replaced": None,
            "replacement_is_primary": False, "required_gain": 0.0,
            "baseline_score": _finite(baseline_score),
            "challenger_score": _finite(challenger_score), "passes": True,
            "primary_features": sorted(primary),
        }

    limit = promotion._selection_limit(agent)
    if len(active) < limit:
        return {
            "action": "append", "entities": active + [challenger], "replaced": None,
            "replacement_is_primary": False, "required_gain": base_margin,
            "baseline_score": _finite(baseline_score),
            "challenger_score": _finite(challenger_score), "passes": True,
            "primary_features": sorted(primary),
        }

    # Explicit user-selected entities retain the stronger protection already present in
    # promotion.py: Sensor Tournament must never silently displace them.
    protected = {str(x) for x in (agent.get("input_entities") or [])}
    removable = [(idx, eid) for idx, eid in enumerate(active) if eid not in protected]
    if not removable:
        return {
            "action": "blocked", "entities": None, "replaced": None,
            "replacement_is_primary": False, "required_gain": None,
            "baseline_score": _finite(baseline_score),
            "challenger_score": _finite(challenger_score), "passes": False,
            "reason": "no_replaceable_schema_slot", "primary_features": sorted(primary),
        }

    scores = dict((tournament or {}).get("feature_scores") or {})
    ranked = sorted(
        removable,
        key=lambda item: (float(scores.get(item[1], 0.0)), -int(item[0]), item[1]),
    )
    skipped_primary = []
    for idx, entity_id in ranked:
        is_primary = entity_id in primary
        required_gain = primary_margin if is_primary else base_margin
        passes = beats_margin(baseline_score, challenger_score, required_gain)
        if not passes:
            if is_primary:
                skipped_primary.append(entity_id)
            continue
        entities = list(active)
        entities[idx] = challenger
        return {
            "action": "replace", "entities": entities, "replaced": entity_id,
            "replacement_is_primary": bool(is_primary),
            "required_gain": float(required_gain),
            "baseline_score": _finite(baseline_score),
            "challenger_score": _finite(challenger_score), "passes": True,
            "primary_features": sorted(primary),
            "skipped_primary_features": skipped_primary,
        }

    return {
        "action": "blocked", "entities": None, "replaced": None,
        "replacement_is_primary": bool(primary),
        "required_gain": float(primary_margin if primary else base_margin),
        "baseline_score": _finite(baseline_score),
        "challenger_score": _finite(challenger_score), "passes": False,
        "reason": "primary_replacement_gain" if skipped_primary else "replacement_hysteresis",
        "primary_features": sorted(primary),
        "skipped_primary_features": skipped_primary,
    }


def _compute_plan(service, agent, policy, challenger, tournament):
    actions = [float(x) for x in action_values(agent)]
    if not actions:
        return replacement_plan(agent, policy, challenger, tournament, None, None)
    model = service._load_shadow_model(agent["id"], challenger, len(actions))
    metrics = metric_row(model, actions)
    return replacement_plan(
        agent, policy, challenger, tournament,
        metrics.get("baseline_score"), metrics.get("challenger_score"),
    )


def install_primary_protection(service):
    """Patch schema-slot choice after the normal promotion state machine is installed."""
    global _ACTIVE_SERVICE, _BASE_CHOOSER, _PATCHED
    _ACTIVE_SERVICE = service
    if _BASE_CHOOSER is None:
        _BASE_CHOOSER = promotion._choose_schema_after_promotion

    if not _PATCHED:
        def choose_with_primary_protection(agent, policy, challenger, tournament):
            active_service = _ACTIVE_SERVICE
            if active_service is None:
                return _BASE_CHOOSER(agent, policy, challenger, tournament)
            plan = _compute_plan(active_service, agent, policy, challenger, tournament)
            previews = getattr(active_service, "_primary_replacement_previews", None)
            if previews is None:
                previews = {}
                active_service._primary_replacement_previews = previews
            previews[(str(agent["id"]), str(challenger))] = dict(plan)
            if plan.get("action") in ("append", "replace", "unchanged"):
                return plan.get("entities"), plan.get("replaced")
            return None, None

        promotion._choose_schema_after_promotion = choose_with_primary_protection
        _PATCHED = True

    if not getattr(service, "_primary_protection_status_installed", False):
        original_status = service.shadow_status

        def status_with_primary_protection(agent):
            payload = original_status(agent)
            tournament = service.state(agent["id"])
            policy = (getattr(service.engine, "models", {}) or {}).get(agent["id"])
            previews = getattr(service, "_primary_replacement_previews", {})
            for row in payload.get("challengers") or []:
                challenger = row.get("entity_id")
                plan = previews.get((str(agent["id"]), str(challenger)))
                if plan is None and policy is not None:
                    plan = _compute_plan(service, agent, policy, challenger, tournament)
                plan = dict(plan or {})
                row["replacement_candidate"] = plan.get("replaced")
                row["replacement_is_primary"] = bool(plan.get("replacement_is_primary"))
                row["replacement_required_gain"] = plan.get("required_gain")
                row["replacement_passes_hysteresis"] = bool(plan.get("passes"))
                row["primary_features"] = list(plan.get("primary_features") or [])
                row["primary_replacement_block_reason"] = plan.get("reason")
            payload["primary_sensor_protection"] = {
                "primary_replacement_gain": primary_replacement_gain(),
                "rule": "challenger_score > baseline_score + primary_replacement_gain",
            }
            return payload

        service.shadow_status = status_with_primary_protection
        service._primary_protection_status_installed = True

    service.primary_feature_ids = primary_feature_ids
    service._primary_protection_installed = True
    return service
