"""Conservative Candidate Correct/Teach pipeline.

Ordinary explicit feedback must evolve the current Live generation, not erase it and
relearn a replacement policy from raw history.  This extension is installed last so it
can preserve the existing Candidate lifecycle/config/safety wrappers while changing the
build semantics for Correct/Teach only:

    Live Gen N -> exact hidden snapshot -> supervised correction -> offline regression gate
               -> future paired A/B (only after the offline gate passes)

Explicit Full Rebuild / config-change workflows keep the existing historical rebuild
path. Candidate policies remain hidden from normal runtime enumeration and this module
never creates ActionIntent, calls Executor, or invokes Home Assistant services.
"""
import json
import math
import time
import uuid

from settings import OPTIONS, iso_now
from telemetry import HEAVY_JOBS
from training_budget import TRAINING_BUDGET


_CORRECT_REASONS = {
    "feedback", "wrong_decision", "wrong_decision_undo", "teach", "teach_undo", "teach_train",
}
_FULL_REBUILD_REASONS = {"manual_rebuild", "config_change"}
_CANDIDATE_WORK_KEY = "candidate_correct_work"
_BUDGET_THREAD_NAME = "adaptive-ai-index-candidate-correct"


def _set_candidate_work(manager, parent_id, **fields):
    parent_id = str(parent_id)
    with manager.lock:
        runtime = manager.runtime.setdefault(parent_id, {})
        work = dict(runtime.get(_CANDIDATE_WORK_KEY) or {})
        work.update(fields)
        work["updated_ts"] = time.time()
        runtime[_CANDIDATE_WORK_KEY] = work
        return dict(work)


def _candidate_work(manager, parent_id):
    with manager.lock:
        runtime = manager.runtime.get(str(parent_id)) or {}
        return dict(runtime.get(_CANDIDATE_WORK_KEY) or {})


def _candidate_worker_queue(manager, row):
    state = str(row.get("state") or "queued")
    if state not in ("queued", "building") or str(row.get("reason") or "") not in _CORRECT_REASONS:
        return None
    rows = [
        item for item in manager._all_rows()
        if str(item.get("state") or "") in ("queued", "building")
        and str(item.get("reason") or "") in _CORRECT_REASONS
    ]
    candidate_id = str(row.get("candidate_id") or "")
    if state == "building":
        return {
            "state": "active", "position": 0, "ahead": 0,
            "reason": "candidate_correct", "backend": "candidate_worker",
            "blocked_by": None, "agent_id": candidate_id,
        }
    position = 1
    for index, item in enumerate(rows):
        if str(item.get("candidate_id") or "") == candidate_id:
            position = index + 1
            break
    return {
        "state": "queued", "position": position, "ahead": max(0, position - 1),
        "reason": "candidate_correct", "backend": "candidate_worker",
        "blocked_by": HEAVY_JOBS.owner, "agent_id": candidate_id,
    }


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _ensure_gate_column(store):
    with store.lock, store.conn() as c:
        cols = {str(r[1]) for r in c.execute("PRAGMA table_info(agent_candidates)").fetchall()}
        if "offline_gate_json" not in cols:
            c.execute("ALTER TABLE agent_candidates ADD COLUMN offline_gate_json TEXT NOT NULL DEFAULT '{}'")


def _copy_parent_snapshot(manager, parent_id, candidate_id):
    """Replace a hidden surrogate with an exact persisted snapshot of its Live parent.

    ``rl_models.model_json`` is copied byte-for-byte so policy weights, schema,
    selection metadata, model revision and benchmark internals start identically.
    Historical experiences and feedback/audit rows are copied as well. The only
    intentional runtime difference is ``mode='paused'``: a Candidate is never allowed to
    own physical control.
    """
    store = manager.store
    with store.lock, store.conn() as c:
        parent = c.execute("SELECT * FROM agents WHERE id=?", (str(parent_id),)).fetchone()
        candidate = c.execute("SELECT * FROM agents WHERE id=?", (str(candidate_id),)).fetchone()
        model = c.execute(
            "SELECT model_json,updated_at FROM rl_models WHERE agent_id=?", (str(parent_id),)
        ).fetchone()
        if not parent or not candidate:
            raise RuntimeError("Live or Candidate agent disappeared before snapshot")
        if not model:
            raise RuntimeError("Live policy snapshot is unavailable; train Live before Correct")

        # Future schema/config columns automatically participate in the snapshot unless
        # they are identity/runtime-safety fields.
        columns = [str(r[1]) for r in c.execute("PRAGMA table_info(agents)").fetchall()]
        excluded = {"id", "name", "mode", "created_at"}
        copied = [name for name in columns if name not in excluded]
        if copied:
            assignments = ",".join(f"{name}=?" for name in copied)
            values = [parent[name] for name in copied]
            c.execute(
                f"UPDATE agents SET {assignments},mode='paused' WHERE id=?",
                values + [str(candidate_id)],
            )

        c.execute("DELETE FROM rl_models WHERE agent_id=?", (str(candidate_id),))
        c.execute(
            "INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)",
            (str(candidate_id), str(model["model_json"]), str(model["updated_at"] or iso_now())),
        )

        c.execute("DELETE FROM historical_experiences WHERE agent_id=?", (str(candidate_id),))
        c.execute(
            """INSERT INTO historical_experiences
               (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
               SELECT ?,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id
               FROM historical_experiences WHERE agent_id=? ORDER BY id""",
            (str(candidate_id), str(parent_id)),
        )

        c.execute("DELETE FROM rl_feedback WHERE agent_id=?", (str(candidate_id),))
        c.execute(
            """INSERT INTO rl_feedback
               (agent_id,created_at,action_index,action_value,reward,reason,features_json,user_id,source)
               SELECT ?,created_at,action_index,action_value,reward,reason,features_json,user_id,source
               FROM rl_feedback WHERE agent_id=? ORDER BY id""",
            (str(candidate_id), str(parent_id)),
        )

        # Leftovers from the pre-fix Candidate rebuild pipeline must not re-enter the
        # destructive TrainingQueue path after an upgrade/retry.
        if c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='teaching_rl_jobs'"
        ).fetchone():
            c.execute("DELETE FROM teaching_rl_jobs WHERE agent_id=?", (str(candidate_id),))

    store.touch_agent_index()
    manager.engine.models.pop(str(candidate_id), None)
    manager.engine.runtime.pop(str(candidate_id), None)
    return manager.store.get_agent_config(str(candidate_id))


def _features(raw):
    data = _json(raw, {})
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        try:
            idx = int(key)
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[idx] = number
    return out


def _nearest_action(policy, value):
    actions = [float(x) for x in policy.actions]
    if not actions:
        raise RuntimeError("Candidate policy has no actions")
    return min(range(len(actions)), key=lambda i: abs(actions[i] - float(value)))


def _prediction_index(policy, features):
    prediction = float(policy.predict(features)[0]["value"])
    return _nearest_action(policy, prediction), prediction


def _teach_rows(service, candidate):
    from teaching_rl import fingerprint

    return [
        row for row in service.labels(candidate["id"])
        if str(row.get("fingerprint")) == str(fingerprint(candidate))
    ]


def _conservative_fine_tune(manager, candidate, *, parent_id=None):
    """Fine-tune the snapshot without feature selection or historical rebuilding.

    All pre-correction predictions are collected before the first policy update. A
    negative correction therefore targets exactly the action this Candidate snapshot
    actually chose at that Teach context. ``previous_desired`` is retained in storage
    for diagnostics but is never used as a negative-learning target.
    """
    service = getattr(manager.engine, "rl_teaching", None)
    if service is None:
        raise RuntimeError("Teach RL service unavailable")

    manager.engine.models.pop(candidate["id"], None)
    policy = manager.engine.policy(candidate)
    labels = _teach_rows(service, candidate)
    parent_id = str(parent_id or candidate.get("id") or "")
    total_labels = max(1, len(labels))
    positive_weight = max(1, int(OPTIONS.get("teach_rl_positive_weight", 6)))
    negative_weight = max(0, int(OPTIONS.get("teach_rl_negative_weight", 3)))
    deadband = max(.01, float(candidate.get("deadband") or .01))

    # Critically, collect every prediction from the untouched snapshot first. Later
    # corrections cannot change which action is considered the rejected action for an
    # earlier Teach sample in the same revision.
    usable = []
    before_correct = 0
    for index, label in enumerate(labels):
        if parent_id:
            _set_candidate_work(
                manager, parent_id, state="active", phase="reconstructing_correct_context",
                progress=0.42 + 0.16 * (index / total_labels),
                labels_done=index, labels_total=len(labels),
            )
        features = service._label_context(candidate, policy, label["sample_ts"])
        if features is None:
            continue
        chosen_idx, chosen_value = _prediction_index(policy, features)
        desired_idx = _nearest_action(policy, float(label["desired"]))
        desired_value = float(policy.actions[desired_idx])
        correct = abs(chosen_value - desired_value) <= deadband
        before_correct += int(correct)
        usable.append({
            "label": label,
            "features": features,
            "chosen_idx": chosen_idx,
            "chosen_value": chosen_value,
            "desired_idx": desired_idx,
            "desired_value": desired_value,
            "was_correct": bool(correct),
        })
        TRAINING_BUDGET.checkpoint(
            "candidate_correct_context", thread_name=_BUDGET_THREAD_NAME
        )

    base_revision = str(getattr(policy, "model_revision", "") or "")
    negative_updates = 0
    total_usable = max(1, len(usable))
    for index, sample in enumerate(usable):
        if parent_id:
            _set_candidate_work(
                manager, parent_id, state="active", phase="applying_correct_updates",
                progress=0.60 + 0.15 * (index / total_usable),
                labels_done=index, labels_total=len(usable),
            )
        for horizon in policy.horizons:
            if sample["chosen_idx"] != sample["desired_idx"]:
                for _ in range(negative_weight):
                    policy.update(horizon, sample["chosen_idx"], sample["features"], -1.0)
                    negative_updates += 1
            for _ in range(positive_weight):
                policy.update(horizon, sample["desired_idx"], sample["features"], 1.0)

        if sample["chosen_idx"] != sample["desired_idx"]:
            manager.store.add_feedback(
                candidate["id"], sample["chosen_idx"], policy.actions[sample["chosen_idx"]], -1.0,
                "Candidate Teach rejected actual pre-correction prediction",
                sample["features"], "teach-ui", source="candidate_correct",
            )
        manager.store.add_feedback(
            candidate["id"], sample["desired_idx"], policy.actions[sample["desired_idx"]], 1.0,
            "Candidate Teach desired correction", sample["features"], "teach-ui",
            source="candidate_correct",
        )
        TRAINING_BUDGET.checkpoint(
            "candidate_correct_update", thread_name=_BUDGET_THREAD_NAME
        )

    # A changed set of weights is a new Candidate model revision, while the report keeps
    # the exact parent revision from which it originated.
    if usable:
        policy.model_revision = str(uuid.uuid4())

    after_correct = 0
    for sample in usable:
        _, chosen_value = _prediction_index(policy, sample["features"])
        after_correct += int(abs(chosen_value - sample["desired_value"]) <= deadband)

    if parent_id:
        _set_candidate_work(
            manager, parent_id, state="active", phase="saving_candidate_model",
            progress=0.78, labels_done=len(usable), labels_total=len(usable),
        )
    TRAINING_BUDGET.checkpoint(
        "candidate_correct_save", force=True, thread_name=_BUDGET_THREAD_NAME
    )
    manager.store.save_model(candidate["id"], policy.serialize())
    manager.engine.models[candidate["id"]] = policy
    return {
        "mode": "conservative_snapshot_finetune",
        "labels_applied": len(usable),
        "teach_fit_before_count": int(before_correct),
        "teach_fit_after_count": int(after_correct),
        "teach_fit_total": len(usable),
        "teach_fit_before": (before_correct / len(usable)) if usable else None,
        "teach_fit_after": (after_correct / len(usable)) if usable else None,
        "negative_updates": int(negative_updates),
        "negative_actions": sum(1 for x in usable if x["chosen_idx"] != x["desired_idx"]),
        "previous_desired_used_for_negative": False,
        "schema_changed": False,
        "base_model_revision": base_revision or None,
        "candidate_model_revision": str(getattr(policy, "model_revision", "") or "") or None,
    }


def _history_rows(store, agent_id, teach_times=()):
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT h.id,h.target_history_id,h.action_index,h.action_value,h.features_json,e.ts
               FROM historical_experiences h
               LEFT JOIN entity_history e ON e.id=h.target_history_id
               WHERE h.agent_id=? ORDER BY h.id""",
            (str(agent_id),),
        ).fetchall()]

    # The Teach context is not allowed to certify itself. Historical experiences are
    # target-transition rows rather than arbitrary chart points, so exclude an experience
    # only when its transition timestamp is the Teach sample itself (small storage/float
    # tolerance); unrelated neighbouring history remains available for regression proof.
    times = [float(x) for x in teach_times]
    if not times:
        return rows
    filtered = []
    for index, row in enumerate(rows):
        if index and index % 64 == 0:
            TRAINING_BUDGET.checkpoint(
                "candidate_correct_history_filter", thread_name=_BUDGET_THREAD_NAME
            )
        ts = row.get("ts")
        if ts is not None and any(abs(float(ts) - sample_ts) <= .5 for sample_ts in times):
            continue
        filtered.append(row)
    return filtered


def _score(policy, agent, rows):
    actions = [float(x) for x in policy.actions]
    binary = len(actions) == 2 or str(agent.get("target_property") or "") == "power"
    per_action = {}
    predicted_actions = set()
    samples = correct = 0
    tolerance = max(
        float(agent.get("deadband") or 0.0),
        (float(agent.get("max_value") or 0.0) - float(agent.get("min_value") or 0.0)) * .03,
    )
    for index, row in enumerate(rows):
        if index and index % 64 == 0:
            TRAINING_BUDGET.checkpoint(
                "candidate_correct_offline_score", thread_name=_BUDGET_THREAD_NAME
            )
        features = _features(row.get("features_json"))
        if not features:
            continue
        try:
            actual_idx = int(row["action_index"])
            if actual_idx < 0 or actual_idx >= len(actions):
                continue
            predicted_idx, predicted_value = _prediction_index(policy, features)
        except (TypeError, ValueError, RuntimeError, KeyError):
            continue
        actual_value = actions[actual_idx]
        ok = predicted_idx == actual_idx if binary else abs(predicted_value - actual_value) <= tolerance
        samples += 1
        correct += int(ok)
        predicted_actions.add(predicted_idx)
        slot = per_action.setdefault(str(actual_idx), {"samples": 0, "correct": 0})
        slot["samples"] += 1
        slot["correct"] += int(ok)

    per_accuracy = {
        key: float(value["correct"]) / max(1, int(value["samples"]))
        for key, value in per_action.items() if int(value.get("samples") or 0) > 0
    }
    if binary:
        score = (sum(per_accuracy.values()) / len(per_accuracy)) if len(per_accuracy) == 2 else None
    else:
        score = (float(correct) / samples) if samples else None
    return {
        "samples": int(samples),
        "correct": int(correct),
        "score": score,
        "balanced": bool(binary),
        "per_action": per_action,
        "per_action_accuracy": per_accuracy,
        "actual_class_coverage": len(per_action),
        "predicted_class_coverage": len(predicted_actions),
    }


def _benchmark_stats(agent):
    """Adapt the existing HistoryManager held-out benchmark for explicit Full Rebuilds.

    Correct uses the stricter same-row comparison above because its schema is frozen. A
    schema-changing Full Rebuild cannot safely replay the old model's serialized feature
    vector through the new schema, so reuse each policy's already-qualified historical
    benchmark instead of pretending those feature indexes are interchangeable.
    """
    detail = dict(agent.get("benchmark_detail") or {})
    counts = dict(detail.get("counts") or {})
    raw_per = dict(counts.get("per_action") or {})
    per_action = {
        str(key): {
            "samples": int(value.get("samples") or 0),
            "correct": int(value.get("correct") or 0),
        }
        for key, value in raw_per.items()
        if isinstance(value, dict)
    }
    per_accuracy = dict(detail.get("per_action_accuracy") or {})
    if not per_accuracy:
        per_accuracy = {
            key: float(value["correct"]) / max(1, int(value["samples"]))
            for key, value in per_action.items() if int(value.get("samples") or 0) > 0
        }
    binary = bool(detail.get("balanced")) or str(agent.get("target_property") or "") == "power"
    class_coverage = bool(detail.get("class_coverage", len(per_action) >= (2 if binary else 1)))
    qualified = str(agent.get("training_state") or "") == "qualified"
    predicted_coverage = 2 if binary and qualified and class_coverage else (1 if int(agent.get("benchmark_samples") or 0) else 0)
    return {
        "samples": int(agent.get("benchmark_samples") or counts.get("samples") or 0),
        "correct": int(counts.get("correct") or 0),
        "score": agent.get("benchmark_score"),
        "balanced": bool(binary),
        "per_action": per_action,
        "per_action_accuracy": {str(k): float(v) for k, v in per_accuracy.items()},
        "actual_class_coverage": len([1 for v in per_action.values() if int(v.get("samples") or 0) > 0]),
        "predicted_class_coverage": predicted_coverage,
    }


def _offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    max_regression = max(0.0, float(OPTIONS.get("agent_candidate_max_accuracy_regression", .03)))
    min_samples = max(1, int(OPTIONS.get("candidate_benchmark_min_samples", 12)))
    teach_report = dict(teach_report or {})
    before = teach_report.get("teach_fit_before")
    after = teach_report.get("teach_fit_after")
    teach_total = teach_report.get("teach_fit_total")
    conservative_correct = teach_report.get("mode") == "conservative_snapshot_finetune"
    teach_evidence = not conservative_correct or int(teach_total or 0) > 0
    teach_ok = teach_evidence and (
        True if before is None or after is None else float(after) + 1e-12 >= float(before)
    )

    n = min(int(parent_stats.get("samples") or 0), int(candidate_stats.get("samples") or 0))
    binary = bool(parent_stats.get("balanced") or candidate_stats.get("balanced"))
    class_evidence = (not binary) or (
        int(parent_stats.get("actual_class_coverage") or 0) >= 2
        and int(candidate_stats.get("actual_class_coverage") or 0) >= 2
    )
    parent_score = parent_stats.get("score")
    candidate_score = candidate_stats.get("score")
    sufficient = bool(
        teach_evidence and n >= min_samples and class_evidence
        and parent_score is not None and candidate_score is not None
    )

    if not sufficient:
        status = "insufficient_evidence"
        passed = False
        regression = None
        collapse = False
        reasons = []
        if not teach_evidence:
            reasons.append("no usable Teach context could be reconstructed")
        if n < min_samples or not class_evidence or parent_score is None or candidate_score is None:
            reasons.append(f"need at least {min_samples} non-Teach historical samples with required class coverage")
    else:
        regression = float(candidate_score) - float(parent_score)
        collapse = False
        if binary:
            collapse = int(candidate_stats.get("predicted_class_coverage") or 0) < 2
            parent_per = dict(parent_stats.get("per_action_accuracy") or {})
            candidate_per = dict(candidate_stats.get("per_action_accuracy") or {})
            for key, parent_acc in parent_per.items():
                if float(parent_acc) > 0.0 and float(candidate_per.get(key, 0.0)) <= 0.0:
                    collapse = True
        regression_ok = regression + max_regression >= -1e-12
        passed = bool(teach_ok and regression_ok and not collapse)
        status = "passed" if passed else "failed"
        reasons = []
        if not teach_ok:
            reasons.append("Teach fit regressed")
        if not regression_ok:
            reasons.append(f"historical regression exceeded {max_regression * 100:.1f} pp")
        if collapse:
            reasons.append("binary one-class collapse detected")

    return {
        "status": status,
        "passed": bool(passed),
        "teach_fit_before": before,
        "teach_fit_after": after,
        "teach_fit_before_count": teach_report.get("teach_fit_before_count"),
        "teach_fit_after_count": teach_report.get("teach_fit_after_count"),
        "teach_fit_total": teach_total,
        "historical_parent_score": parent_score,
        "historical_candidate_score": candidate_score,
        "regression_delta": regression,
        "benchmark_samples": int(n),
        "minimum_samples": int(min_samples),
        "max_regression": float(max_regression),
        "balanced": bool(binary),
        "binary_collapse": bool(collapse),
        "parent_per_action_accuracy": parent_stats.get("per_action_accuracy") or {},
        "candidate_per_action_accuracy": candidate_stats.get("per_action_accuracy") or {},
        "candidate_predicted_class_coverage": int(candidate_stats.get("predicted_class_coverage") or 0),
        "reasons": reasons,
        "base_model_revision": teach_report.get("base_model_revision"),
        "candidate_model_revision": teach_report.get("candidate_model_revision"),
        "schema_changed": bool(teach_report.get("schema_changed", False)),
        "updated_ts": time.time(),
    }


def _persist_gate(manager, row, gate, *, passed_state="comparing"):
    state = passed_state if gate.get("passed") else (
        "insufficient_evidence" if gate.get("status") == "insufficient_evidence" else "offline_blocked"
    )
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE agent_candidates SET state=?,offline_gate_json=?,build_finished_ts=?,
               comparison_started_ts=?,updated_ts=? WHERE parent_agent_id=?""",
            (
                state, json.dumps(gate, separators=(",", ":")), now,
                now if gate.get("passed") else None, now, str(row["parent_agent_id"]),
            ),
        )
    manager.runtime.pop(str(row["parent_agent_id"]), None)
    level = "info" if gate.get("passed") else "warning"
    manager.store.event(
        row["parent_agent_id"], level, "agent_candidate_offline_regression_gate",
        "Candidate passed offline regression gate; future paired comparison may start" if gate.get("passed") else
        "Candidate did not pass offline regression gate; future paired comparison is blocked",
        {"candidate_id": row["candidate_id"], "offline_gate": gate},
    )
    return state


def _policy_for(manager, agent):
    manager.engine.models.pop(str(agent["id"]), None)
    return manager.engine.policy(agent)


def install(manager):
    if getattr(manager, "_candidate_conservative_correct", False):
        return manager

    _ensure_gate_column(manager.store)
    original_create = manager._create_candidate
    original_start = manager._start_build
    original_finish = manager._finish_build_if_ready
    original_status = manager.status
    original_summary = manager._comparison_summary
    original_before = manager.before_live_process
    original_after = manager.after_live_process

    def create_candidate(parent):
        row = original_create(parent)
        if row and manager.store.get_model(parent["id"]) is not None:
            _copy_parent_snapshot(manager, parent["id"], row["candidate_id"])
        return manager._candidate_row(parent["id"]) or row

    def start_build(row):
        reason = str(row.get("reason") or "feedback")
        if reason in _FULL_REBUILD_REASONS:
            return original_start(row)
        if reason not in _CORRECT_REASONS:
            # Unknown workflow is intentionally conservative: preserve the established
            # rebuild semantics rather than silently changing its contract.
            return original_start(row)

        parent = manager.store.get_agent_config(row["parent_agent_id"])
        candidate = manager.store.get_agent_config(row["candidate_id"])
        if not parent or not candidate:
            manager._fail(row, "candidate or live agent disappeared")
            return True
        if manager.store.get_model(parent["id"]) is None:
            manager._fail(row, "Live policy snapshot is unavailable; train Live before Correct")
            return True

        owner = "candidate_correct"
        if not HEAVY_JOBS.acquire(owner):
            _set_candidate_work(
                manager, parent["id"], state="queued", phase="waiting_for_heavy_slot",
                progress=0.0, blocked_by=HEAVY_JOBS.owner,
            )
            return False

        TRAINING_BUDGET.begin(thread_name=_BUDGET_THREAD_NAME)
        build_revision = int(row.get("feedback_revision") or 0)
        now = time.time()
        try:
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET state='building',build_revision=?,dirty=0,
                       build_started_ts=?,build_finished_ts=NULL,comparison_started_ts=NULL,
                       comparison_json='{}',offline_gate_json='{}',last_error=NULL,updated_ts=?
                       WHERE parent_agent_id=?""",
                    (build_revision, now, now, str(row["parent_agent_id"])),
                )
            _set_candidate_work(
                manager, parent["id"], state="active", phase="snapshot",
                progress=0.05, started_ts=now, blocked_by=None,
            )
            manager.store.event(
                parent["id"], "info", "agent_candidate_correct_started",
                "Candidate Correct worker started conservative snapshot fine-tune",
                {"candidate_id": candidate["id"], "generation": row.get("generation"),
                 "build_revision": build_revision, "backend": "candidate_worker"},
            )

            candidate = _copy_parent_snapshot(manager, parent["id"], candidate["id"])
            _set_candidate_work(
                manager, parent["id"], state="active", phase="syncing_feedback", progress=0.12
            )
            synced = manager._sync_feedback(parent, candidate)
            candidate = manager.store.get_agent_config(candidate["id"]) or candidate
            TRAINING_BUDGET.checkpoint(
                "candidate_correct_snapshot", force=True, thread_name=_BUDGET_THREAD_NAME
            )

            # Parent and Candidate are scored on the exact same immutable held-out row
            # list. Exclude every active Teach timestamp up front, even if one Teach
            # context later proves unusable; this cannot leak a training point into the
            # regression benchmark and avoids changing the denominator after fine-tune.
            labels = _teach_rows(manager.engine.rl_teaching, candidate)
            teach_times = [float(x["sample_ts"]) for x in labels]
            _set_candidate_work(
                manager, parent["id"], state="active", phase="loading_offline_benchmark",
                progress=0.20, labels_total=len(labels),
            )
            rows = _history_rows(manager.store, candidate["id"], teach_times)
            _set_candidate_work(
                manager, parent["id"], state="active", phase="scoring_parent_snapshot",
                progress=0.28, benchmark_rows=len(rows),
            )
            parent_policy = _policy_for(manager, candidate)
            parent_stats = _score(parent_policy, candidate, rows)
            _set_candidate_work(
                manager, parent["id"], state="active", phase="fine_tuning",
                progress=0.40, benchmark_rows=len(rows), labels_total=len(labels),
            )

            report = _conservative_fine_tune(
                manager, candidate, parent_id=parent["id"]
            )
            candidate = manager.store.get_agent_config(candidate["id"]) or candidate
            _set_candidate_work(
                manager, parent["id"], state="active", phase="scoring_candidate",
                progress=0.82, benchmark_rows=len(rows),
            )
            candidate_policy = _policy_for(manager, candidate)
            candidate_stats = _score(candidate_policy, candidate, rows)
            _set_candidate_work(
                manager, parent["id"], state="active", phase="offline_gate", progress=0.94
            )
            gate = _offline_gate(parent, parent_stats, candidate_stats, report)

            fresh = manager._candidate_row(parent["id"]) or row
            if int(fresh.get("feedback_revision") or 0) > build_revision or int(fresh.get("dirty") or 0):
                with manager.store.lock, manager.store.conn() as c:
                    c.execute(
                        "UPDATE agent_candidates SET state='queued',dirty=1,updated_ts=? WHERE parent_agent_id=?",
                        (time.time(), str(parent["id"])),
                    )
                _set_candidate_work(
                    manager, parent["id"], state="queued", phase="new_feedback",
                    progress=0.0, blocked_by=None,
                )
                manager.store.event(
                    parent["id"], "info", "agent_candidate_requeued",
                    "New feedback arrived during Candidate fine-tune; restarting from a fresh Live snapshot",
                    {"candidate_id": candidate["id"], "build_revision": build_revision,
                     "feedback_revision": int(fresh.get("feedback_revision") or 0)},
                )
                manager.wake_event.set()
                return True

            _persist_gate(manager, fresh, gate)
            _set_candidate_work(
                manager, parent["id"], state="done", phase="complete", progress=1.0,
                finished_ts=time.time(), labels_done=report.get("labels_applied"),
                labels_total=report.get("labels_applied"),
            )
            manager.store.event(
                parent["id"], "info", "agent_candidate_correct_complete",
                "Candidate Correct fine-tuned the exact Live snapshot without rebuild or schema reselection",
                {"candidate_id": candidate["id"], "generation": fresh.get("generation"),
                 "feedback": synced, "teach": report, "offline_gate": gate,
                 "backend": "candidate_worker"},
            )
            return True
        except Exception as exc:
            _set_candidate_work(
                manager, parent["id"], state="failed", phase="failed", progress=0.0,
                error=f"{type(exc).__name__}: {exc}", finished_ts=time.time(),
            )
            manager._fail(row, f"{type(exc).__name__}: {exc}")
            return True
        finally:
            TRAINING_BUDGET.end()
            HEAVY_JOBS.release(owner)

    def finish_build_if_ready(row):
        # Conservative Correct never enters TrainingQueue. Explicit Full Rebuild remains
        # asynchronous; once its own historical qualification finishes, compare those
        # established held-out benchmark scores before permitting future A/B.
        reason = str(row.get("reason") or "")
        result = original_finish(row)
        if not result or reason not in _FULL_REBUILD_REASONS:
            return result
        fresh = manager._candidate_row(row["parent_agent_id"]) or row
        if str(fresh.get("state") or "") not in ("comparing", "ready"):
            return result
        existing_gate = _json(fresh.get("offline_gate_json"), {})
        if existing_gate.get("status"):
            return result
        parent = manager.store.get_agent_config(fresh["parent_agent_id"])
        candidate = manager.store.get_agent_config(fresh["candidate_id"])
        if not parent or not candidate or not manager.store.get_model(candidate["id"]):
            return result
        try:
            gate = _offline_gate(parent, _benchmark_stats(parent), _benchmark_stats(candidate), {})
            _persist_gate(manager, fresh, gate)
        except Exception as exc:
            manager._fail(fresh, f"offline regression gate failed: {type(exc).__name__}: {exc}")
        return True

    def comparison_summary(row, parent=None, candidate=None):
        out = original_summary(row, parent, candidate)
        gate = _json(row.get("offline_gate_json"), {})
        out["offline_gate_passed"] = bool(gate.get("passed"))
        out["promotable"] = bool(out.get("promotable") and gate.get("passed"))
        return out

    def _future_gate_passed(agent):
        row = manager._candidate_row(agent["id"])
        if not row or str(row.get("state") or "") not in ("comparing", "ready"):
            return True
        return bool(_json(row.get("offline_gate_json"), {}).get("passed"))

    def before_live_process(agent, state_map):
        # Even the tiny interval between a legacy rebuild completing and its regression
        # gate being persisted cannot collect future A/B evidence.
        if not _future_gate_passed(agent):
            return None
        return original_before(agent, state_map)

    def after_live_process(agent, state_map):
        if not _future_gate_passed(agent):
            return None
        return original_after(agent, state_map)

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        row = manager._candidate_row(parent_id)
        gate = _json((row or {}).get("offline_gate_json"), {})
        result["offline_gate"] = gate
        result["teach_fit_before"] = gate.get("teach_fit_before")
        result["teach_fit_after"] = gate.get("teach_fit_after")
        result["teach_fit_before_count"] = gate.get("teach_fit_before_count")
        result["teach_fit_after_count"] = gate.get("teach_fit_after_count")
        result["teach_fit_total"] = gate.get("teach_fit_total")
        result["historical_regression_delta"] = gate.get("regression_delta")
        result["historical_parent_score"] = gate.get("historical_parent_score")
        result["historical_candidate_score"] = gate.get("historical_candidate_score")
        result["historical_benchmark_samples"] = gate.get("benchmark_samples")

        if row and str(row.get("reason") or "") in _CORRECT_REASONS:
            work = _candidate_work(manager, row["parent_agent_id"])
            synthetic_queue = _candidate_worker_queue(manager, row)
            if result.get("queue") is None and synthetic_queue is not None:
                result["queue"] = synthetic_queue
            if work:
                result["candidate_work"] = work
                result["training_backend"] = "candidate_worker"
                if str(row.get("state") or "") == "building":
                    result["training_progress"] = max(
                        float(result.get("training_progress") or 0.0),
                        float(work.get("progress") or 0.0),
                    )

        if not gate.get("passed"):
            result["promotable"] = False
            if isinstance(result.get("comparison"), dict):
                result["comparison"]["promotable"] = False
        return result

    manager._create_candidate = create_candidate
    manager._start_build = start_build
    manager._finish_build_if_ready = finish_build_if_ready
    manager._comparison_summary = comparison_summary
    manager.before_live_process = before_live_process
    manager.after_live_process = after_live_process
    manager.status = status
    manager._candidate_conservative_correct = True
    manager.candidate_correct_contract = "exact_live_snapshot_then_conservative_finetune"
    manager.candidate_offline_gate_contract = "held_out_history_before_future_ab"

    # A pre-upgrade Candidate that was already comparing never passed this new gate.
    # Requeue it instead of silently grandfathering stale future evidence.
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE agent_candidates SET state='queued',dirty=1,comparison_json='{}',
               queued_ts=?,updated_ts=?
               WHERE state IN ('comparing','ready')
                 AND (offline_gate_json IS NULL OR offline_gate_json='' OR offline_gate_json='{}')""",
            (now, now),
        )
    manager.wake_event.set()
    return manager
