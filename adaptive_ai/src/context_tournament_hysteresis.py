"""Strict hysteresis for Sensor Tournament promotion.

Step 9 already introduced ``context_tournament_min_gain`` as the practical improvement
threshold. Step 10 makes that threshold an explicit hysteresis rule instead of a simple
"new is better than old" comparison:

    challenger_score > baseline_score + min_gain

The comparison is deliberately strict. With ``min_gain = 0.03`` an exact +3 percentage
point tie at the boundary is not enough to churn the active schema; the challenger must
clear the boundary. The same rule is applied to cumulative promotion eligibility and to
each consecutive evaluation window.
"""
import math

import context_tournament_promotion as promotion
from context_tournament_metrics import metric_row


_INSTALLED = False
_ORIGINAL_ELIGIBILITY = None
_ORIGINAL_FINALIZE_WINDOW = None


def beats_with_hysteresis(old_score, new_score, min_gain):
    """Return True only when the challenger clears the old score by the full margin."""
    if old_score is None or new_score is None:
        return False
    try:
        old = float(old_score)
        new = float(new_score)
        margin = max(0.0, float(min_gain))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(old) and math.isfinite(new) and math.isfinite(margin)):
        return False
    return new > old + margin


def hysteresis_diagnostics(old_score, new_score, min_gain):
    """Expose the exact comparison used by automatic promotion."""
    try:
        old = None if old_score is None else float(old_score)
        new = None if new_score is None else float(new_score)
        margin = max(0.0, float(min_gain))
    except (TypeError, ValueError):
        old, new, margin = None, None, max(0.0, float(min_gain or 0.0))
    required = None if old is None else old + margin
    excess = None if new is None or required is None else new - required
    return {
        "old_score": old,
        "new_score": new,
        "min_gain": margin,
        "required_new_score": required,
        "hysteresis_excess": excess,
        "passes_hysteresis": beats_with_hysteresis(old, new, margin),
    }


def install():
    """Install strict hysteresis into the existing promotion extension.

    ``context_tournament_promotion`` intentionally owns the promotion state machine. This
    extension replaces only its two score-boundary decisions, leaving sample/day/window/
    cooldown gates, schema migration and the non-controlling Shadow boundary untouched.
    """
    global _INSTALLED, _ORIGINAL_ELIGIBILITY, _ORIGINAL_FINALIZE_WINDOW
    if _INSTALLED:
        return promotion

    _ORIGINAL_ELIGIBILITY = promotion.promotion_eligibility
    _ORIGINAL_FINALIZE_WINDOW = promotion._finalize_one_window

    def eligibility_with_hysteresis(model, actions, now, last_promotion_ts=None, options=None):
        result = _ORIGINAL_ELIGIBILITY(model, actions, now, last_promotion_ts, options)
        metrics = metric_row(model, actions)
        cfg = result.get("config") or promotion.tournament_config(options)
        diag = hysteresis_diagnostics(
            metrics.get("baseline_score"), metrics.get("challenger_score"), cfg.get("min_gain", 0.03)
        )
        checks = dict(result.get("checks") or {})
        # Keep the legacy `gain` gate name for API compatibility, but make it use the
        # strict hysteresis rule. The explicit key makes the semantics visible to UI/logs.
        checks["gain"] = bool(diag["passes_hysteresis"])
        checks["hysteresis"] = bool(diag["passes_hysteresis"])
        result["checks"] = checks
        result["ready"] = all(checks.values())
        result.update(diag)
        return result

    def finalize_window_with_hysteresis(model, actions, config):
        row = metric_row(promotion._window_model(model, len(actions)), actions)
        diag = hysteresis_diagnostics(
            row.get("baseline_score"), row.get("challenger_score"), config.get("min_gain", 0.03)
        )
        gain = row.get("gain")
        won = bool(diag["passes_hysteresis"])
        model["promotion_consecutive_wins"] = (
            int(model.get("promotion_consecutive_wins") or 0) + 1 if won else 0
        )
        model["promotion_completed_windows"] = int(model.get("promotion_completed_windows") or 0) + 1
        model["promotion_last_window_gain"] = gain
        model["promotion_last_window_score"] = row.get("challenger_score")
        model["promotion_last_window_required_score"] = diag.get("required_new_score")
        model["promotion_last_window_hysteresis_excess"] = diag.get("hysteresis_excess")
        history = list(model.get("promotion_window_history") or [])
        history.append({
            "start_ts": float(model.get("promotion_window_start_ts") or 0.0),
            "end_ts": float(model.get("promotion_window_end_ts") or 0.0),
            "samples": int(row.get("samples") or 0),
            "baseline_score": row.get("baseline_score"),
            "challenger_score": row.get("challenger_score"),
            "required_challenger_score": diag.get("required_new_score"),
            "hysteresis_excess": diag.get("hysteresis_excess"),
            "min_gain": diag.get("min_gain"),
            "gain": gain,
            "win": won,
        })
        model["promotion_window_history"] = history[-promotion.WINDOW_HISTORY_LIMIT:]
        return won

    promotion.promotion_eligibility = eligibility_with_hysteresis
    promotion._finalize_one_window = finalize_window_with_hysteresis
    _INSTALLED = True
    return promotion
