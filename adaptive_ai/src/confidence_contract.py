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


CONTRACT_VERSION = 1
PROBABILITY_BINS = 10
DEFAULT_SELECTION_EPISODES = 12
DEFAULT_FINAL_EPISODES = 12
DEFAULT_MIN_PER_ACTION = 4
DEFAULT_DEPENDENCY_WINDOW_SECONDS = 30.0
DEFAULT_HALF_LIFE_EPISODES = 40.0
DEFAULT_OVERCONFIDENCE_GAP = 0.15

METRIC_SEMANTICS = {
    "presence_probability": "calibrated_probability_candidate_0_to_1",
    "forecast_uncertainty": "uncertainty_score_0_to_1_not_probability_of_failure",
    "expected_action_utility": "expected_reward_or_utility_not_probability",
    "data_coverage": "effective_independent_evidence_fraction_not_quality",
    "empirical_policy_quality": "future_episode_observed_quality_with_interval",
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


def _dumps(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _backend_key(model):
    model = dict(model or {})
    backend = str(model.get("backend") or "diagonal_linucb")
    version = str(model.get("version") or "unknown")
    return f"{backend}:v{version}"


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


def _episode_weight(index, total, half_life=DEFAULT_HALF_LIFE_EPISODES):
    half_life = max(1.0, float(half_life))
    age = max(0, int(total) - 1 - int(index))
    return 0.5 ** (float(age) / half_life)


def _dependency_cluster(row, window=DEFAULT_DEPENDENCY_WINDOW_SECONDS):
    explicit = row.get("dependency_cluster") or row.get("cluster_id")
    if explicit is not None:
        return str(explicit)
    ts = _finite(row.get("outcome_ts"), _finite(row.get("ts"), 0.0)) or 0.0
    root = str(row.get("root_agent_id") or row.get("scope_id") or "global")
    return f"{root}:{int(ts // max(1.0, float(window)))}"


def independent_episode_rows(rows, *, id_key="prediction_event_id", end_ts=None):
    """Deduplicate episode evidence and sort chronologically.

    Replaying the same episode never creates another calibration sample. When an explicit
    episode id is missing, the timestamp+scope tuple is used only as a compatibility key.
    """
    unique = {}
    for raw in rows or ():
        row = dict(raw or {})
        ts = _finite(row.get("outcome_ts"), _finite(row.get("ts")))
        if ts is None or (end_ts is not None and ts > float(end_ts)):
            continue
        explicit = row.get(id_key) or row.get("episode_id")
        key = str(explicit) if explicit else f"compat:{row.get('root_agent_id')}:{ts:.6f}"
        previous = unique.get(key)
        if previous is None or ts < _finite(previous.get("outcome_ts"), _finite(previous.get("ts"), ts)):
            unique[key] = row
    return sorted(unique.values(), key=lambda row: _finite(row.get("outcome_ts"), _finite(row.get("ts"), 0.0)) or 0.0)


def dependency_adjusted_weights(rows, *, half_life=DEFAULT_HALF_LIFE_EPISODES,
                                window=DEFAULT_DEPENDENCY_WINDOW_SECONDS):
    rows = list(rows or ())
    raw = [_episode_weight(i, len(rows), half_life) for i in range(len(rows))]
    clusters = defaultdict(list)
    for idx, row in enumerate(rows):
        clusters[_dependency_cluster(row, window)].append(idx)
    adjusted = list(raw)
    for indices in clusters.values():
        cluster_sum = sum(raw[i] for i in indices)
        if cluster_sum > 1.0:
            scale = 1.0 / cluster_sum
            for i in indices:
                adjusted[i] *= scale
    return adjusted


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
    weights = dependency_adjusted_weights(rows, half_life=half_life)
    total_w = sum(weights)
    n_eff = effective_sample_size(weights)
    bucket = [dict(weight=0.0, prediction=0.0, observed=0.0, episodes=0) for _ in range(int(bins))]
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
    gap = None if mean_prediction is None else mean_prediction - observed_rate
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
        "sufficient_evidence": n_eff >= DEFAULT_FINAL_EPISODES,
    }


def action_quality_report(rows, *, scope_id=None, end_ts=None,
                          min_total=DEFAULT_FINAL_EPISODES,
                          min_per_action=DEFAULT_MIN_PER_ACTION,
                          half_life=DEFAULT_HALF_LIFE_EPISODES):
    """Independent future action quality. Confidence-like policy scores are diagnostics only."""
    selected = []
    for raw in rows or ():
        row = dict(raw or {})
        if scope_id is not None:
            candidate_scope = row.get("scope_id") or row.get("root_agent_id")
            if str(candidate_scope) != str(scope_id):
                continue
        selected.append(row)
    rows = independent_episode_rows(selected, end_ts=end_ts)
    weights = dependency_adjusted_weights(rows, half_life=half_life)
    by_action = {"OFF": [], "ON": []}
    all_success = 0.0
    all_weight = 0.0
    strengths_num = 0.0
    strengths_weight = 0.0
    for row, weight in zip(rows, weights):
        outcome = 1 if float(row.get("outcome") or 0.0) >= .5 else 0
        correct = bool(row.get("child_correct"))
        by_action["ON" if outcome else "OFF"].append((correct, weight))
        all_weight += weight
        all_success += weight if correct else 0.0
        strength = _finite(row.get("child_confidence"))
        if strength is not None:
            strengths_num += max(0.0, min(1.0, strength)) * weight
            strengths_weight += weight
    total_eff = effective_sample_size(weights)
    overall = wilson_interval(all_success, all_weight, total_eff)
    per_action = {}
    for name, values in by_action.items():
        action_weights = [weight for _, weight in values]
        total = sum(action_weights)
        success = sum(weight for correct, weight in values if correct)
        eff = effective_sample_size(action_weights)
        interval = wilson_interval(success, total, eff)
        per_action[name] = {
            "episodes": len(values),
            "effective_n": eff,
            "accuracy": interval["mean"],
            "quality_lower_bound": interval["lower"],
            "quality_upper_bound": interval["upper"],
            "error_rate": None if interval["mean"] is None else 1.0 - interval["mean"],
            "sufficient_evidence": eff >= float(min_per_action),
        }
    strength = strengths_num / strengths_weight if strengths_weight else None
    observed = overall["mean"]
    overstated = bool(total_eff >= 8 and strength is not None and observed is not None
                      and strength - observed > DEFAULT_OVERCONFIDENCE_GAP)
    ready = bool(total_eff >= float(min_total)
                 and per_action["OFF"]["sufficient_evidence"]
                 and per_action["ON"]["sufficient_evidence"])
    return {
        "metric": "future_episode_action_quality",
        "probability_claim": False,
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
            """
        )


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
        return probability_calibration(self.rows(metric_id, model_key, scope_id),
                                       scope_id=str(scope_id), model_key=str(model_key))


class EvaluationEpochJournal:
    """Freeze challenger selection data before collecting a fixed future final test."""
    def __init__(self, store):
        self.store = store
        ensure_tables(store)

    def get(self, parent_gid, child_gid, model_revision):
        with self.store.conn() as c:
            row = c.execute(
                """SELECT * FROM confidence_evaluation_epochs
                   WHERE parent_generation_id=? AND child_generation_id=?
                     AND model_revision=? AND contract_version=?""",
                (str(parent_gid), str(child_gid), str(model_revision), CONTRACT_VERSION),
            ).fetchone()
        return dict(row) if row else None

    def ensure(self, parent_gid, child_gid, model_revision, backend_key, pairs,
               *, selection_target=DEFAULT_SELECTION_EPISODES,
               final_target=DEFAULT_FINAL_EPISODES,
               min_per_action=DEFAULT_MIN_PER_ACTION):
        existing = self.get(parent_gid, child_gid, model_revision)
        if existing:
            return existing
        rows = independent_episode_rows(pairs)
        selection = action_quality_report(rows, scope_id=None,
                                          min_total=selection_target,
                                          min_per_action=min_per_action)
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
                (str(parent_gid), str(child_gid), str(model_revision), str(backend_key),
                 CONTRACT_VERSION, cutoff, int(final_target), int(min_per_action), now),
            )
        return self.get(parent_gid, child_gid, model_revision)

    def final_report(self, epoch, pairs, *, scope_id=None):
        if not epoch:
            return {
                "status": "selection_evidence_insufficient",
                "sufficient_evidence": False,
                "recommendation": "abstain_selection_not_frozen",
                "contract_version": CONTRACT_VERSION,
            }
        cutoff = float(epoch["selection_cutoff_ts"])
        rows = [row for row in independent_episode_rows(pairs)
                if float(row.get("outcome_ts") or 0.0) > cutoff]
        end_ts = _finite(epoch.get("final_end_ts"))
        report = action_quality_report(
            rows, scope_id=scope_id, end_ts=end_ts,
            min_total=int(epoch["final_target"]),
            min_per_action=int(epoch["min_per_action"]),
        )
        if end_ts is None and report["sufficient_evidence"]:
            locked_end = None
            for idx in range(1, len(rows) + 1):
                prefix = action_quality_report(
                    rows[:idx], scope_id=scope_id,
                    min_total=int(epoch["final_target"]),
                    min_per_action=int(epoch["min_per_action"]),
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
                epoch = self.get(epoch["parent_generation_id"], epoch["child_generation_id"],
                                 epoch["model_revision"])
                report = action_quality_report(
                    rows, scope_id=scope_id, end_ts=epoch.get("final_end_ts"),
                    min_total=int(epoch["final_target"]),
                    min_per_action=int(epoch["min_per_action"]),
                )
        report.update({
            "status": "complete" if report.get("sufficient_evidence") else "collecting_fixed_future_test",
            "contract_version": CONTRACT_VERSION,
            "selection_cutoff_ts": cutoff,
            "final_target": int(epoch["final_target"]),
            "min_per_action": int(epoch["min_per_action"]),
            "final_end_ts": epoch.get("final_end_ts"),
            "backend_key": epoch.get("backend_key"),
            "model_revision": epoch.get("model_revision"),
            "peek_safe": True,
            "optional_stopping_protection": "fixed_target_and_locked_final_end",
        })
        return report


def contract_descriptor():
    return {
        "version": CONTRACT_VERSION,
        "metric_semantics": dict(METRIC_SEMANTICS),
        "selection_min_independent_episodes": DEFAULT_SELECTION_EPISODES,
        "final_min_independent_episodes": DEFAULT_FINAL_EPISODES,
        "final_min_per_action": DEFAULT_MIN_PER_ACTION,
        "dependency_window_seconds": DEFAULT_DEPENDENCY_WINDOW_SECONDS,
        "probability_metrics": ["brier_score", "reliability_bins"],
        "action_metrics": ["episode_error_rate", "mean_binary_cost", "quality_lower_bound", "quality_upper_bound"],
        "promotion_rule": "selection evidence freezes first; promotion needs a later fixed future test with separate ON/OFF evidence",
        "legacy_confidence": "compatibility_only_decision_strength_not_probability",
        "abstain": "insufficient independent evidence keeps Shadow/fallback",
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
    summary.setdefault("confidence_contract", contract_descriptor())
    if not is_fast_target(parent) or str(parent.get("target_property") or "") != "power":
        summary["confidence_contract"] = {
            **contract_descriptor(),
            "final_evaluation": {
                "status": "not_applicable_non_fast_binary",
                "sufficient_evidence": None,
            },
        }
        summary["decision_strength_semantics"] = METRIC_SEMANTICS["decision_strength"]
        return summary
    generation = _generation_for_candidate(manager.store, row.get("candidate_id"))
    if not generation or not generation.get("parent_generation_id"):
        summary["confidence_contract"] = contract_descriptor()
        return summary
    parent_gid = str(generation["parent_generation_id"])
    child_gid = str(generation["generation_id"])
    pairs = _pair_rows(manager.store, parent_gid, child_gid)
    model = manager.store.get_model(candidate["id"]) if candidate and candidate.get("id") else None
    model_revision = str((model or {}).get("model_revision") or generation.get("model_revision") or "unknown")
    backend_key = _backend_key(model)
    selection_target = max(DEFAULT_SELECTION_EPISODES, int(summary.get("required_future_samples") or 0))
    min_per_action = max(DEFAULT_MIN_PER_ACTION, int(summary.get("required_future_samples_per_action") or 0))
    epoch = epochs.ensure(parent_gid, child_gid, model_revision, backend_key, pairs,
                          selection_target=selection_target,
                          final_target=DEFAULT_FINAL_EPISODES,
                          min_per_action=min_per_action)
    final = epochs.final_report(epoch, pairs, scope_id=str(generation.get("root_agent_id") or ""))

    legacy_preference = summary.get("preference_confidence")
    summary["preference_alignment_score"] = legacy_preference
    summary["preference_alignment_semantics"] = METRIC_SEMANTICS["preference_alignment"]
    summary["decision_strength_semantics"] = METRIC_SEMANTICS["decision_strength"]
    summary["confidence_contract"] = {
        **contract_descriptor(),
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
        old_pref["reason"] = ("preference alignment reached the selection threshold" if old_pref.get("passed")
                              else "preference alignment is below the selection threshold")
        old_pref["metric_semantics"] = METRIC_SEMANTICS["preference_alignment"]
        observed = dict(old_pref.get("observed") or {})
        observed["probability_claim"] = False
        old_pref["observed"] = observed
        gates["preference_evidence"] = old_pref
    final_passed = bool(final.get("sufficient_evidence"))
    gates["independent_final_evaluation"] = {
        "passed": final_passed,
        "reason": ("fixed future evaluation complete with separate ON/OFF evidence"
                   if final_passed else
                   "fixed future evaluation is incomplete; remain Shadow/fallback"),
        "custom_override": "never",
        "metric_semantics": METRIC_SEMANTICS["empirical_policy_quality"],
        "observed": final,
    }
    summary["promotion_gates"] = gates
    vetoes = [dict(item) for item in (summary.get("promotion_vetoes") or [])
              if item.get("gate") != "independent_final_evaluation"]
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
    manager.list_status = lambda: [_decorate(item) for item in (original_list_status() or []) if item]
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
                return http.static("confidence_contract_ui.js", "application/javascript; charset=utf-8")
            return original_get(http)

        def static(http, name, content_type):
            if name == "index.html":
                path = manager.core.STATIC_DIR / name
                if path.exists():
                    body = path.read_text(encoding="utf-8")
                    marker = '<script src="confidence_contract_ui.js?v=0.14.11-f13"></script>'
                    if marker not in body:
                        body = body.replace('</body>', marker + '\n</body>')
                    return http.send_bytes(200, body.encode("utf-8"), content_type)
            return original_static(http, name, content_type)

        handler.do_GET = do_get
        handler.static = static

    manager.confidence_contract = contract_descriptor()
    manager.confidence_probability_journal = probabilities
    manager.confidence_evaluation_epochs = epochs
    manager._confidence_contract_installed = True
    return manager
