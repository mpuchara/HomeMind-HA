"""Fair, future-held-out comparison of fast-device policy backends.

This module is diagnostics only. It never dispatches Home Assistant services and never
changes the production backend. Demonstrations and contextual-bandit rewards are kept
separate: an unchosen action never receives an invented reward.
"""
from __future__ import annotations

import json
import math
import sys
import time
import uuid

from policy import DiagonalLinUCB
from policy_full_ridge import FullRidgeLinUCBBackend


BENCHMARK_VERSION = 2
DEMONSTRATION_SOURCES = {"manual", "explicit_correction", "synthetic_label"}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _features(raw):
    out = {}
    for key, value in dict(raw or {}).items():
        try:
            idx, number = int(key), float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[idx] = number
    return out


def _feature_labels(episode):
    out = {}
    for key, raw in dict(episode.get("feature_labels") or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        out[idx] = [str(x) for x in values if x is not None]
    return out


_V12_ENTITY_SUFFIXES = {
    "value", "valid", "communication_age", "event_age", "quality",
    "trend_1", "trend_2", "trend_3", "time_since_edge",
    "category_bit_0", "category_bit_1", "category_bit_2",
}


def _semantic_group(idx, labels):
    names = labels.get(int(idx)) or []
    if not names:
        return ("legacy_index", int(idx))
    name = str(names[0])
    if name == "bias":
        return ("bias",)
    if name.startswith("time:"):
        return ("time",)
    if name.startswith("home:"):
        return ("home",)
    if "interaction" in name or "*" in name:
        return ("interaction", name)
    if ":" in name:
        prefix, suffix = name.rsplit(":", 1)
        if suffix in _V12_ENTITY_SUFFIXES:
            # Observation v12 missingness is represented by sibling channels. Selecting
            # only :value would collapse a genuine zero with unknown/unavailable again.
            return ("entity_v12", prefix)
    return ("semantic", name)


def semantic_feature_indices(training_episodes, max_features=24):
    """Select bounded semantic groups from training only, preserving v12 missingness."""
    stats, labels = {}, {}
    for episode in training_episodes:
        episode_labels = _feature_labels(episode)
        labels.update(episode_labels)
        values = _features(episode.get("features"))
        # HomeMind vectors are sparse: an omitted slot numerically means zero, not that
        # the schema slot does not exist. Labels therefore define the semantic support.
        for idx in sorted(set(values) | set(episode_labels)):
            value = float(values.get(idx, 0.0))
            row = stats.setdefault(idx, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += value
            row[2] += value * value
    total = max(1, len(training_episodes))
    score_by_idx = {}
    for idx, (count, total_value, total_sq) in stats.items():
        mean = total_value / max(1, count)
        variance = max(0.0, total_sq / max(1, count) - mean * mean)
        availability = count / total
        score_by_idx[idx] = float("inf") if idx == 0 else availability * (0.05 + math.sqrt(variance))

    groups = {}
    for idx in stats:
        groups.setdefault(_semantic_group(idx, labels), []).append(idx)
    ranked = sorted(
        groups.items(),
        key=lambda item: (-max(score_by_idx[i] for i in item[1]), str(item[0])),
    )
    limit = max(1, int(max_features))
    selected = []
    if 0 in stats:
        selected.append(0)
    for group, indices in ranked:
        members = sorted(set(indices) - set(selected))
        if not members:
            continue
        # Named v12 groups are atomic: do not pick :value without :valid/:quality/etc.
        if group[0] == "entity_v12" and len(selected) + len(members) > limit:
            continue
        for idx in members:
            if len(selected) >= limit:
                break
            selected.append(idx)
        if len(selected) >= limit:
            break
    if not selected and stats:
        selected = [min(stats)]
    return sorted(set(selected))

def split_future(episodes, train_fraction=0.60, validation_fraction=0.20):
    ordered = sorted((dict(row) for row in episodes), key=lambda row: (float(row.get("timestamp") or 0.0), str(row.get("id") or "")))
    n = len(ordered)
    if n < 5:
        raise ValueError("benchmark requires at least five chronological episodes")
    train_end = max(1, min(n - 2, int(math.floor(n * train_fraction))))
    validation_end = max(train_end + 1, min(n - 1, int(math.floor(n * (train_fraction + validation_fraction)))))
    return ordered[:train_end], ordered[train_end:validation_end], ordered[validation_end:]


def trial_records_to_episodes(store, owner_agent_id=None):
    """Read Stage-11 TrialRecords as logged contextual-bandit episodes."""
    where = "WHERE status='labelled' AND reward IS NOT NULL"
    params = []
    if owner_agent_id is not None:
        where += " AND owner_agent_id=?"
        params.append(str(owner_agent_id))
    with store.conn() as c:
        rows = c.execute("SELECT * FROM experiment_trial_records " + where + " ORDER BY created_ts,trial_id", params).fetchall()
    episodes = []
    for row in rows:
        row = dict(row)
        context = _json(row.get("context_json"), {})
        assigned = _json(row.get("assigned_action_json"), {})
        action_set = _json(row.get("action_set_json"), [])
        propensities = {}
        allowed = []
        for action in action_set:
            try:
                idx = int(action.get("index"))
            except (TypeError, ValueError):
                continue
            allowed.append(idx)
            p = _finite(action.get("propensity"))
            if p is not None:
                propensities[idx] = p
        executed = assigned.get("index")
        if executed is None:
            continue
        executed = int(executed)
        episodes.append({
            "id": str(row["trial_id"]), "timestamp": float(row.get("created_ts") or 0.0),
            "features": _features(context.get("policy_features")),
            "feature_labels": dict(context.get("policy_feature_labels") or {}),
            "allowed_actions": sorted(set(allowed or [executed])),
            "executed_action": executed, "reward": float(row["reward"]),
            "logged_propensity": _finite(row.get("propensity")),
            "action_propensities": propensities, "kind": "bandit",
            "demonstration_action": None, "demonstration_source": None,
            "horizon": int(round(float(context.get("horizon") or 1))),
        })
    return episodes


class _DiagonalBenchmarkBackend:
    name = "diagonal_linucb"
    version = 5

    def __init__(self, dims, actions, alpha=0.65, feature_indices=None):
        self.actions = [float(x) for x in actions]
        self.feature_indices = None if feature_indices is None else sorted(set(int(x) for x in feature_indices))
        if self.feature_indices is None:
            self.index_map = None
            backend_dims = int(dims)
        else:
            self.index_map = {idx: slot for slot, idx in enumerate(self.feature_indices)}
            backend_dims = max(1, len(self.feature_indices))
        self.head = DiagonalLinUCB(backend_dims, self.actions, float(alpha))

    def _project(self, features):
        raw = _features(features)
        if self.index_map is None:
            return raw
        return {self.index_map[idx]: value for idx, value in raw.items() if idx in self.index_map}

    def predict(self, features, allowed_indices=None):
        return self.head.choose(self._project(features), explore=False, allowed_indices=allowed_indices)

    def update(self, action_idx, features, reward, sample_ts=None):
        self.head.update(int(action_idx), self._project(features), float(reward), sample_ts)

    def validate(self, action_idx, features, reward, sample_ts=None):
        self.head.validate(int(action_idx), self._project(features), float(reward), sample_ts)

    def serialize(self):
        return {
            "backend": self.name, "backend_version": self.version,
            "feature_indices": self.feature_indices, "head": self.head.export(),
        }


class _FullRidgeBenchmarkBackend:
    name = FullRidgeLinUCBBackend.BACKEND
    version = FullRidgeLinUCBBackend.VERSION

    def __init__(self, actions, feature_indices, alpha=0.65, ridge=1.0):
        self.actions = [float(x) for x in actions]
        self.backend = FullRidgeLinUCBBackend(actions=actions, horizons=[1], feature_indices=feature_indices,
                                             alpha=alpha, ridge=ridge)

    def predict(self, features, allowed_indices=None):
        chosen, confidence, arms, _h, _support, _novelty = self.backend.predict(_features(features), allowed_indices=allowed_indices)
        return chosen, confidence, arms

    def update(self, action_idx, features, reward, sample_ts=None):
        self.backend.update(1, int(action_idx), _features(features), float(reward), sample_ts)

    def validate(self, action_idx, features, reward, sample_ts=None):
        self.backend.validate(1, int(action_idx), _features(features), float(reward), sample_ts)

    def serialize(self):
        return self.backend.serialize()


def _learn_episode(backend, episode):
    features = episode.get("features") or {}
    ts = episode.get("timestamp")
    kind = str(episode.get("kind") or "")
    if kind == "demonstration":
        action = episode.get("demonstration_action")
        source = str(episode.get("demonstration_source") or "")
        if action is not None and source in DEMONSTRATION_SOURCES:
            backend.update(int(action), features, 1.0, ts)
        return
    if kind == "bandit":
        action = episode.get("executed_action")
        reward = _finite(episode.get("reward"))
        if action is not None and reward is not None:
            backend.update(int(action), features, reward, ts)


def _validate_episode(backend, episode):
    if str(episode.get("kind") or "") != "bandit":
        return
    action = episode.get("executed_action")
    reward = _finite(episode.get("reward"))
    if action is not None and reward is not None:
        backend.validate(int(action), episode.get("features") or {}, reward, episode.get("timestamp"))


def _metrics(backend, episodes):
    demo_total = demo_correct = 0
    bandit_rows = 0
    ips_sum = 0.0
    unsupported = 0
    calibration_abs = []
    inference_ns = []
    choices = []
    for episode in episodes:
        allowed = [int(x) for x in (episode.get("allowed_actions") or range(len(backend.actions)))]
        started = time.perf_counter_ns()
        chosen, _confidence, arms = backend.predict(episode.get("features") or {}, allowed_indices=allowed)
        inference_ns.append(time.perf_counter_ns() - started)
        choices.append(int(chosen["index"]))
        if str(episode.get("kind") or "") == "demonstration":
            source = str(episode.get("demonstration_source") or "")
            target = episode.get("demonstration_action")
            if source in DEMONSTRATION_SOURCES and target is not None:
                demo_total += 1
                demo_correct += int(int(target) == int(chosen["index"]))
            continue
        if str(episode.get("kind") or "") != "bandit":
            continue
        bandit_rows += 1
        propensities = {int(k): float(v) for k, v in dict(episode.get("action_propensities") or {}).items()}
        propensity = propensities.get(int(chosen["index"]))
        if propensity is None or propensity <= 0.0:
            unsupported += 1
            continue
        executed = int(episode.get("executed_action"))
        reward = float(episode.get("reward"))
        if int(chosen["index"]) == executed:
            ips_sum += reward / propensity
            predicted = float(next(a["mean"] for a in arms if int(a["index"]) == executed))
            calibration_abs.append(abs(max(-1.0, min(1.0, predicted)) - reward))
    supported = unsupported == 0
    return {
        "demonstration_accuracy": (demo_correct / demo_total) if demo_total else None,
        "demonstration_samples": demo_total,
        "bandit_supported": supported,
        "bandit_rows": bandit_rows,
        "bandit_unsupported_rows": unsupported,
        "bandit_ips_reward": (ips_sum / bandit_rows) if supported and bandit_rows else None,
        "reward_calibration_mae": (sum(calibration_abs) / len(calibration_abs)) if calibration_abs else None,
        "reward_calibration_samples": len(calibration_abs),
        "mean_inference_us": (sum(inference_ns) / max(1, len(inference_ns))) / 1000.0,
        "choices": choices,
    }


def _validation_score(metrics):
    values = []
    if metrics.get("demonstration_accuracy") is not None:
        values.append(float(metrics["demonstration_accuracy"]))
    if metrics.get("bandit_ips_reward") is not None:
        values.append((float(metrics["bandit_ips_reward"]) + 1.0) / 2.0)
    return sum(values) / len(values) if values else -1.0


def _small_correction_curve(backend_factory, training, validation, points=(1, 2, 4, 8, 16)):
    demonstrations = [row for row in training if row.get("kind") == "demonstration" and
                      row.get("demonstration_source") in DEMONSTRATION_SOURCES]
    curve = []
    for point in points:
        if not demonstrations:
            break
        backend = backend_factory()
        for episode in demonstrations[:min(point, len(demonstrations))]:
            _learn_episode(backend, episode)
        metrics = _metrics(backend, validation)
        curve.append({"corrections": min(point, len(demonstrations)),
                      "demonstration_accuracy": metrics.get("demonstration_accuracy")})
        if point >= len(demonstrations):
            break
    return curve


def ensure_benchmark_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS policy_backend_benchmarks (
                run_id TEXT PRIMARY KEY,
                benchmark_version INTEGER NOT NULL,
                created_ts REAL NOT NULL,
                agent_id TEXT,
                baseline_backend TEXT NOT NULL,
                candidate_backend TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_policy_backend_benchmarks_agent
                ON policy_backend_benchmarks(agent_id,created_ts);
            """
        )


def persist_result(store, result, agent_id=None):
    ensure_benchmark_tables(store)
    run_id = str(result.get("run_id") or uuid.uuid4())
    result["run_id"] = run_id
    with store.lock, store.conn() as c:
        c.execute(
            """INSERT INTO policy_backend_benchmarks
               (run_id,benchmark_version,created_ts,agent_id,baseline_backend,candidate_backend,result_json)
               VALUES(?,?,?,?,?,?,?)""",
            (run_id, BENCHMARK_VERSION, time.time(), str(agent_id) if agent_id else None,
             "diagonal_linucb", FullRidgeLinUCBBackend.BACKEND,
             json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)),
        )
    return run_id


def _deep_size(value, seen=None):
    """Approximate live Python state RAM without claiming platform-independent RSS."""
    seen = set() if seen is None else seen
    oid = id(value)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_deep_size(v, seen) for v in value)
    return int(size)



def run_benchmark(episodes, actions, *, max_features=24, alpha=0.65,
                  ridge_grid=(0.5, 1.0, 2.0), store=None, agent_id=None):
    train, validation, test = split_future(episodes)
    feature_indices = semantic_feature_indices(train, max_features=max_features)
    max_index = max([0] + [idx for row in train for idx in _features(row.get("features"))])
    dims = max(1, max_index + 1)

    def new_production_baseline():
        return _DiagonalBenchmarkBackend(dims=dims, actions=actions, alpha=alpha)

    def new_matched_baseline():
        return _DiagonalBenchmarkBackend(
            dims=len(feature_indices), actions=actions, alpha=alpha,
            feature_indices=feature_indices,
        )

    def train_backend(factory):
        backend = factory()
        elapsed = 0
        for episode in train:
            started = time.perf_counter_ns()
            _learn_episode(backend, episode)
            elapsed += time.perf_counter_ns() - started
        for episode in validation:
            _validate_episode(backend, episode)
        return backend, elapsed, _metrics(backend, validation)

    production, production_train_ns, production_validation = train_backend(new_production_baseline)
    matched, matched_train_ns, matched_validation = train_backend(new_matched_baseline)

    candidates = []
    for ridge in ridge_grid:
        factory = lambda ridge=ridge: _FullRidgeBenchmarkBackend(
            actions=actions, feature_indices=feature_indices, alpha=alpha, ridge=ridge
        )
        candidate, train_ns, metrics = train_backend(factory)
        candidates.append((_validation_score(metrics), float(ridge), candidate, train_ns, metrics))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    _score, selected_ridge, candidate, candidate_train_ns, candidate_validation = candidates[0]

    production_test = _metrics(production, test)
    matched_test = _metrics(matched, test)
    candidate_test = _metrics(candidate, test)

    def state_cost(backend):
        raw = backend.serialize()
        return {
            "serialized_bytes": len(json.dumps(raw, separators=(",", ":"), allow_nan=False)),
            "python_state_bytes": _deep_size(raw),
            "python_state_bytes_scope": "serialized_numeric_state_approximation_not_process_rss",
        }

    def gain(candidate_metrics, baseline_metrics, key):
        a, b = candidate_metrics.get(key), baseline_metrics.get(key)
        return None if a is None or b is None else float(a) - float(b)

    matched_acc_gain = gain(candidate_test, matched_test, "demonstration_accuracy")
    production_acc_gain = gain(candidate_test, production_test, "demonstration_accuracy")
    matched_ips_gain = gain(candidate_test, matched_test, "bandit_ips_reward")
    production_ips_gain = gain(candidate_test, production_test, "bandit_ips_reward")

    known_matched = [x for x in (matched_acc_gain, matched_ips_gain) if x is not None]
    known_production = [x for x in (production_acc_gain, production_ips_gain) if x is not None]
    positive_backend_gain = any(x > 0.0 for x in known_matched)
    no_matched_regression = bool(known_matched) and all(x >= 0.0 for x in known_matched)
    no_production_regression = (not known_production) or all(x >= 0.0 for x in known_production)
    bandit_coverage_ok = bool(candidate_test.get("bandit_supported", True))
    candidate_wins = bool(
        positive_backend_gain and no_matched_regression and no_production_regression and bandit_coverage_ok
    )

    production_cost = state_cost(production)
    matched_cost = state_cost(matched)
    candidate_cost = state_cost(candidate)
    result = {
        "run_id": str(uuid.uuid4()), "benchmark_version": BENCHMARK_VERSION,
        "comparison": "identical_future_episodes_identical_allowed_actions",
        "backend_effect_comparison": "full_ridge_vs_diagonal_on_identical_semantic_projection",
        "production_reference": "current_full_vector_diagonal_linucb",
        "default_backend": "diagonal_linucb", "automatic_backend_switch": False,
        "candidate_status": "shadow_candidate_supported" if candidate_wins else "keep_diagonal_default",
        "nonlinear_backend_added": False,
        "feature_selection": {
            "source": "training_only", "indices": feature_indices,
            "max_features": int(max_features),
            "semantic_group_atomicity": "observation_v12_entity_groups_when_labels_available",
            "legacy_unlabelled_records": "individual_index_fallback",
        },
        "hyperparameter_selection": {
            "source": "validation_only", "ridge": selected_ridge,
            "grid": [float(x) for x in ridge_grid], "alpha": float(alpha),
        },
        "splits": {
            "train": len(train), "validation": len(validation), "future_test": len(test),
            "train_end_ts": train[-1].get("timestamp"),
            "validation_end_ts": validation[-1].get("timestamp"),
            "test_start_ts": test[0].get("timestamp"),
        },
        "baseline": {
            "backend": "diagonal_linucb", "role": "production_reference_full_vector",
            "backend_version": 5, "validation": production_validation, "future_test": production_test,
            "train_us": production_train_ns / 1000.0, **production_cost,
        },
        "matched_baseline": {
            "backend": "diagonal_linucb", "role": "backend_effect_control_same_semantic_vector",
            "backend_version": 5, "feature_indices": feature_indices,
            "validation": matched_validation, "future_test": matched_test,
            "train_us": matched_train_ns / 1000.0, **matched_cost,
        },
        "candidate": {
            "backend": FullRidgeLinUCBBackend.BACKEND,
            "backend_version": FullRidgeLinUCBBackend.VERSION,
            "validation": candidate_validation, "future_test": candidate_test,
            "train_us": candidate_train_ns / 1000.0, **candidate_cost,
        },
        "future_test_gain": {
            "vs_matched_diagonal": {
                "demonstration_accuracy": matched_acc_gain, "bandit_ips_reward": matched_ips_gain,
            },
            "vs_production_diagonal": {
                "demonstration_accuracy": production_acc_gain, "bandit_ips_reward": production_ips_gain,
            },
        },
        "calibration_separate_from_choice": True,
        "bandit_contract": "reward_only_for_executed_action_refuse_off_policy_without_propensity_coverage",
        "automation_replay_is_policy_quality_evidence": False,
        "small_correction_curve": {
            "production_baseline": _small_correction_curve(new_production_baseline, train, validation),
            "matched_baseline": _small_correction_curve(new_matched_baseline, train, validation),
            "candidate": _small_correction_curve(
                lambda: _FullRidgeBenchmarkBackend(
                    actions=actions, feature_indices=feature_indices,
                    alpha=alpha, ridge=selected_ridge,
                ), train, validation,
            ),
        },
        "candidate_support_rule": {
            "positive_gain_vs_matched_representation": positive_backend_gain,
            "no_regression_vs_matched_representation": no_matched_regression,
            "no_regression_vs_production_reference": no_production_regression,
            "bandit_propensity_coverage": bandit_coverage_ok,
        },
    }
    if store is not None:
        persist_result(store, result, agent_id=agent_id)
    return result

