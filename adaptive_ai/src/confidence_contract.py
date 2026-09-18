"""Stage 13 confidence semantics and independent future calibration.

This module deliberately separates probability calibration from policy decision quality.
Legacy numeric ``confidence`` fields remain readable for compatibility, but are explicitly
classified as decision-strength / gating scores and are never presented as probabilities
of comfort or correctness.

Candidate promotion gets a fixed-horizon, future-only evaluation epoch. Selection evidence
is frozen first; final evaluation starts afterwards and, once complete, its end timestamp is
locked so regular UI polling cannot enlarge the declared evidence set via optional stopping.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from urllib.parse import urlsplit

from fast_runtime import is_fast_target


CONTRACT_VERSION = 2
PROBABILITY_BINS = 10
DEFAULT_SELECTION_EPISODES = 12
DEFAULT_FINAL_EPISODES = 12
DEFAULT_MIN_PER_ACTION = 4
DEFAULT_DEPENDENCY_WINDOW_SECONDS = 30.0
DEFAULT_HALF_LIFE_EPISODES = 40.0
DEFAULT_OVERCONFIDENCE_GAP = 0.15
DEFAULT_FINAL_MAX_REGRESSION = 0.03
FINAL_CALIBRATION_EVIDENCE_KINDS = {"manual_user_target_change", "independent_preference_label", "episode_evaluator_independent"}
EVALUATION_REVISION_SEPARATOR = "::backend="

METRIC_SEMANTICS = {
    "presence_probability": "calibrated_probability_candidate_0_to_1",
    "forecast_uncertainty": "uncertainty_score_0_to_1_not_probability_of_failure",
    "expected_action_utility": "expected_reward_or_utility_not_probability",
    "data_coverage": "effective_independent_evidence_fraction_not_quality",
    "empirical_policy_quality": "fixed_future_paired_observed_quality_with_interval_not_probability",
    "preference_alignment": "weighted_alignment_lower_bound_not_comfort_probability",
    "decision_strength": "legacy_structural_gate_score_not_probability",
}


def _finite(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _backend_key(model):
    model = dict(model or {})
    backend = str(model.get("backend") or "diagonal_linucb")
    version = str(model.get("version") or "unknown")
    return f"{backend}:v{version}"


def _evaluation_revision(model_revision, backend_key):
    raw = str(model_revision or "unknown")
    if EVALUATION_REVISION_SEPARATOR in raw:
        return raw
    return f"{raw}{EVALUATION_REVISION_SEPARATOR}{backend_key}"


def _source_model_revision(evaluation_revision):
    return str(evaluation_revision or "unknown").split(EVALUATION_REVISION_SEPARATOR, 1)[0]


def effective_sample_size(weights):
    clean = [max(0.0, float(w)) for w in weights if _finite(w) is not None and float(w) > 0]
    if not clean:
        return 0.0
    total = sum(clean)
    denom = sum(w * w for w in clean)
    return (total * total / denom) if denom > 1e-12 else 0.0


def wilson_interval(success_weight, total_weight, effective_n, z=1.96):
    total = max(0.0, float(total_weight))
    n = max(0.0, float(effective_n))
    if total <= 1e-12 or n <= 1e-12:
        return {"mean": None, "lower": None, "upper": None, "effective_n": n}
    p = max(0.0, min(1.0, float(success_weight) / total))
    z2 = float(z) ** 2
    den = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = float(z) * math.sqrt(max(0.0, p * (1.0 - p) / n + z2 / (4.0 * n * n)))
    return {
        "mean": p,
        "lower": max(0.0, (centre - spread) / den),
        "upper": min(1.0, (centre + spread) / den),
        "effective_n": n,
    }


def _episode_weight(index, total, half_life=DEFAULT_HALF_LIFE_EPISODES,
                    recent_full_weight=DEFAULT_FINAL_EPISODES):
    """Decay old evidence while keeping the declared fixed-test window unshrunk."""
    age = max(0, int(total) - 1 - int(index))
    fresh = max(0, int(recent_full_weight))
    if age < fresh:
        return 1.0
    half_life = max(1.0, float(half_life))
    decay_age = age - fresh + 1
    return 0.5 ** (float(decay_age) / half_life)


def _dependency_cluster(row, window=DEFAULT_DEPENDENCY_WINDOW_SECONDS):
    explicit = row.get("dependency_cluster") or row.get("cluster_id")
    if explicit is not None:
        return str(explicit)
    ts = _finite(row.get("outcome_ts"), _finite(row.get("ts"), 0.0)) or 0.0
    root = str(row.get("root_agent_id") or row.get("scope_id") or "global")
    return f"{root}:{int(ts // max(1.0, float(window)))}"


def independent_episode_rows(rows, *, id_key="prediction_event_id", end_ts=None):
    """Deduplicate episode evidence and sort chronologically."""
    unique = {}
    for raw in rows or ():
        row = dict(raw or {})
        ts = _finite(row.get("outcome_ts"), _finite(row.get("ts")))
        if ts is None or (end_ts is not None and ts > float(end_ts)):
            continue
        explicit = row.get(id_key) or row.get("episode_id")
        key = str(explicit) if explicit else f"compat:{row.get('root_agent_id')}:{ts:.6f}"
        previous = unique.get(key)
        previous_ts = (_finite(previous.get("outcome_ts"), _finite(previous.get("ts")))
                       if previous is not None else None)
        if previous is None or previous_ts is None or ts < previous_ts:
            unique[key] = row
    return sorted(
        unique.values(),
        key=lambda row: _finite(row.get("outcome_ts"), _finite(row.get("ts"), 0.0)) or 0.0,
    )


def dependency_adjusted_weights(rows, *, half_life=DEFAULT_HALF_LIFE_EPISODES,
                                window=DEFAULT_DEPENDENCY_WINDOW_SECONDS,
                                recent_full_weight=DEFAULT_FINAL_EPISODES):
    """Return row weights with each dependency cluster capped to one evidence unit."""
    rows = list(rows or ())
    raw = [
        _episode_weight(i, len(rows), half_life, recent_full_weight)
        for i in range(len(rows))
    ]
    clusters = defaultdict(list)
    for idx, row in enumerate(rows):
        clusters[_dependency_cluster(row, window)].append(idx)
    adjusted = list(raw)
    for indices in clusters.values():
        cluster_sum = sum(raw[i] for i in indices)
        if cluster_sum > 1.0:
            scale = 1.0 / cluster_sum
            for idx in indices:
                adjusted[idx] *= scale
    return adjusted


def dependency_effective_sample_size(rows, weights,
                                     window=DEFAULT_DEPENDENCY_WINDOW_SECONDS):
    """Kish effective N over dependency clusters, not repeated rows."""
    cluster_weights = defaultdict(float)
    for row, weight in zip(rows or (), weights or ()):
        value = max(0.0, float(weight))
        if value <= 0:
            continue
        cluster_weights[_dependency_cluster(row, window)] += value
    units = [min(1.0, value) for value in cluster_weights.values() if value > 0]
    return effective_sample_size(units)


def probability_calibration(rows, *, scope_id=None, model_key=None,
                            bins=PROBABILITY_BINS,
                            half_life=DEFAULT_HALF_LIFE_EPISODES):
    """Brier/reliability report from independent labelled probability episodes only."""
    filtered = []
    for raw in rows or ():
        row = dict(raw or {})
        if scope_id is not None and str(row.get("scope_id")) != str(scope_id):
            continue
        if model_key is not None and str(row.get("model_key")) != str(model_key):
            continue
        if not bool(row.get("independent", True)):
            continue
        source_kind = str(row.get("source_kind") or row.get("source") or "")
        if source_kind.startswith("training") or source_kind.startswith("model:"):
            continue
        p = _finite(row.get("prediction"))
        y = _finite(row.get("observed"))
        if p is None or y is None or not (0.0 <= p <= 1.0):
            continue
        row["prediction"] = p
        row["observed"] = 1.0 if y >= .5 else 0.0
        filtered.append(row)
    rows = independent_episode_rows(filtered, id_key="episode_id")
    weights = dependency_adjusted_weights(
        rows,
        half_life=half_life,
        recent_full_weight=DEFAULT_FINAL_EPISODES,
    )
    total_w = sum(weights)
    n_eff = dependency_effective_sample_size(rows, weights)
    bucket = [dict(weight=0.0, prediction=0.0, observed=0.0, episodes=0)
              for _ in range(int(bins))]
    brier_num = 0.0
    forecast_num = 0.0
    observed_num = 0.0
    for row, weight in zip(rows, weights):
        p = row["prediction"]
        y = row["observed"]
        idx = min(int(bins) - 1, max(0, int(p * int(bins))))
        cell = bucket[idx]
        cell["weight"] += weight
        cell["prediction"] += p * weight
        cell["observed"] += y * weight
        cell["episodes"] += 1
        brier_num += ((p - y) ** 2) * weight
        forecast_num += p * weight
        observed_num += y * weight
    reliability = []
    for idx, cell in enumerate(bucket):
        weight = cell["weight"]
        reliability.append({
            "lo": idx / float(bins),
            "hi": (idx + 1) / float(bins),
            "episodes": cell["episodes"],
            "weight": weight,
            "mean_prediction": cell["prediction"] / weight if weight else None,
            "observed_frequency": cell["observed"] / weight if weight else None,
        })
    mean_prediction = forecast_num / total_w if total_w else None
    observed_rate = observed_num / total_w if total_w else None
    gap = None if mean_prediction is None or observed_rate is None else mean_prediction - observed_rate
    overconfident = bool(n_eff >= 8 and gap is not None and gap > DEFAULT_OVERCONFIDENCE_GAP)
    return {
        "metric": "probability_calibration",
        "probability_claim": True,
        "scope_id": scope_id,
        "model_key": model_key,
        "episodes": len(rows),
        "effective_n": n_eff,
        "brier_score": brier_num / total_w if total_w else None,
        "reliability_bins": reliability,
        "mean_prediction": mean_prediction,
        "observed_frequency": observed_rate,
        "calibration_gap": gap,
        "overconfident": overconfident,
        "sufficient_evidence": bool(len(rows) >= DEFAULT_FINAL_EPISODES
                                    and n_eff >= DEFAULT_FINAL_EPISODES),
    }


def _eligible_calibration_row(row):
    kind = str(row.get("evidence_kind") or "")
    return bool(
        row.get("calibration_eligible")
        and kind in FINAL_CALIBRATION_EVIDENCE_KINDS
        and _finite(row.get("calibration_outcome")) is not None
        and row.get("calibration_parent_correct") is not None
        and row.get("calibration_child_correct") is not None
    )


def _calibration_view(row):
    out = dict(row or {})
    if _eligible_calibration_row(out):
        out["outcome"] = float(out["calibration_outcome"])
        out["parent_correct"] = int(out["calibration_parent_correct"])
        out["child_correct"] = int(out["calibration_child_correct"])
    return out


def action_quality_report(rows, *, scope_id=None, end_ts=None,
                          min_total=DEFAULT_FINAL_EPISODES,
                          min_per_action=DEFAULT_MIN_PER_ACTION,
                          half_life=DEFAULT_HALF_LIFE_EPISODES,
                          correct_key="child_correct",
                          confidence_key="child_confidence",
                          require_calibration_eligible=False):
    """Episode-level action quality; never interprets confidence as a probability."""
    selected = []
    source_counts = defaultdict(int)
    for raw in rows or ():
        row = dict(raw or {})
        if scope_id is not None:
            candidate_scope = row.get("scope_id") or row.get("root_agent_id")
            if str(candidate_scope) != str(scope_id):
                continue
        if require_calibration_eligible and not _eligible_calibration_row(row):
            continue
        source_counts[str(row.get("evidence_kind") or "unclassified")] += 1
        selected.append(row)
    rows = independent_episode_rows(selected, end_ts=end_ts)
    weights = dependency_adjusted_weights(
        rows,
        half_life=half_life,
        recent_full_weight=max(1, int(min_total)),
    )
    by_action = {"OFF": [], "ON": []}
    all_success = 0.0
    all_weight = 0.0
    strengths_num = 0.0
    strengths_weight = 0.0
    for row, weight in zip(rows, weights):
        outcome = 1 if float(row.get("outcome") or 0.0) >= .5 else 0
        correct = bool(row.get(correct_key))
        name = "ON" if outcome else "OFF"
        by_action[name].append((row, correct, weight))
        all_weight += weight
        all_success += weight if correct else 0.0
        strength = _finite(row.get(confidence_key))
        if strength is not None:
            strengths_num += max(0.0, min(1.0, strength)) * weight
            strengths_weight += weight
    total_eff = dependency_effective_sample_size(rows, weights)
    overall = wilson_interval(all_success, all_weight, total_eff)
    per_action = {}
    for name, values in by_action.items():
        action_rows = [row for row, _, _ in values]
        action_weights = [weight for _, _, weight in values]
        total = sum(action_weights)
        success = sum(weight for _, correct, weight in values if correct)
        eff = dependency_effective_sample_size(action_rows, action_weights)
        interval = wilson_interval(success, total, eff)
        per_action[name] = {
            "episodes": len(values),
            "effective_n": eff,
            "accuracy": interval["mean"],
            "quality_lower_bound": interval["lower"],
            "quality_upper_bound": interval["upper"],
            "error_rate": None if interval["mean"] is None else 1.0 - interval["mean"],
            "sufficient_evidence": bool(len(values) >= int(min_per_action)
                                        and eff >= float(min_per_action)),
        }
    strength = strengths_num / strengths_weight if strengths_weight else None
    observed = overall["mean"]
    overstated = bool(total_eff >= 8 and strength is not None and observed is not None
                      and strength - observed > DEFAULT_OVERCONFIDENCE_GAP)
    ready = bool(
        len(rows) >= int(min_total)
        and total_eff >= float(min_total)
        and per_action["OFF"]["sufficient_evidence"]
        and per_action["ON"]["sufficient_evidence"]
    )
    return {
        "metric": "future_episode_action_quality",
        "probability_claim": False,
        "correctness_key": str(correct_key),
        "confidence_key": str(confidence_key),
        "calibration_eligible_only": bool(require_calibration_eligible),
        "evidence_kinds": dict(source_counts),
        "episodes": len(rows),
        "effective_n": total_eff,
        "accuracy": overall["mean"],
        "quality_lower_bound": overall["lower"],
        "quality_upper_bound": overall["upper"],
        "episode_error_rate": None if observed is None else 1.0 - observed,
        "mean_binary_cost": None if observed is None else 1.0 - observed,
        "per_action": per_action,
        "decision_strength_mean": strength,
        "decision_strength_semantics": METRIC_SEMANTICS["decision_strength"],
        "decision_strength_overstated": overstated,
        "sufficient_evidence": ready,
        "recommendation": "evaluate" if ready else "abstain_insufficient_independent_evidence",
    }


def _paired_delta_interval(rows, weights, *, max_regression):
    if not rows:
        return {
            "mean_delta": None, "lower": None, "upper": None, "effective_n": 0.0,
            "non_regression_passed": False, "max_allowed_regression": float(max_regression),
        }
    deltas = [
        (1.0 if bool(row.get("child_correct")) else 0.0)
        - (1.0 if bool(row.get("parent_correct")) else 0.0)
        for row in rows
    ]
    total = sum(weights)
    n_eff = dependency_effective_sample_size(rows, weights)
    if total <= 1e-12 or n_eff <= 1e-12:
        return {
            "mean_delta": None, "lower": None, "upper": None, "effective_n": n_eff,
            "non_regression_passed": False, "max_allowed_regression": float(max_regression),
        }
    mean = sum(w * d for w, d in zip(weights, deltas)) / total
    variance = sum(w * ((d - mean) ** 2) for w, d in zip(weights, deltas)) / total
    se = math.sqrt(max(0.0, variance) / max(1.0, n_eff))
    lower = max(-1.0, mean - 1.96 * se)
    upper = min(1.0, mean + 1.96 * se)
    return {
        "mean_delta": mean,
        "lower": lower,
        "upper": upper,
        "effective_n": n_eff,
        "max_allowed_regression": float(max_regression),
        "non_regression_passed": bool(lower >= -float(max_regression)),
        "interval": "paired_weighted_normal_95pct",
    }


def paired_future_quality_report(rows, *, scope_id=None, end_ts=None,
                                 min_total=DEFAULT_FINAL_EPISODES,
                                 min_per_action=DEFAULT_MIN_PER_ACTION,
                                 max_regression=DEFAULT_FINAL_MAX_REGRESSION):
    """Fixed future comparison on the same independent labelled episodes for parent/child."""
    selected = []
    for raw in rows or ():
        row = dict(raw or {})
        candidate_scope = row.get("scope_id") or row.get("root_agent_id")
        if scope_id is not None and str(candidate_scope) != str(scope_id):
            continue
        if not _eligible_calibration_row(row):
            continue
        selected.append(_calibration_view(row))
    selected = independent_episode_rows(selected, end_ts=end_ts)
    weights = dependency_adjusted_weights(
        selected,
        recent_full_weight=max(1, int(min_total)),
    )
    child = action_quality_report(
        selected, scope_id=None, end_ts=end_ts,
        min_total=min_total, min_per_action=min_per_action,
        correct_key="child_correct", confidence_key="child_confidence",
        require_calibration_eligible=True,
    )
    parent = action_quality_report(
        selected, scope_id=None, end_ts=end_ts,
        min_total=min_total, min_per_action=min_per_action,
        correct_key="parent_correct", confidence_key="parent_confidence",
        require_calibration_eligible=True,
    )
    overall = _paired_delta_interval(selected, weights, max_regression=max_regression)
    per_action = {}
    for action_name, action_value in (("OFF", 0), ("ON", 1)):
        subset, subset_weights = [], []
        for row, weight in zip(selected, weights):
            outcome = 1 if float(row.get("outcome") or 0.0) >= .5 else 0
            if outcome == action_value:
                subset.append(row)
                subset_weights.append(weight)
        interval = _paired_delta_interval(subset, subset_weights, max_regression=max_regression)
        interval["sufficient_evidence"] = bool(
            child["per_action"][action_name]["sufficient_evidence"]
            and parent["per_action"][action_name]["sufficient_evidence"]
        )
        per_action[action_name] = interval

    evidence_ready = bool(child["sufficient_evidence"] and parent["sufficient_evidence"])
    non_regression = bool(
        evidence_ready
        and overall["non_regression_passed"]
        and per_action["OFF"]["sufficient_evidence"]
        and per_action["OFF"]["non_regression_passed"]
        and per_action["ON"]["sufficient_evidence"]
        and per_action["ON"]["non_regression_passed"]
    )
    return {
        "metric": "fixed_future_paired_policy_quality",
        "probability_claim": False,
        "evidence_contract": "future_explicit_independent_preference_or_episode_labels_only",
        "episodes": len(selected),
        "effective_n": child["effective_n"],
        "sufficient_evidence": evidence_ready,
        "child": child,
        "parent": parent,
        "paired_delta": overall,
        "per_action_delta": per_action,
        "promotion_quality_passed": non_regression,
        "recommendation": (
            "pass_paired_non_regression"
            if non_regression else
            "fail_paired_quality" if evidence_ready else
            "abstain_insufficient_independent_evidence"
        ),
    }


def record_independent_candidate_label(store, *, parent_generation_id, child_generation_id,
                                       prediction_event_id, desired_action, source_kind,
                                       source_id, dependency_cluster=None):
    """Attach one immutable independent label without rewriting the raw target transition."""
    kind = str(source_kind or "")
    if kind not in FINAL_CALIBRATION_EVIDENCE_KINDS - {"manual_user_target_change"}:
        raise ValueError("unsupported independent Candidate calibration source")
    desired = 1.0 if float(desired_action) >= .5 else 0.0
    with store.lock, store.conn() as c:
        row = c.execute(
            """SELECT parent_prediction,child_prediction,calibration_source_id
               FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=? AND prediction_event_id=?
               ORDER BY outcome_ts DESC LIMIT 1""",
            (str(parent_generation_id), str(child_generation_id), str(prediction_event_id)),
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        existing = row.get("calibration_source_id")
        if existing:
            # Immutable/idempotent: the first independent calibration fact wins.
            return False
        parent_ok = int((1.0 if float(row["parent_prediction"]) >= .5 else 0.0) == desired)
        child_ok = int((1.0 if float(row["child_prediction"]) >= .5 else 0.0) == desired)
        c.execute(
            """UPDATE candidate_generation_pairs
               SET evidence_kind=?,calibration_eligible=1,dependency_cluster=?,
                   calibration_outcome=?,calibration_parent_correct=?,
                   calibration_child_correct=?,calibration_source_id=?
               WHERE parent_generation_id=? AND child_generation_id=? AND prediction_event_id=?
                 AND (calibration_source_id IS NULL OR calibration_source_id=?)""",
            (
                kind,
                None if dependency_cluster is None else str(dependency_cluster),
                desired, parent_ok, child_ok, str(source_id),
                str(parent_generation_id), str(child_generation_id), str(prediction_event_id),
                str(source_id),
            ),
        )
        return c.execute("SELECT changes()").fetchone()[0] > 0


def _table_exists(connection, name):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (str(name),),
    ).fetchone())


def _ensure_pair_revision_tracking(store):
    """Install O(1) edge revision tracking once candidate pair storage exists.

    Existing historical rows intentionally do not need a bootstrap scan. The first
    report on an old edge computes from source evidence and caches revision 0; every
    subsequent insert/update advances the durable revision through SQLite triggers.
    """
    if getattr(store, "_confidence_pair_revision_tracking_ready", False):
        return True
    with store.lock, store.conn() as c:
        if not _table_exists(c, "candidate_generation_pairs"):
            return False
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS confidence_pair_revisions (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                selection_revision INTEGER NOT NULL DEFAULT 0,
                calibration_revision INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id)
            );

            CREATE TRIGGER IF NOT EXISTS trg_confidence_pair_insert_revision
            AFTER INSERT ON candidate_generation_pairs
            BEGIN
                INSERT INTO confidence_pair_revisions
                    (parent_generation_id,child_generation_id,selection_revision,
                     calibration_revision,updated_ts)
                VALUES(
                    NEW.parent_generation_id,NEW.child_generation_id,1,
                    CASE WHEN COALESCE(NEW.calibration_eligible,0)=1 THEN 1 ELSE 0 END,
                    CAST(strftime('%s','now') AS REAL)
                )
                ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                    selection_revision=confidence_pair_revisions.selection_revision+1,
                    calibration_revision=confidence_pair_revisions.calibration_revision+
                        CASE WHEN COALESCE(NEW.calibration_eligible,0)=1 THEN 1 ELSE 0 END,
                    updated_ts=excluded.updated_ts;
            END;

            CREATE TRIGGER IF NOT EXISTS trg_confidence_pair_update_revision
            AFTER UPDATE ON candidate_generation_pairs
            BEGIN
                INSERT INTO confidence_pair_revisions
                    (parent_generation_id,child_generation_id,selection_revision,
                     calibration_revision,updated_ts)
                VALUES(
                    NEW.parent_generation_id,NEW.child_generation_id,1,1,
                    CAST(strftime('%s','now') AS REAL)
                )
                ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                    selection_revision=confidence_pair_revisions.selection_revision+1,
                    calibration_revision=confidence_pair_revisions.calibration_revision+1,
                    updated_ts=excluded.updated_ts;
            END;
            """
        )
    store._confidence_pair_revision_tracking_ready = True
    return True


def _pair_revisions(store, parent_gid, child_gid):
    if not _ensure_pair_revision_tracking(store):
        return {"selection_revision": 0, "calibration_revision": 0}
    with store.conn() as c:
        row = c.execute(
            """SELECT selection_revision,calibration_revision
               FROM confidence_pair_revisions
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
    return dict(row) if row else {"selection_revision": 0, "calibration_revision": 0}


def _selection_pair_rows(store, parent_gid, child_gid):
    """Minimal source rows required by the selection-quality contract."""
    with store.conn() as c:
        return [dict(row) for row in c.execute(
            """SELECT root_agent_id,prediction_event_id,outcome_ts,outcome,
                      parent_confidence,child_confidence,parent_correct,child_correct,
                      dependency_cluster,evidence_kind,calibration_eligible,
                      calibration_outcome,calibration_parent_correct,
                      calibration_child_correct,calibration_source_id
               FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=?
               ORDER BY outcome_ts""",
            (str(parent_gid), str(child_gid)),
        ).fetchall()]


def _final_pair_rows(store, parent_gid, child_gid, cutoff, end_ts=None):
    """Read only independent-final-evaluation candidates, never screening history."""
    kinds = sorted(FINAL_CALIBRATION_EVIDENCE_KINDS)
    placeholders = ",".join("?" for _ in kinds)
    sql = f"""
        SELECT root_agent_id,prediction_event_id,outcome_ts,outcome,
               parent_confidence,child_confidence,parent_correct,child_correct,
               dependency_cluster,evidence_kind,calibration_eligible,
               calibration_outcome,calibration_parent_correct,
               calibration_child_correct,calibration_source_id
        FROM candidate_generation_pairs
        WHERE parent_generation_id=? AND child_generation_id=?
          AND outcome_ts>?
          AND calibration_eligible=1
          AND evidence_kind IN ({placeholders})
          AND calibration_outcome IS NOT NULL
          AND calibration_parent_correct IS NOT NULL
          AND calibration_child_correct IS NOT NULL
    """
    params = [str(parent_gid), str(child_gid), float(cutoff)] + kinds
    if end_ts is not None:
        sql += " AND outcome_ts<=?"
        params.append(float(end_ts))
    sql += " ORDER BY outcome_ts"
    with store.conn() as c:
        return [dict(row) for row in c.execute(sql, params).fetchall()]


def _cache_fingerprint(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS confidence_probability_episodes (
                metric_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                ts REAL NOT NULL,
                prediction REAL NOT NULL,
                observed REAL NOT NULL,
                source_kind TEXT NOT NULL,
                dependency_cluster TEXT,
                independent INTEGER NOT NULL,
                created_ts REAL NOT NULL,
                PRIMARY KEY(metric_id,model_key,scope_id,episode_id)
            );
            CREATE INDEX IF NOT EXISTS idx_confidence_probability_scope
                ON confidence_probability_episodes(metric_id,model_key,scope_id,ts);

            CREATE TABLE IF NOT EXISTS confidence_evaluation_epochs (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                model_revision TEXT NOT NULL,
                backend_key TEXT NOT NULL,
                contract_version INTEGER NOT NULL,
                selection_cutoff_ts REAL NOT NULL,
                final_target INTEGER NOT NULL,
                min_per_action INTEGER NOT NULL,
                final_end_ts REAL,
                created_ts REAL NOT NULL,
                completed_ts REAL,
                PRIMARY KEY(parent_generation_id,child_generation_id,model_revision,contract_version)
            );
            CREATE INDEX IF NOT EXISTS idx_confidence_epoch_child
                ON confidence_evaluation_epochs(child_generation_id,created_ts DESC);

            CREATE TABLE IF NOT EXISTS confidence_selection_scan_cache (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                evaluation_revision TEXT NOT NULL,
                contract_version INTEGER NOT NULL,
                selection_revision INTEGER NOT NULL,
                params_fingerprint TEXT NOT NULL,
                sufficient INTEGER NOT NULL,
                scanned_rows INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id,evaluation_revision,contract_version)
            );

            CREATE TABLE IF NOT EXISTS confidence_final_report_cache (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                evaluation_revision TEXT NOT NULL,
                contract_version INTEGER NOT NULL,
                calibration_revision INTEGER NOT NULL,
                params_fingerprint TEXT NOT NULL,
                report_json TEXT NOT NULL,
                scanned_rows INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id,evaluation_revision,contract_version)
            );

            CREATE TABLE IF NOT EXISTS confidence_probability_revisions (
                metric_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(metric_id,model_key,scope_id)
            );

            CREATE TABLE IF NOT EXISTS confidence_probability_report_cache (
                metric_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                contract_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                scanned_rows INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(metric_id,model_key,scope_id,contract_version)
            );
            """
        )
    _ensure_pair_revision_tracking(store)


class ProbabilityCalibrationJournal:
    def __init__(self, store):
        self.store = store
        ensure_tables(store)

    def record(self, *, metric_id, model_key, scope_id, episode_id, ts,
               prediction, observed, source_kind, dependency_cluster=None,
               independent=True):
        source_kind = str(source_kind or "").strip()
        if not episode_id:
            raise ValueError("Independent calibration requires a stable episode_id")
        if not source_kind or source_kind.startswith("model:") or source_kind.startswith("training"):
            independent = False
        p = _finite(prediction)
        y = _finite(observed)
        if p is None or y is None or not 0.0 <= p <= 1.0:
            raise ValueError("Probability calibration values must be finite and prediction within [0,1]")
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO confidence_probability_episodes
                   (metric_id,model_key,scope_id,episode_id,ts,prediction,observed,source_kind,
                    dependency_cluster,independent,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (str(metric_id), str(model_key), str(scope_id), str(episode_id), float(ts),
                 p, 1.0 if y >= .5 else 0.0, source_kind,
                 None if dependency_cluster is None else str(dependency_cluster),
                 1 if independent else 0, now),
            )
            inserted = c.execute("SELECT changes()").fetchone()[0] > 0
        return inserted

    def rows(self, metric_id, model_key, scope_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                """SELECT * FROM confidence_probability_episodes
                   WHERE metric_id=? AND model_key=? AND scope_id=? ORDER BY ts""",
                (str(metric_id), str(model_key), str(scope_id)),
            ).fetchall()]

    def report(self, metric_id, model_key, scope_id):
        return probability_calibration(
            self.rows(metric_id, model_key, scope_id),
            scope_id=str(scope_id),
            model_key=str(model_key),
        )


class EvaluationEpochJournal:
    """Freeze challenger selection data before collecting a fixed future final test.

    ``model_revision`` in persistence is an evaluation revision composed from the source
    model revision and backend key. This preserves additive schema compatibility while
    guaranteeing that a backend switch cannot inherit an old calibration epoch even when
    the source revision string happens to be reused.
    """

    def __init__(self, store):
        self.store = store
        ensure_tables(store)

    def get(self, parent_gid, child_gid, model_revision, backend_key=None):
        requested = str(model_revision)
        if backend_key is not None:
            requested = _evaluation_revision(requested, backend_key)
        with self.store.conn() as c:
            row = c.execute(
                """SELECT * FROM confidence_evaluation_epochs
                   WHERE parent_generation_id=? AND child_generation_id=?
                     AND model_revision=? AND contract_version=?""",
                (str(parent_gid), str(child_gid), requested, CONTRACT_VERSION),
            ).fetchone()
            if row is None and backend_key is None and EVALUATION_REVISION_SEPARATOR not in requested:
                row = c.execute(
                    """SELECT * FROM confidence_evaluation_epochs
                       WHERE parent_generation_id=? AND child_generation_id=?
                         AND model_revision LIKE ? AND contract_version=?
                       ORDER BY created_ts DESC LIMIT 1""",
                    (str(parent_gid), str(child_gid),
                     requested + EVALUATION_REVISION_SEPARATOR + "%", CONTRACT_VERSION),
                ).fetchone()
        return dict(row) if row else None

    def ensure(self, parent_gid, child_gid, model_revision, backend_key, pairs,
               *, selection_target=DEFAULT_SELECTION_EPISODES,
               final_target=DEFAULT_FINAL_EPISODES,
               min_per_action=DEFAULT_MIN_PER_ACTION):
        evaluation_revision = _evaluation_revision(model_revision, backend_key)
        existing = self.get(parent_gid, child_gid, evaluation_revision)
        if existing:
            return existing
        rows = independent_episode_rows(pairs)
        selection = action_quality_report(
            rows,
            scope_id=None,
            min_total=selection_target,
            min_per_action=min_per_action,
        )
        if not selection["sufficient_evidence"]:
            return None
        cutoff = max(float(row.get("outcome_ts") or 0.0) for row in rows)
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO confidence_evaluation_epochs
                   (parent_generation_id,child_generation_id,model_revision,backend_key,
                    contract_version,selection_cutoff_ts,final_target,min_per_action,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(parent_gid), str(child_gid), evaluation_revision, str(backend_key),
                 CONTRACT_VERSION, cutoff, int(final_target), int(min_per_action), now),
            )
        return self.get(parent_gid, child_gid, evaluation_revision)

    def final_report(self, epoch, pairs, *, scope_id=None,
                     max_regression=DEFAULT_FINAL_MAX_REGRESSION):
        if not epoch:
            return {
                "status": "selection_evidence_insufficient",
                "sufficient_evidence": False,
                "promotion_quality_passed": False,
                "recommendation": "abstain_selection_not_frozen",
                "contract_version": CONTRACT_VERSION,
            }
        cutoff = float(epoch["selection_cutoff_ts"])
        rows = [
            row for row in independent_episode_rows(pairs)
            if float(row.get("outcome_ts") or 0.0) > cutoff
        ]
        end_ts = _finite(epoch.get("final_end_ts"))
        report = paired_future_quality_report(
            rows,
            scope_id=scope_id,
            end_ts=end_ts,
            min_total=int(epoch["final_target"]),
            min_per_action=int(epoch["min_per_action"]),
            max_regression=max_regression,
        )

        # Freeze the declared test at the first point where independent evidence is
        # sufficient, regardless of whether quality passes. A failed holdout cannot be
        # healed by peeking at later observations.
        if end_ts is None and report["sufficient_evidence"]:
            locked_end = None
            for idx in range(1, len(rows) + 1):
                prefix = paired_future_quality_report(
                    rows[:idx],
                    scope_id=scope_id,
                    min_total=int(epoch["final_target"]),
                    min_per_action=int(epoch["min_per_action"]),
                    max_regression=max_regression,
                )
                if prefix["sufficient_evidence"]:
                    locked_end = float(rows[idx - 1].get("outcome_ts") or 0.0)
                    report = prefix
                    break
            if locked_end is not None:
                now = time.time()
                with self.store.lock, self.store.conn() as c:
                    c.execute(
                        """UPDATE confidence_evaluation_epochs SET final_end_ts=?,completed_ts=?
                           WHERE parent_generation_id=? AND child_generation_id=?
                             AND model_revision=? AND contract_version=? AND final_end_ts IS NULL""",
                        (locked_end, now, epoch["parent_generation_id"], epoch["child_generation_id"],
                         epoch["model_revision"], CONTRACT_VERSION),
                    )
                epoch = self.get(
                    epoch["parent_generation_id"],
                    epoch["child_generation_id"],
                    epoch["model_revision"],
                )
                report = paired_future_quality_report(
                    rows,
                    scope_id=scope_id,
                    end_ts=epoch.get("final_end_ts"),
                    min_total=int(epoch["final_target"]),
                    min_per_action=int(epoch["min_per_action"]),
                    max_regression=max_regression,
                )

        complete = bool(report.get("sufficient_evidence"))
        passed = bool(report.get("promotion_quality_passed"))
        report.update({
            "status": (
                "complete_passed" if complete and passed else
                "complete_failed_quality" if complete else
                "collecting_fixed_future_test"
            ),
            "contract_version": CONTRACT_VERSION,
            "selection_cutoff_ts": cutoff,
            "final_target": int(epoch["final_target"]),
            "min_per_action": int(epoch["min_per_action"]),
            "max_allowed_regression": float(max_regression),
            "final_end_ts": epoch.get("final_end_ts"),
            "backend_key": epoch.get("backend_key"),
            "model_revision": _source_model_revision(epoch.get("model_revision")),
            "evaluation_revision": epoch.get("model_revision"),
            "peek_safe": True,
            "optional_stopping_protection": "lock_on_evidence_completion_not_on_quality_success",
        })
        return report


def contract_descriptor():
    return {
        "version": CONTRACT_VERSION,
        "metric_semantics": dict(METRIC_SEMANTICS),
        "selection_min_independent_episodes": DEFAULT_SELECTION_EPISODES,
        "selection_evidence_semantics": "behavioural_screening_may_include_external_transitions_not_final_calibration",
        "final_min_independent_episodes": DEFAULT_FINAL_EPISODES,
        "final_min_per_action": DEFAULT_MIN_PER_ACTION,
        "final_max_allowed_regression": DEFAULT_FINAL_MAX_REGRESSION,
        "final_calibration_evidence_kinds": sorted(FINAL_CALIBRATION_EVIDENCE_KINDS),
        "dependency_window_seconds": DEFAULT_DEPENDENCY_WINDOW_SECONDS,
        "probability_metrics": ["brier_score", "reliability_bins"],
        "action_metrics": [
            "episode_error_rate", "mean_binary_cost",
            "quality_lower_bound", "quality_upper_bound",
            "paired_delta", "per_action_delta",
        ],
        "promotion_rule": (
            "selection evidence freezes first; promotion needs a later fixed future test "
            "from independent preference labels, separate ON/OFF effective evidence and "
            "paired child-vs-parent non-regression"
        ),
        "legacy_confidence": "compatibility_only_decision_strength_not_probability",
        "backend_recalibration": "evaluation revision includes backend identity",
        "automation_replay": "screening_only_not_final_calibration_evidence",
        "abstain": "insufficient independent evidence or failed fixed holdout keeps Shadow/fallback",
    }


def _generation_for_candidate(store, candidate_id):
    with store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
            (str(candidate_id),),
        ).fetchone()
    return dict(row) if row else None


def _pair_rows(store, parent_generation_id, child_generation_id):
    with store.conn() as c:
        return [dict(row) for row in c.execute(
            """SELECT * FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=? ORDER BY outcome_ts""",
            (str(parent_generation_id), str(child_generation_id)),
        ).fetchall()]


def _decorate_summary(manager, epochs, row, summary, parent, candidate):
    summary = dict(summary or {})
    descriptor = contract_descriptor()
    summary.setdefault("confidence_contract", descriptor)
    if not is_fast_target(parent) or str(parent.get("target_property") or "") != "power":
        summary["confidence_contract"] = {
            **descriptor,
            "final_evaluation": {
                "status": "not_applicable_non_fast_binary",
                "sufficient_evidence": None,
            },
        }
        summary["decision_strength_semantics"] = METRIC_SEMANTICS["decision_strength"]
        return summary

    generation = _generation_for_candidate(manager.store, row.get("candidate_id"))
    if not generation or not generation.get("parent_generation_id"):
        summary["confidence_contract"] = descriptor
        return summary

    parent_gid = str(generation["parent_generation_id"])
    child_gid = str(generation["generation_id"])
    pairs = _pair_rows(manager.store, parent_gid, child_gid)
    model = manager.store.get_model(candidate["id"]) if candidate and candidate.get("id") else None
    model_revision = str(
        (model or {}).get("model_revision")
        or generation.get("model_revision")
        or "unknown"
    )
    backend_key = _backend_key(model)
    selection_target = max(DEFAULT_SELECTION_EPISODES, int(summary.get("required_future_samples") or 0))
    min_per_action = max(DEFAULT_MIN_PER_ACTION, int(summary.get("required_future_samples_per_action") or 0))
    epoch = epochs.ensure(
        parent_gid,
        child_gid,
        model_revision,
        backend_key,
        pairs,
        selection_target=selection_target,
        final_target=DEFAULT_FINAL_EPISODES,
        min_per_action=min_per_action,
    )
    max_regression = DEFAULT_FINAL_MAX_REGRESSION
    try:
        from settings import OPTIONS
        max_regression = max(
            0.0,
            float(OPTIONS.get("agent_candidate_max_accuracy_regression", DEFAULT_FINAL_MAX_REGRESSION)),
        )
    except Exception:
        pass
    final = epochs.final_report(
        epoch,
        pairs,
        scope_id=str(generation.get("root_agent_id") or ""),
        max_regression=max_regression,
    )

    legacy_preference = summary.get("preference_confidence")
    summary["preference_alignment_score"] = legacy_preference
    summary["preference_alignment_semantics"] = METRIC_SEMANTICS["preference_alignment"]
    summary["decision_strength_semantics"] = METRIC_SEMANTICS["decision_strength"]
    summary["confidence_contract"] = {
        **descriptor,
        "backend_key": backend_key,
        "model_revision": model_revision,
        "final_evaluation": final,
        "selection_metric": summary.get("comparison_metric"),
        "selection_preference_alignment": legacy_preference,
    }

    gates = dict(summary.get("promotion_gates") or {})
    old_pref = gates.get("preference_evidence")
    if isinstance(old_pref, dict):
        old_pref = dict(old_pref)
        old_pref["reason"] = (
            "preference alignment reached the selection threshold"
            if old_pref.get("passed")
            else "preference alignment is below the selection threshold"
        )
        old_pref["metric_semantics"] = METRIC_SEMANTICS["preference_alignment"]
        observed = dict(old_pref.get("observed") or {})
        observed["probability_claim"] = False
        old_pref["observed"] = observed
        gates["preference_evidence"] = old_pref

    final_passed = bool(final.get("promotion_quality_passed"))
    gates["independent_final_evaluation"] = {
        "passed": final_passed,
        "reason": (
            "fixed future independent preference evaluation passed paired child-vs-parent non-regression"
            if final_passed else
            "fixed future evidence is complete but Candidate failed paired quality/non-regression"
            if final.get("sufficient_evidence") else
            "fixed future independent preference evidence is incomplete; remain Shadow/fallback"
        ),
        "custom_override": "never",
        "metric_semantics": METRIC_SEMANTICS["empirical_policy_quality"],
        "observed": final,
    }
    summary["promotion_gates"] = gates
    vetoes = [
        dict(item) for item in (summary.get("promotion_vetoes") or [])
        if item.get("gate") != "independent_final_evaluation"
    ]
    if not final_passed:
        vetoes.append({
            "gate": "independent_final_evaluation",
            "reason": gates["independent_final_evaluation"]["reason"],
            "custom_override": "never",
        })
    summary["promotion_vetoes"] = vetoes
    summary["promotion_veto_reasons"] = [item.get("reason") for item in vetoes]
    summary["promotable"] = bool(summary.get("promotable")) and final_passed
    summary["empirical_policy_quality"] = final
    return summary


def install(manager):
    """Install Stage-13 semantics after the existing Candidate/Trial composition."""
    if getattr(manager, "_confidence_contract_installed", False):
        return manager

    ensure_tables(manager.store)
    epochs = EvaluationEpochJournal(manager.store)
    probabilities = ProbabilityCalibrationJournal(manager.store)
    original_summary = manager._comparison_summary
    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)

    def comparison_summary(row, parent=None, candidate=None):
        base = original_summary(row, parent, candidate)
        parent_agent = parent or manager.store.get_agent_config(row.get("parent_agent_id"))
        candidate_agent = candidate or manager.store.get_agent(row.get("candidate_id"))
        if not parent_agent or not candidate_agent:
            out = dict(base or {})
            out["confidence_contract"] = contract_descriptor()
            return out
        return _decorate_summary(manager, epochs, row, base, parent_agent, candidate_agent)

    manager._comparison_summary = comparison_summary

    def _decorate(result):
        if not result:
            return result
        out = dict(result)
        row = None
        parent_id = out.get("parent_agent_id")
        if parent_id and callable(getattr(manager, "_candidate_row", None)):
            row = manager._candidate_row(parent_id)
        if row:
            parent = manager.store.get_agent_config(row.get("parent_agent_id"))
            candidate = manager.store.get_agent(row.get("candidate_id"))
            summary = manager._comparison_summary(row, parent, candidate)
            out["comparison"] = summary
            out["confidence_contract"] = summary.get("confidence_contract")
            out["preference_alignment_score"] = summary.get("preference_alignment_score")
            out["empirical_policy_quality"] = summary.get("empirical_policy_quality")
            out["promotion_gates"] = summary.get("promotion_gates") or {}
            out["promotion_vetoes"] = summary.get("promotion_vetoes") or []
            out["promotion_veto_reasons"] = summary.get("promotion_veto_reasons") or []
            out["promotable"] = bool(summary.get("promotable"))
            out["model_confidence_semantics"] = METRIC_SEMANTICS["decision_strength"]
        else:
            out["confidence_contract"] = contract_descriptor()
        return out

    manager.status = lambda parent_id: _decorate(original_status(parent_id))
    manager.list_status = lambda: [
        _decorate(item) for item in (original_list_status() or []) if item
    ]
    if original_lineage_status is not None:
        manager.lineage_status = lambda ref: _decorate(original_lineage_status(ref))

    handler = getattr(manager.core, "Handler", None)
    if handler is not None:
        original_get = handler.do_GET
        original_static = handler.static

        def do_get(http):
            path = urlsplit(http.path).path
            if path == "/confidence_contract_ui.js":
                if not http.require_trusted_client():
                    return
                return http.static(
                    "confidence_contract_ui.js",
                    "application/javascript; charset=utf-8",
                )
            return original_get(http)

        def static(http, name, content_type):
            if name == "index.html":
                path = manager.core.STATIC_DIR / name
                if path.exists():
                    body = path.read_text(encoding="utf-8")
                    marker = '<script src="confidence_contract_ui.js?v=0.14.11"></script>'
                    if marker not in body:
                        body = body.replace("</body>", marker + "\n</body>")
                    return http.send_bytes(200, body.encode("utf-8"), content_type)
            return original_static(http, name, content_type)

        handler.do_GET = do_get
        handler.static = static

    manager.confidence_contract = contract_descriptor()
    manager.record_independent_candidate_label = (
        lambda **kwargs: record_independent_candidate_label(manager.store, **kwargs)
    )
    manager.confidence_probability_journal = probabilities
    manager.confidence_evaluation_epochs = epochs
    manager._confidence_contract_installed = True
    return manager
