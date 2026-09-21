"""Class-balanced safety layer for binary Candidate Correct.

Users normally mark *errors*, not representative examples.  A sequence such as
"this should have been ON" is therefore error sampling and must not be interpreted as
an ON-heavy class distribution.  The base conservative Correct already starts from an
exact Live snapshot; this layer keeps that property and changes only the supervised
fine-tune batch:

* collect all marked errors against the untouched parent snapshot,
* for binary targets, fill the under-represented Desired class with correctly-predicted
  historical stability anchors from the same frozen feature schema,
* choose the closest anchors in feature space so we protect the relevant decision
  boundary rather than replaying arbitrary OFF/ON history,
* train the resulting class-balanced batch for at most three small rounds,
* apply a rejected-action negative update only once per marked error,
* keep the normal same-row offline regression gate and future paired A/B afterwards.

This module never creates ActionIntent and never calls Executor.  It patches the two
module-level hooks used by ``agent_candidate_conservative_correct`` after that extension
has been installed.
"""
from __future__ import annotations

import math
import time
import uuid

import agent_candidate_conservative_correct as conservative


_MAX_CORRECTION_ROUNDS = 3
_MAX_ANCHOR_REPEAT = 3
_PATCHED = False
_BASE_FINE_TUNE = conservative._conservative_fine_tune
_BASE_OFFLINE_GATE = conservative._offline_gate


def _feature_distance(left, right):
    """Mean squared distance over the frozen explicit schema, excluding the bias slot."""
    keys = (set(left or {}) | set(right or {})) - {0}
    if not keys:
        return 0.0
    return sum((float((left or {}).get(k, 0.0)) - float((right or {}).get(k, 0.0))) ** 2 for k in keys) / len(keys)


def _anchor_pool(manager, candidate, policy, teach_times):
    """Historical rows the untouched parent snapshot already gets right.

    Stability anchors are deliberately positive, observed history.  We never invent a
    synthetic OFF/ON label.  A row is eligible only when the snapshot itself predicts the
    recorded action, so Correct cannot use a known parent mistake as a preservation anchor.
    """
    with manager.store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT h.target_history_id,h.action_index,h.action_value,h.reward,h.features_json,e.ts
               FROM historical_experiences h
               LEFT JOIN entity_history e ON e.id=h.target_history_id
               WHERE h.agent_id=? AND h.reward>0 ORDER BY e.ts,h.id""",
            (str(candidate["id"]),),
        ).fetchall()]

    actions = [float(x) for x in policy.actions]
    out = []
    for row in rows:
        ts = row.get("ts")
        if ts is not None and any(abs(float(ts) - float(t)) <= .5 for t in teach_times):
            continue
        features = conservative._features(row.get("features_json"))
        if not features:
            continue
        try:
            actual_idx = int(row["action_index"])
            if actual_idx < 0 or actual_idx >= len(actions):
                continue
            predicted_idx, _ = conservative._prediction_index(policy, features)
        except (TypeError, ValueError, RuntimeError, KeyError):
            continue
        if predicted_idx != actual_idx:
            continue
        out.append({
            "target_history_id": int(row["target_history_id"]),
            "ts": None if ts is None else float(ts),
            "action_idx": actual_idx,
            "features": features,
        })
    return out


def _select_balancing_anchors(manager, candidate, policy, usable):
    """Fill binary Desired-class deficits with nearest correct historical examples."""
    if len(policy.actions) != 2:
        return [], {"required": False, "correction_class_counts": {}, "anchor_class_counts": {}, "shortfall": {}}

    correction_counts = {0: 0, 1: 0}
    for sample in usable:
        correction_counts[int(sample["desired_idx"])] += 1
    target = max(correction_counts.values()) if correction_counts else 0
    if target <= 0:
        return [], {
            "required": False,
            "correction_class_counts": {str(k): int(v) for k, v in correction_counts.items()},
            "anchor_class_counts": {"0": 0, "1": 0},
            "shortfall": {"0": 0, "1": 0},
        }

    teach_times = [float(x["label"]["sample_ts"]) for x in usable]
    pool = _anchor_pool(manager, candidate, policy, teach_times)
    anchors = []
    anchor_counts = {0: 0, 1: 0}
    shortfall = {0: 0, 1: 0}

    for action_idx in (0, 1):
        need = max(0, target - correction_counts[action_idx])
        if need <= 0:
            continue
        candidates = [row for row in pool if int(row["action_idx"]) == action_idx]
        # Protect the boundary around the errors that are pushing *away* from this class.
        references = [x["features"] for x in usable if int(x["desired_idx"]) != action_idx]
        if not references:
            references = [x["features"] for x in usable]
        scored = []
        for row in candidates:
            distance = min((_feature_distance(row["features"], ref) for ref in references), default=0.0)
            scored.append((distance, row["ts"] if row["ts"] is not None else float("inf"), row["target_history_id"], row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        chosen = [item[3] for item in scored[:need]]
        anchors.extend(chosen)
        anchor_counts[action_idx] = len(chosen)
        shortfall[action_idx] = max(0, need - len(chosen))

    return anchors, {
        "required": bool(correction_counts[0] != correction_counts[1]),
        "target_per_class": int(target),
        "correction_class_counts": {str(k): int(v) for k, v in correction_counts.items()},
        "anchor_class_counts": {str(k): int(v) for k, v in anchor_counts.items()},
        "shortfall": {str(k): int(v) for k, v in shortfall.items()},
        "available_correct_anchor_rows": int(len(pool)),
    }


def _fit_count(policy, samples, *, desired_key="desired_idx"):
    correct = 0
    for sample in samples:
        predicted_idx, _ = conservative._prediction_index(policy, sample["features"])
        correct += int(int(predicted_idx) == int(sample[desired_key]))
    return correct


def _balanced_fine_tune(manager, candidate):
    """Fine-tune a binary snapshot as a balanced *error-correction* batch.

    Corrections are not assumed to describe the natural class prior.  For non-binary
    targets we deliberately keep the established conservative implementation unchanged.
    """
    service = getattr(manager.engine, "rl_teaching", None)
    if service is None:
        raise RuntimeError("Teach RL service unavailable")

    manager.engine.models.pop(candidate["id"], None)
    policy = manager.engine.policy(candidate)
    if len(policy.actions) != 2:
        return _BASE_FINE_TUNE(manager, candidate)

    labels = conservative._teach_rows(service, candidate)
    deadband = max(.01, float(candidate.get("deadband") or .01))
    usable = []
    before_correct = 0

    # Freeze every pre-correction prediction before the first update.  The negative label
    # always targets the action the parent snapshot really chose, never a later mutation.
    for label in labels:
        features = service._label_context(candidate, policy, label["sample_ts"])
        if features is None:
            continue
        chosen_idx, chosen_value = conservative._prediction_index(policy, features)
        desired_idx = conservative._nearest_action(policy, float(label["desired"]))
        desired_value = float(policy.actions[desired_idx])
        was_correct = abs(chosen_value - desired_value) <= deadband
        before_correct += int(was_correct)
        usable.append({
            "label": label,
            "features": features,
            "chosen_idx": int(chosen_idx),
            "chosen_value": float(chosen_value),
            "desired_idx": int(desired_idx),
            "desired_value": desired_value,
            "was_correct": bool(was_correct),
        })

    if not usable:
        return _BASE_FINE_TUNE(manager, candidate)

    anchors, balance = _select_balancing_anchors(manager, candidate, policy, usable)
    for anchor in anchors:
        anchor["desired_idx"] = int(anchor["action_idx"])

    base_revision = str(getattr(policy, "model_revision", "") or "")
    negative_updates = 0
    positive_updates = {0: 0, 1: 0}
    anchor_updates = {0: 0, 1: 0}
    rounds = 0

    # One rejected-action penalty is enough to encode "not this action here".  Repeating
    # the negative three/six times was the main route by which error-only ON labels could
    # erase a perfectly valid OFF arm globally through the shared bias feature.
    for sample in usable:
        if sample["chosen_idx"] == sample["desired_idx"]:
            continue
        for horizon in policy.horizons:
            policy.update(horizon, sample["chosen_idx"], sample["features"], -1.0)
            negative_updates += 1

    # Small bounded rounds.  Every round contains the same number of positive examples
    # per Desired class after historical anchors are added.  Stop as soon as all explicit
    # corrections fit *and* every selected stability anchor is still retained.
    for round_index in range(_MAX_CORRECTION_ROUNDS):
        rounds = round_index + 1
        for sample in usable:
            for horizon in policy.horizons:
                policy.update(horizon, sample["desired_idx"], sample["features"], 1.0)
                positive_updates[sample["desired_idx"]] += 1
        for anchor in anchors:
            for horizon in policy.horizons:
                policy.update(horizon, anchor["desired_idx"], anchor["features"], 1.0)
                anchor_updates[anchor["desired_idx"]] += 1

        correction_fit = _fit_count(policy, usable)
        anchor_fit = _fit_count(policy, anchors) if anchors else 0
        if correction_fit == len(usable) and (not anchors or anchor_fit == len(anchors)):
            break

    if usable or anchors:
        policy.model_revision = str(uuid.uuid4())

    after_correct = _fit_count(policy, usable)
    anchor_correct = _fit_count(policy, anchors) if anchors else 0
    observed_predictions = set()
    for sample in usable + anchors:
        predicted_idx, _ = conservative._prediction_index(policy, sample["features"])
        observed_predictions.add(int(predicted_idx))

    for sample in usable:
        if sample["chosen_idx"] != sample["desired_idx"]:
            manager.store.add_feedback(
                candidate["id"], sample["chosen_idx"], policy.actions[sample["chosen_idx"]], -1.0,
                "Candidate Correct rejected actual pre-correction prediction",
                sample["features"], "teach-ui", source="candidate_correct",
            )
        manager.store.add_feedback(
            candidate["id"], sample["desired_idx"], policy.actions[sample["desired_idx"]], 1.0,
            "Candidate Correct desired correction", sample["features"], "teach-ui",
            source="candidate_correct",
        )

    manager.store.save_model(candidate["id"], policy.serialize())
    manager.engine.models[candidate["id"]] = policy

    correction_counts = balance.get("correction_class_counts") or {}
    anchor_counts = balance.get("anchor_class_counts") or {}
    return {
        "mode": "conservative_snapshot_finetune",
        "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
        "labels_applied": len(usable),
        "teach_fit_before_count": int(before_correct),
        "teach_fit_after_count": int(after_correct),
        "teach_fit_total": len(usable),
        "teach_fit_before": (before_correct / len(usable)) if usable else None,
        "teach_fit_after": (after_correct / len(usable)) if usable else None,
        "correction_rounds": int(rounds),
        "correction_class_counts": correction_counts,
        "stability_anchor_class_counts": anchor_counts,
        "stability_anchor_total": len(anchors),
        "stability_anchor_retained": int(anchor_correct),
        "stability_anchor_shortfall": balance.get("shortfall") or {},
        "stability_anchor_available": int(balance.get("available_correct_anchor_rows") or 0),
        "class_balance_required": bool(balance.get("required")),
        "positive_updates_by_class": {str(k): int(v) for k, v in positive_updates.items()},
        "anchor_updates_by_class": {str(k): int(v) for k, v in anchor_updates.items()},
        "negative_updates": int(negative_updates),
        "negative_actions": sum(1 for x in usable if x["chosen_idx"] != x["desired_idx"]),
        "balanced_support_predicted_class_coverage": len(observed_predictions),
        "previous_desired_used_for_negative": False,
        "schema_changed": False,
        "base_model_revision": base_revision or None,
        "candidate_model_revision": str(getattr(policy, "model_revision", "") or "") or None,
        # Private hand-off for the later margin-repair layer. It is removed before any
        # offline gate/report persistence, so raw feature vectors never become UI/API
        # diagnostics merely because Stage 2 needs to re-check anchor retention.
        "_stability_anchor_samples": [
            {
                "target_history_id": anchor.get("target_history_id"),
                "desired_idx": int(anchor["desired_idx"]),
                "features": dict(anchor["features"]),
            }
            for anchor in anchors
        ],
    }


def _balanced_offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    gate = _BASE_OFFLINE_GATE(parent, parent_stats, candidate_stats, teach_report)
    report = dict(teach_report or {})
    if report.get("balance_mode") != "error_only_labels_plus_context_matched_historical_anchors":
        return gate

    gate["balance_mode"] = report.get("balance_mode")
    gate["correction_class_counts"] = report.get("correction_class_counts") or {}
    gate["stability_anchor_class_counts"] = report.get("stability_anchor_class_counts") or {}
    gate["stability_anchor_shortfall"] = report.get("stability_anchor_shortfall") or {}
    gate["stability_anchor_total"] = int(report.get("stability_anchor_total") or 0)
    gate["stability_anchor_retained"] = int(report.get("stability_anchor_retained") or 0)
    gate["correction_rounds"] = int(report.get("correction_rounds") or 0)

    reasons = list(gate.get("reasons") or [])
    total = int(report.get("teach_fit_total") or 0)
    before = int(report.get("teach_fit_before_count") or 0)
    after = int(report.get("teach_fit_after_count") or 0)
    # A marked error must actually improve.  "0/4 before -> 0/4 after" is not a
    # successful correction merely because it did not get numerically worse.
    if total > 0 and before < total and after <= before:
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct did not improve any marked error" not in reasons:
            reasons.append("Correct did not improve any marked error")

    shortfall = sum(int(v or 0) for v in (report.get("stability_anchor_shortfall") or {}).values())
    if bool(report.get("class_balance_required")) and shortfall > 0:
        gate["passed"] = False
        gate["status"] = "insufficient_evidence"
        if "insufficient opposite-class historical stability anchors" not in reasons:
            reasons.append("insufficient opposite-class historical stability anchors")

    anchor_total = int(report.get("stability_anchor_total") or 0)
    anchor_retained = int(report.get("stability_anchor_retained") or 0)
    if anchor_total > 0 and anchor_retained < anchor_total:
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct damaged selected opposite-class stability anchors" not in reasons:
            reasons.append("Correct damaged selected opposite-class stability anchors")

    gate["reasons"] = reasons
    gate["updated_ts"] = time.time()
    return gate


def install(manager):
    global _PATCHED
    if getattr(manager, "_candidate_balanced_correct_installed", False):
        return manager
    if not _PATCHED:
        conservative._conservative_fine_tune = _balanced_fine_tune
        conservative._offline_gate = _balanced_offline_gate
        _PATCHED = True
    manager._candidate_balanced_correct_installed = True
    manager.candidate_correct_balance_contract = (
        "error_only_binary_labels_balanced_with_context_matched_correct_history_and_bounded_three_round_finetune"
    )
    return manager
