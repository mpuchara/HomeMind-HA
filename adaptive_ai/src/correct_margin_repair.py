"""Stage-2 Correct optimizer: margin repair plus a stable correction base.

The old hard-repair loop repeatedly added positive evidence to Desired.  For a diagonal
per-arm learner that does not move the already-winning wrong arm, so a strongly established
wrong decision can remain ahead after many updates.  This stage repairs the *decision
margin*: Desired is reinforced while the strongest competing arm is explicitly penalized.

The second contract separates lineage parentage from optimization ancestry.  Candidate
lineage still records the direct parent for audit/A-B comparison, but a failed
offline-blocked Candidate is never used as the next weight base.  Correct starts from the
newest retained Candidate whose offline gate passed, or Root Live when no such correction
champion exists.  Supervision labels still sync from the direct parent, so restarting from
a stable model does not lose accumulated Correct facts.

This module changes Candidate-build/training semantics only.  It never enters realtime
event->intent inference and never creates ActionIntent or calls Executor.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import time
import uuid

import agent_candidate_balanced_correct as balanced
import agent_candidate_conservative_correct as conservative
import agent_candidate_hard_correct as hard
import agent_candidate_lineage as lineage
from settings import OPTIONS


_PATCHED = False
_BASE_BALANCED_FINE_TUNE = hard._BASE_BALANCED_FINE_TUNE
_BASE_HARD_GATE = hard._hard_offline_gate
_BASE_COPY_PARENT_SNAPSHOT = conservative._copy_parent_snapshot

_DEFAULT_TARGET_MARGIN = 0.02
_DEFAULT_MAX_ROUNDS = 12
_DEFAULT_PER_LABEL_ROUNDS = 12
_DEFAULT_STALL_ROUNDS = 2
_DEFAULT_MIN_PROGRESS = 1e-4


def _json(raw, default=None):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _option(name, default):
    try:
        return type(default)(OPTIONS.get(name, default))
    except Exception:
        return default


def _event_id(label):
    value = (label or {}).get("supervision_event_id")
    if value:
        return str(value)
    try:
        from correct_data_foundation import supervision_event_id
        return supervision_event_id(
            (label or {}).get("fingerprint"),
            (label or {}).get("sample_ts"),
            (label or {}).get("desired"),
        )
    except Exception:
        return "label:%s" % str((label or {}).get("id") or "unknown")


def _arm_mean(arms, index):
    for arm in arms or ():
        if int(arm.get("index", -1)) == int(index):
            try:
                return float(arm.get("mean") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _strongest_competitor(arms, desired_idx):
    others = [arm for arm in (arms or ()) if int(arm.get("index", -1)) != int(desired_idx)]
    if not others:
        return None
    return max(
        others,
        key=lambda arm: (float(arm.get("mean") or 0.0), -int(arm.get("index") or 0)),
    )


def _decision_state(policy, sample):
    chosen, _confidence, arms, horizon, _support, _novelty = policy.predict(
        sample["features"]
    )
    desired_idx = int(sample["desired_idx"])
    competitor = _strongest_competitor(arms, desired_idx)
    desired_mean = _arm_mean(arms, desired_idx)
    wrong_idx = None if competitor is None else int(competitor["index"])
    wrong_mean = desired_mean if competitor is None else float(competitor.get("mean") or 0.0)
    return {
        "predicted_idx": int(chosen["index"]),
        "desired_idx": desired_idx,
        "horizon": int(horizon),
        "desired_mean": desired_mean,
        "wrong_idx": wrong_idx,
        "wrong_mean": wrong_mean,
        "margin": desired_mean - wrong_mean,
    }


def _margin_summary(policy, samples):
    rows = []
    for sample in samples or ():
        state = _decision_state(policy, sample)
        state["supervision_event_id"] = _event_id(sample.get("label") or {})
        state["label_id"] = (sample.get("label") or {}).get("id")
        rows.append(state)
    margins = [float(row["margin"]) for row in rows]
    fit = sum(
        1 for row in rows
        if int(row["predicted_idx"]) == int(row["desired_idx"])
    )
    return {
        "fit": int(fit),
        "total": len(rows),
        "margin_min": min(margins) if margins else None,
        "margin_mean": (sum(margins) / len(margins)) if margins else None,
        "rows": rows,
    }


def _head_margin(policy, horizon, desired_idx, features):
    head = policy.heads[int(horizon)]
    arms = head.evaluate(features)
    competitor = _strongest_competitor(arms, desired_idx)
    desired_mean = _arm_mean(arms, desired_idx)
    if competitor is None:
        return None, desired_mean, desired_mean, float("inf")
    wrong_idx = int(competitor["index"])
    wrong_mean = float(competitor.get("mean") or 0.0)
    return wrong_idx, desired_mean, wrong_mean, desired_mean - wrong_mean


def _update_margin_pair(policy, sample, target_margin):
    """Move both sides of the boundary for every horizon that still lacks margin."""
    desired_idx = int(sample["desired_idx"])
    features = sample["features"]
    updates = 0
    negative = 0
    positive = 0
    # Candidate policies are isolated from live inference; one lock keeps the pair
    # atomic while avoiding a new model_revision UUID for every individual head update.
    with policy.lock:
        for horizon in policy.horizons:
            wrong_idx, _desired_mean, _wrong_mean, margin = _head_margin(
                policy, horizon, desired_idx, features
            )
            if wrong_idx is None or margin >= float(target_margin):
                continue
            head = policy.heads[int(horizon)]
            head.update(int(wrong_idx), features, -1.0)
            head.update(desired_idx, features, 1.0)
            negative += 1
            positive += 1
            updates += 2
    return updates, positive, negative


def _repair_margins(
    policy,
    samples,
    *,
    target_margin=None,
    max_rounds=None,
    per_label_round_budget=None,
    stall_rounds=None,
    min_progress=None,
):
    """Bounded pairwise repair with per-label budgets and progress stopping."""
    target_margin = max(
        0.0,
        float(_DEFAULT_TARGET_MARGIN if target_margin is None else target_margin),
    )
    max_rounds = max(
        1, int(_DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds)
    )
    per_label_round_budget = max(
        1,
        int(
            _DEFAULT_PER_LABEL_ROUNDS
            if per_label_round_budget is None
            else per_label_round_budget
        ),
    )
    stall_rounds = max(
        1, int(_DEFAULT_STALL_ROUNDS if stall_rounds is None else stall_rounds)
    )
    min_progress = max(
        0.0,
        float(_DEFAULT_MIN_PROGRESS if min_progress is None else min_progress),
    )

    before = _margin_summary(policy, samples)
    previous = before
    label_rounds = Counter()
    positive_updates = negative_updates = total_updates = 0
    rounds = stalled = 0
    stop_reason = "already_satisfied"
    round_history = []

    def satisfied(summary):
        if int(summary["total"]) <= 0:
            return True
        return all(
            int(row["predicted_idx"]) == int(row["desired_idx"])
            and float(row["margin"]) >= target_margin
            for row in summary["rows"]
        )

    if not satisfied(before):
        stop_reason = "max_rounds"

    while not satisfied(previous) and rounds < max_rounds:
        rounds += 1
        round_updates = 0
        budget_exhausted = True
        for sample in samples:
            state = _decision_state(policy, sample)
            if (
                int(state["predicted_idx"]) == int(state["desired_idx"])
                and float(state["margin"]) >= target_margin
            ):
                continue
            key = _event_id(sample.get("label") or {})
            if label_rounds[key] >= per_label_round_budget:
                continue
            budget_exhausted = False
            updates, positive, negative = _update_margin_pair(
                policy, sample, target_margin
            )
            if updates:
                label_rounds[key] += 1
                round_updates += updates
                total_updates += updates
                positive_updates += positive
                negative_updates += negative

        current = _margin_summary(policy, samples)
        fit_gain = int(current["fit"]) - int(previous["fit"])
        prev_mean = previous.get("margin_mean")
        curr_mean = current.get("margin_mean")
        margin_gain = (
            0.0
            if prev_mean is None or curr_mean is None
            else float(curr_mean) - float(prev_mean)
        )
        round_history.append({
            "round": rounds,
            "fit": int(current["fit"]),
            "total": int(current["total"]),
            "margin_min": current.get("margin_min"),
            "margin_mean": current.get("margin_mean"),
            "fit_gain": int(fit_gain),
            "margin_gain": float(margin_gain),
            "updates": int(round_updates),
        })

        if satisfied(current):
            stop_reason = "target_margin_satisfied"
            previous = current
            break
        if round_updates <= 0:
            stop_reason = "per_label_budget_exhausted" if budget_exhausted else "no_updates"
            previous = current
            break
        if fit_gain <= 0 and margin_gain < min_progress:
            stalled += 1
        else:
            stalled = 0
        if stalled >= stall_rounds:
            stop_reason = "no_margin_progress"
            previous = current
            break
        previous = current

    after = _margin_summary(policy, samples)
    return {
        "before": before,
        "after": after,
        "rounds": int(rounds),
        "updates": int(total_updates),
        "positive_updates": int(positive_updates),
        "negative_updates": int(negative_updates),
        "target_margin": float(target_margin),
        "per_label_round_budget": int(per_label_round_budget),
        "per_label_rounds": dict(sorted(label_rounds.items())),
        "stop_reason": stop_reason,
        "round_history": round_history,
    }


def _margin_correct_fine_tune(manager, candidate):
    """Balanced first pass followed by bounded pairwise margin repair."""
    report = dict(_BASE_BALANCED_FINE_TUNE(manager, candidate) or {})
    if report.get("balance_mode") != "error_only_labels_plus_context_matched_historical_anchors":
        return report

    # Balanced stage may expose its selected anchors privately for post-repair diagnostics.
    anchors = list(report.pop("_stability_anchor_samples", []) or [])

    policy = manager.engine.models.get(candidate["id"])
    if policy is None:
        manager.engine.models.pop(candidate["id"], None)
        policy = manager.engine.policy(candidate)

    all_explicit = hard._collect_explicit_corrections(manager, candidate, policy)
    usable, conflicts = hard._partition_conflicts(all_explicit)
    conflicting_ids = sorted(
        {
            label_id
            for group in conflicts
            for label_id in (group.get("label_ids") or [])
        }
    )
    report["conflicting_correction_count"] = len(conflicting_ids)
    report["conflicting_correction_groups"] = conflicts
    report["conflicting_label_ids"] = conflicting_ids
    report["conflict_policy"] = (
        "exclude_indistinguishable_contradictions_from_hard_fit_and_request_context"
    )
    report["hard_fit_target_total"] = len(usable)

    if not usable:
        report.update({
            "hard_correction_required": False,
            "hard_correction_satisfied": True,
            "hard_fit_before_count": 0,
            "hard_fit_after_count": 0,
            "hard_repair_rounds": 0,
            "hard_repair_updates": 0,
            "hard_repair_positive_updates": 0,
            "hard_repair_negative_updates": 0,
            "hard_repair_stop_reason": "no_nonconflicting_targets",
            "hard_margin_target": float(
                _option("correct_hard_target_margin", _DEFAULT_TARGET_MARGIN)
            ),
            "teach_fit_before_count": 0,
            "teach_fit_after_count": 0,
            "teach_fit_total": 0,
            "teach_fit_before": None,
            "teach_fit_after": None,
        })
        return report

    target_margin = max(
        0.0, float(_option("correct_hard_target_margin", _DEFAULT_TARGET_MARGIN))
    )
    repair = _repair_margins(
        policy,
        usable,
        target_margin=target_margin,
        max_rounds=max(
            1, int(_option("correct_hard_max_repair_rounds", _DEFAULT_MAX_ROUNDS))
        ),
        per_label_round_budget=max(
            1,
            int(
                _option(
                    "correct_hard_per_label_round_budget",
                    _DEFAULT_PER_LABEL_ROUNDS,
                )
            ),
        ),
        stall_rounds=max(
            1,
            int(_option("correct_hard_stall_rounds", _DEFAULT_STALL_ROUNDS)),
        ),
        min_progress=max(
            0.0,
            float(_option("correct_hard_min_margin_progress", _DEFAULT_MIN_PROGRESS)),
        ),
    )

    if repair["updates"]:
        policy.model_revision = str(uuid.uuid4())
        manager.store.save_model(candidate["id"], policy.serialize())
        manager.engine.models[candidate["id"]] = policy

    before = repair["before"]
    after = repair["after"]
    total = len(usable)
    anchor_retained = balanced._fit_count(policy, anchors) if anchors else 0
    if anchors:
        report["stability_anchor_retained"] = int(anchor_retained)

    report.update({
        "hard_fit_before_count": int(before["fit"]),
        "hard_fit_after_count": int(after["fit"]),
        "hard_correction_required": (
            int(before["fit"]) < total
            or any(float(row["margin"]) < target_margin for row in before["rows"])
        ),
        "hard_correction_satisfied": bool(
            total > 0
            and int(after["fit"]) == total
            and all(float(row["margin"]) >= target_margin for row in after["rows"])
        ),
        "hard_repair_rounds": int(repair["rounds"]),
        "hard_repair_updates": int(repair["updates"]),
        "hard_repair_positive_updates": int(repair["positive_updates"]),
        "hard_repair_negative_updates": int(repair["negative_updates"]),
        "hard_repair_stop_reason": repair["stop_reason"],
        "hard_repair_per_label_round_budget": int(repair["per_label_round_budget"]),
        "hard_repair_per_label_rounds": repair["per_label_rounds"],
        "hard_margin_target": float(target_margin),
        "hard_margin_before_min": before.get("margin_min"),
        "hard_margin_before_mean": before.get("margin_mean"),
        "hard_margin_after_min": after.get("margin_min"),
        "hard_margin_after_mean": after.get("margin_mean"),
        "hard_margin_round_history": repair["round_history"],
        "hard_unresolved_supervision_ids": [
            row["supervision_event_id"]
            for row in after["rows"]
            if (
                int(row["predicted_idx"]) != int(row["desired_idx"])
                or float(row["margin"]) < target_margin
            )
        ],
        "wrong_arm_penalized_during_hard_repair": bool(
            repair["negative_updates"] > 0
        ),
    })
    report["correction_rounds"] = int(report.get("correction_rounds") or 0) + int(
        repair["rounds"]
    )
    # Keep the established public fit fields aligned with the hard-target population.
    report["teach_fit_before_count"] = int(before["fit"])
    report["teach_fit_after_count"] = int(after["fit"])
    report["teach_fit_total"] = int(total)
    report["teach_fit_before"] = before["fit"] / total if total else None
    report["teach_fit_after"] = after["fit"] / total if total else None

    base = dict(
        getattr(manager, "_correct_margin_base_by_candidate", {}).get(
            str(candidate["id"]), {}
        )
        or {}
    )
    if base:
        report["correction_base"] = base
        report["correction_base_agent_id"] = base.get("agent_id")
        report["correction_base_generation_id"] = base.get("generation_id")
        report["correction_base_reason"] = base.get("reason")
    return report


def _margin_offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    report = dict(teach_report or {})
    gate = dict(_BASE_HARD_GATE(parent, parent_stats, candidate_stats, report) or {})
    for key in (
        "hard_repair_positive_updates",
        "hard_repair_negative_updates",
        "hard_repair_stop_reason",
        "hard_repair_per_label_round_budget",
        "hard_repair_per_label_rounds",
        "hard_margin_target",
        "hard_margin_before_min",
        "hard_margin_before_mean",
        "hard_margin_after_min",
        "hard_margin_after_mean",
        "hard_margin_round_history",
        "hard_unresolved_supervision_ids",
        "wrong_arm_penalized_during_hard_repair",
        "correction_base",
        "correction_base_agent_id",
        "correction_base_generation_id",
        "correction_base_reason",
    ):
        if key in report:
            gate[key] = report[key]

    target = float(report.get("hard_margin_target") or 0.0)
    unresolved = list(report.get("hard_unresolved_supervision_ids") or [])
    reasons = list(gate.get("reasons") or [])
    if unresolved:
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct decision margin remains unresolved" not in reasons:
            reasons.append("Correct decision margin remains unresolved")
    if (
        report.get("hard_margin_after_min") is not None
        and float(report["hard_margin_after_min"]) + 1e-12 < target
    ):
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct minimum margin is below target" not in reasons:
            reasons.append("Correct minimum margin is below target")
    gate["reasons"] = reasons
    gate["correct_optimizer"] = "pairwise_desired_plus_wrong_arm_penalty"
    return gate


def _gate_passed(raw):
    return bool(_json(raw, {}).get("passed"))


def _select_stable_candidate(rows, expected_config_fingerprint):
    """Pure newest-passed selection used by resolver and tests."""
    expected = str(expected_config_fingerprint or "")
    for raw in sorted(
        (dict(row) for row in (rows or ())),
        key=lambda row: (
            int(row.get("generation_number") or -1),
            float(row.get("created_ts") or 0.0),
        ),
        reverse=True,
    ):
        if not raw.get("agent_id") or not int(raw.get("model_retained", 1)):
            continue
        if expected and str(raw.get("config_fingerprint") or "") != expected:
            continue
        if not _gate_passed(raw.get("offline_gate_json")):
            continue
        return raw
    return None


def _resolve_correction_base(manager, parent_id):
    parent_id = str(parent_id)
    parent_agent = manager.store.get_agent_config(parent_id)
    if not parent_agent:
        raise RuntimeError("Correction parent agent is unavailable")
    generation = lineage._row(manager.store, agent_id=parent_id)
    if not generation or generation.get("generation_type") != "candidate":
        return {
            "agent_id": parent_id,
            "generation_id": generation.get("generation_id") if generation else None,
            "generation_number": (
                int(generation.get("generation_number") or 0) if generation else None
            ),
            "reason": "direct_live_parent",
            "direct_parent_agent_id": parent_id,
            "root_agent_id": (
                generation.get("root_agent_id") if generation else parent_id
            ),
        }

    root_id = str(generation["root_agent_id"])
    expected_config = lineage.config_fingerprint(parent_agent)
    with manager.store.conn() as c:
        rows = [
            dict(row)
            for row in c.execute(
                """SELECT g.*, e.offline_gate_json, e.state AS edge_state
                   FROM agent_candidate_generations g
                   LEFT JOIN agent_candidates e ON e.candidate_id=g.agent_id
                   WHERE g.root_agent_id=? AND g.generation_type='candidate'
                     AND g.agent_id IS NOT NULL
                   ORDER BY g.generation_number DESC,g.created_ts DESC""",
                (root_id,),
            ).fetchall()
        ]

    selected = _select_stable_candidate(rows, expected_config)
    if selected is not None and manager.store.get_model(str(selected["agent_id"])):
        return {
            "agent_id": str(selected["agent_id"]),
            "generation_id": selected.get("generation_id"),
            "generation_number": int(selected.get("generation_number") or 0),
            "reason": (
                "direct_parent_offline_passed"
                if str(selected["agent_id"]) == parent_id
                else "latest_offline_passed_candidate"
            ),
            "direct_parent_agent_id": parent_id,
            "root_agent_id": root_id,
        }

    root_agent = manager.store.get_agent_config(root_id)
    if not root_agent or manager.store.get_model(root_id) is None:
        raise RuntimeError("Stable Root Live correction base is unavailable")
    root_config = lineage.config_fingerprint(root_agent)
    if root_config != expected_config:
        raise RuntimeError(
            "Stable correction base config differs from Candidate lineage; Full Rebuild required"
        )
    return {
        "agent_id": root_id,
        "generation_id": "root:%s" % root_id,
        "generation_number": 0,
        "reason": "root_live_after_no_offline_passed_candidate",
        "direct_parent_agent_id": parent_id,
        "root_agent_id": root_id,
    }


def _stable_copy_parent_snapshot(manager, parent_id, candidate_id):
    base = _resolve_correction_base(manager, parent_id)
    result = _BASE_COPY_PARENT_SNAPSHOT(
        manager, str(base["agent_id"]), str(candidate_id)
    )
    runtime = getattr(manager, "_correct_margin_base_by_candidate", None)
    if runtime is None:
        runtime = {}
        manager._correct_margin_base_by_candidate = runtime
    previous = runtime.get(str(candidate_id))
    runtime[str(candidate_id)] = dict(base)
    if (
        base.get("agent_id") != str(parent_id)
        and previous != base
    ):
        manager.store.event(
            base.get("root_agent_id"),
            "info",
            "candidate_correction_base_selected",
            "Correct child restarted from the latest offline-safe correction base",
            {
                **base,
                "candidate_id": str(candidate_id),
                "optimization_parent_differs_from_lineage_parent": True,
            },
        )
    return result


def install(core, manager):
    """Install optimizer/base semantics before Stage-1 diagnostics wraps fine-tune."""
    global _PATCHED
    if getattr(manager, "_correct_margin_repair_installed", False):
        return manager
    if not getattr(manager, "_candidate_hard_correct_installed", False):
        manager = hard.install(manager)

    if not _PATCHED:
        conservative._conservative_fine_tune = _margin_correct_fine_tune
        conservative._offline_gate = _margin_offline_gate
        conservative._copy_parent_snapshot = _stable_copy_parent_snapshot
        hard._hard_correct_fine_tune = _margin_correct_fine_tune
        hard._hard_offline_gate = _margin_offline_gate
        _PATCHED = True

    original_status = manager.status

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        row = manager._candidate_row(parent_id)
        gate = _json((row or {}).get("offline_gate_json"), {})
        result["correct_optimizer"] = gate.get("correct_optimizer")
        result["hard_margin_target"] = gate.get("hard_margin_target")
        result["hard_margin_after_min"] = gate.get("hard_margin_after_min")
        result["hard_margin_after_mean"] = gate.get("hard_margin_after_mean")
        result["hard_repair_stop_reason"] = gate.get("hard_repair_stop_reason")
        result["hard_unresolved_supervision_ids"] = list(
            gate.get("hard_unresolved_supervision_ids") or []
        )
        result["correction_base"] = gate.get("correction_base")
        return result

    manager.status = status
    manager._correct_margin_repair_installed = True
    manager.correct_optimizer_contract = (
        "pairwise_margin_repair_with_wrong_arm_penalty_per_label_budget_and_progress_stop"
    )
    manager.correct_base_contract = (
        "lineage_parent_for_audit_but_weights_from_latest_offline_passed_candidate_or_root_live"
    )
    core.STORE.event(
        None,
        "info",
        "correct_margin_repair_ready",
        "Correct uses pairwise margin repair and an offline-safe optimization base",
        {
            "target_margin": float(
                _option("correct_hard_target_margin", _DEFAULT_TARGET_MARGIN)
            ),
            "max_rounds": int(
                _option("correct_hard_max_repair_rounds", _DEFAULT_MAX_ROUNDS)
            ),
            "per_label_round_budget": int(
                _option(
                    "correct_hard_per_label_round_budget",
                    _DEFAULT_PER_LABEL_ROUNDS,
                )
            ),
            "stall_rounds": int(
                _option("correct_hard_stall_rounds", _DEFAULT_STALL_ROUNDS)
            ),
            "hot_path": False,
        },
    )
    return manager
