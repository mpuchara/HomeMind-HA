"""Fair, future-held-out comparison of fast-device policy backends.

This module is diagnostics only. It never dispatches Home Assistant services and never
changes the production backend. Demonstrations and contextual-bandit rewards are kept
separate: an unchosen action never receives an invented reward.
"""
from __future__ import annotations

import json
import math
import time
import uuid

from policy import DiagonalLinUCB
from policy_full_ridge import FullRidgeLinUCBBackend


BENCHMARK_VERSION = 1
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


def semantic_feature_indices(training_episodes, max_features=24):
    """Choose a bounded explicit vector from training data only, without reward leakage."""
    stats = {}
    for episode in training_episodes:
        for idx, value in _features(episode.get("features")).items():
            row = stats.setdefault(idx, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += value
            row[2] += value * value
    scored = []
    total = max(1, len(training_episodes))
    for idx, (count, total_value, total_sq) in stats.items():
        mean = total_value / max(1, count)
        variance = max(0.0, total_sq / max(1, count) - mean * mean)
        availability = count / total
        score = float("inf") if idx == 0 else availability * (0.05 + math.sqrt(variance))
        scored.append((score, idx))
    scored.sort(key=lambda row: (-row[0], row[1]))
    selected = [idx for _, idx in scored[:max(1, int(max_features))]]
    if 0 in stats and 0 not in selected:
        selected[-1] = 0
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

    def __init__(self, dims, actions, alpha=0.65):
        self.actions = [float(x) for x in actions]
        self.head = DiagonalLinUCB(int(dims), self.actions, float(alpha))

    def predict(self, features, allowed_indices=None):
        return self.head.choose(_features(features), explore=False, allowed_indices=allowed_indices)

    def update(self, action_idx, features, reward, sample_ts=None):
        self.head.update(int(action_idx), _features(features), float(reward), sample_ts)

    def validate(self, action_idx, features, reward, sample_ts=None):
        self.head.validate(int(action_idx), _features(features), float(reward), sample_ts)

    def serialize(self):
        return self.head.export()


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


def run_benchmark(episodes, actions, *, max_features=24, alpha=0.65,
                  ridge_grid=(0.5, 1.0, 2.0), store=None, agent_id=None):
    train, validation, test = split_future(episodes)
    feature_indices = semantic_feature_indices(train, max_features=max_features)
    max_index = max([0] + [idx for row in train for idx in _features(row.get("features"))])
    dims = max(1, max_index + 1)

    def new_baseline():
        return _DiagonalBenchmarkBackend(dims=dims, actions=actions, alpha=alpha)

    baseline = new_baseline()
    baseline_train_ns = 0
    for episode in train:
        started = time.perf_counter_ns(); _learn_episode(baseline, episode); baseline_train_ns += time.perf_counter_ns() - started
    for episode in validation:
        _validate_episode(baseline, episode)
    baseline_validation = _metrics(baseline, validation)

    candidates = []
    for ridge in ridge_grid:
        candidate = _FullRidgeBenchmarkBackend(actions=actions, feature_indices=feature_indices, alpha=alpha, ridge=ridge)
        train_ns = 0
        for episode in train:
            started = time.perf_counter_ns(); _learn_episode(candidate, episode); train_ns += time.perf_counter_ns() - started
        for episode in validation:
            _validate_episode(candidate, episode)
        metrics = _metrics(candidate, validation)
        candidates.append((_validation_score(metrics), float(ridge), candidate, train_ns, metrics))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    _score, selected_ridge, candidate, candidate_train_ns, candidate_validation = candidates[0]

    baseline_test = _metrics(baseline, test)
    candidate_test = _metrics(candidate, test)

    def size_bytes(backend):
        return len(json.dumps(backend.serialize(), separators=(",", ":"), allow_nan=False))

    baseline_acc = baseline_test.get("demonstration_accuracy")
    candidate_acc = candidate_test.get("demonstration_accuracy")
    accuracy_gain = None if baseline_acc is None or candidate_acc is None else candidate_acc - baseline_acc
    ips_gain = None
    if baseline_test.get("bandit_ips_reward") is not None and candidate_test.get("bandit_ips_reward") is not None:
        ips_gain = candidate_test["bandit_ips_reward"] - baseline_test["bandit_ips_reward"]
    candidate_wins = bool(accuracy_gain is not None and accuracy_gain > 0.0 and
                          candidate_test.get("bandit_supported", True) and
                          (ips_gain is None or ips_gain >= 0.0))
    result = {
        "run_id": str(uuid.uuid4()), "benchmark_version": BENCHMARK_VERSION,
        "comparison": "identical_future_episodes_identical_allowed_actions",
        "default_backend": "diagonal_linucb", "automatic_backend_switch": False,
        "candidate_status": "shadow_candidate_supported" if candidate_wins else "keep_diagonal_default",
        "nonlinear_backend_added": False,
        "feature_selection": {"source": "training_only", "indices": feature_indices,
                              "max_features": int(max_features)},
        "hyperparameter_selection": {"source": "validation_only", "ridge": selected_ridge,
                                     "grid": [float(x) for x in ridge_grid], "alpha": float(alpha)},
        "splits": {"train": len(train), "validation": len(validation), "future_test": len(test),
                   "train_end_ts": train[-1].get("timestamp"),
                   "validation_end_ts": validation[-1].get("timestamp"),
                   "test_start_ts": test[0].get("timestamp")},
        "baseline": {"backend": "diagonal_linucb", "backend_version": 5,
                     "validation": baseline_validation, "future_test": baseline_test,
                     "train_us": baseline_train_ns / 1000.0, "serialized_bytes": size_bytes(baseline)},
        "candidate": {"backend": FullRidgeLinUCBBackend.BACKEND,
                      "backend_version": FullRidgeLinUCBBackend.VERSION,
                      "validation": candidate_validation, "future_test": candidate_test,
                      "train_us": candidate_train_ns / 1000.0, "serialized_bytes": size_bytes(candidate)},
        "future_test_gain": {"demonstration_accuracy": accuracy_gain, "bandit_ips_reward": ips_gain},
        "calibration_separate_from_choice": True,
        "bandit_contract": "reward_only_for_executed_action_refuse_off_policy_without_propensity_coverage",
        "automation_replay_is_policy_quality_evidence": False,
        "small_correction_curve": {
            "baseline": _small_correction_curve(new_baseline, train, validation),
            "candidate": _small_correction_curve(
                lambda: _FullRidgeBenchmarkBackend(actions=actions, feature_indices=feature_indices,
                                                    alpha=alpha, ridge=selected_ridge), train, validation),
        },
    }
    if store is not None:
        persist_result(store, result, agent_id=agent_id)
    return result
