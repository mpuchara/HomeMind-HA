"""Prioritize explicit user Correct labels without chasing contradictions forever.

A user marks points because the Candidate/Parent decision at those exact observed
contexts was wrong. Those labels are correction constraints, not a representative sample
of the natural ON/OFF class distribution.

Stage 06 adds an important limit to the old hard-fit rule: two labels that are
indistinguishable to the frozen policy but request different actions are missing-context
conflicts, not two simultaneously satisfiable targets. New journaled feedback normally
prevents such a pair from entering training at all; this layer also protects legacy rows.
Conflicting groups are reported and excluded from the extra hard-repair loop. They are
never turned into repeated evidence and never make 100% fit a promotion requirement.

Non-conflicting corrections keep the existing hierarchy:
1. explicit usable corrections are hard fit targets;
2. class-balancing anchors are soft regularization/evidence;
3. the ordinary held-out regression gate and one-class-collapse protection stay active;
4. repair is strictly bounded.

No ActionIntent is created and no Executor authority is changed here.
"""
from __future__ import annotations

import time
import uuid

import agent_candidate_balanced_correct as balanced
import agent_candidate_conservative_correct as conservative


_MAX_HARD_REPAIR_ROUNDS = 12
_PATCHED = False
_BASE_BALANCED_FINE_TUNE = balanced._balanced_fine_tune
_BASE_OFFLINE_GATE = balanced._BASE_OFFLINE_GATE


def _collect_explicit_corrections(manager, candidate, policy):
    service = getattr(manager.engine, "rl_teaching", None)
    if service is None:
        return []
    labels = conservative._teach_rows(service, candidate)
    usable = []
    for label in labels:
        features = service._label_context(candidate, policy, label["sample_ts"])
        if features is None:
            continue
        desired_idx = conservative._nearest_action(policy, float(label["desired"]))
        usable.append({
            "label": label,
            "features": features,
            "desired_idx": int(desired_idx),
        })
    return usable


def _observable_key(features):
    """Stable equivalence key for what this frozen policy can actually distinguish."""
    out = []
    for key, value in sorted((features or {}).items(), key=lambda item: int(item[0])):
        try:
            out.append((int(key), round(float(value), 7)))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _partition_conflicts(samples):
    groups = {}
    for sample in samples:
        groups.setdefault(_observable_key(sample.get("features")), []).append(sample)

    usable = []
    conflicts = []
    for key, group in groups.items():
        desired = {int(sample["desired_idx"]) for sample in group}
        if len(desired) <= 1:
            usable.extend(group)
            continue
        ids = [
            int(sample["label"]["id"])
            for sample in group
            if sample.get("label", {}).get("id") is not None
        ]
        conflicts.append({
            "label_ids": ids,
            "desired_action_indices": sorted(desired),
            "samples": len(group),
            "observable_features": len(key),
        })
    return usable, conflicts


def _hard_correct_fine_tune(manager, candidate):
    """Run balanced Correct, then hard-repair only satisfiable explicit corrections."""
    report = dict(_BASE_BALANCED_FINE_TUNE(manager, candidate) or {})
    if report.get("balance_mode") != "error_only_labels_plus_context_matched_historical_anchors":
        return report

    policy = manager.engine.models.get(candidate["id"])
    if policy is None:
        manager.engine.models.pop(candidate["id"], None)
        policy = manager.engine.policy(candidate)

    all_explicit = _collect_explicit_corrections(manager, candidate, policy)
    usable, conflicts = _partition_conflicts(all_explicit)
    conflicting_ids = sorted({label_id for group in conflicts for label_id in group["label_ids"]})

    report["conflicting_correction_count"] = len(conflicting_ids)
    report["conflicting_correction_groups"] = conflicts
    report["conflicting_label_ids"] = conflicting_ids
    report["conflict_policy"] = "exclude_indistinguishable_contradictions_from_hard_fit_and_request_context"
    report["hard_fit_target_total"] = len(usable)

    if not usable:
        report["hard_correction_required"] = False
        report["hard_correction_satisfied"] = True
        report["hard_fit_after_count"] = 0
        report["hard_repair_rounds"] = 0
        report["hard_repair_updates"] = 0
        return report

    fit = balanced._fit_count(policy, usable)
    before_fit = fit
    repair_rounds = 0
    repair_updates = 0

    while fit < len(usable) and repair_rounds < _MAX_HARD_REPAIR_ROUNDS:
        repair_rounds += 1
        unresolved = []
        for sample in usable:
            predicted_idx, _ = conservative._prediction_index(policy, sample["features"])
            if int(predicted_idx) != int(sample["desired_idx"]):
                unresolved.append(sample)

        if not unresolved:
            break

        for sample in unresolved:
            for horizon in policy.horizons:
                policy.update(horizon, sample["desired_idx"], sample["features"], 1.0)
                repair_updates += 1

        fit = balanced._fit_count(policy, usable)

    if repair_updates:
        policy.model_revision = str(uuid.uuid4())
        manager.store.save_model(candidate["id"], policy.serialize())
        manager.engine.models[candidate["id"]] = policy

    fit = balanced._fit_count(policy, usable)
    report["hard_fit_before_count"] = int(before_fit)
    report["hard_fit_after_count"] = int(fit)
    report["hard_correction_required"] = before_fit < len(usable)
    report["hard_correction_satisfied"] = bool(fit == len(usable))
    report["hard_repair_rounds"] = int(repair_rounds)
    report["hard_repair_updates"] = int(repair_updates)
    report["correction_rounds"] = int(report.get("correction_rounds") or 0) + int(repair_rounds)
    return report


def _hard_offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    """Require satisfiable explicit corrections; surface conflicts as missing context."""
    report = dict(teach_report or {})
    gate = _BASE_OFFLINE_GATE(parent, parent_stats, candidate_stats, report)
    if report.get("balance_mode") != "error_only_labels_plus_context_matched_historical_anchors":
        return gate

    gate["balance_mode"] = report.get("balance_mode")
    gate["correction_class_counts"] = report.get("correction_class_counts") or {}
    gate["stability_anchor_class_counts"] = report.get("stability_anchor_class_counts") or {}
    gate["stability_anchor_shortfall"] = report.get("stability_anchor_shortfall") or {}
    gate["stability_anchor_total"] = int(report.get("stability_anchor_total") or 0)
    gate["stability_anchor_retained"] = int(report.get("stability_anchor_retained") or 0)
    gate["correction_rounds"] = int(report.get("correction_rounds") or 0)
    gate["hard_repair_rounds"] = int(report.get("hard_repair_rounds") or 0)
    gate["hard_repair_updates"] = int(report.get("hard_repair_updates") or 0)
    gate["conflicting_correction_count"] = int(report.get("conflicting_correction_count") or 0)
    gate["conflicting_correction_groups"] = report.get("conflicting_correction_groups") or []
    gate["conflicting_label_ids"] = report.get("conflicting_label_ids") or []
    gate["conflict_warning"] = bool(gate["conflicting_correction_count"])
    gate["conflict_resolution"] = (
        "collect_additional_context_or_narrow_feedback_scope"
        if gate["conflict_warning"] else None
    )

    reasons = list(gate.get("reasons") or [])
    hard_total = int(report.get("hard_fit_target_total") or 0)
    hard_after = int(report.get("hard_fit_after_count") or 0)

    if hard_total > 0 and hard_after < hard_total:
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct did not satisfy all non-conflicting marked corrections" not in reasons:
            reasons.append("Correct did not satisfy all non-conflicting marked corrections")

    shortfall = sum(int(v or 0) for v in (report.get("stability_anchor_shortfall") or {}).values())
    if bool(report.get("class_balance_required")) and shortfall > 0:
        gate["passed"] = False
        gate["status"] = "insufficient_evidence"
        if "insufficient opposite-class historical stability anchors" not in reasons:
            reasons.append("insufficient opposite-class historical stability anchors")

    anchor_total = int(report.get("stability_anchor_total") or 0)
    anchor_retained = int(report.get("stability_anchor_retained") or 0)
    retention = (anchor_retained / anchor_total) if anchor_total else None
    gate["stability_anchor_retention"] = retention
    gate["stability_anchor_warning"] = bool(anchor_total and anchor_retained < anchor_total)
    gate["hard_correction_satisfied"] = bool(hard_total <= 0 or hard_after == hard_total)
    gate["hard_fit_target_total"] = hard_total
    gate["hard_fit_after_count"] = hard_after

    gate["reasons"] = reasons
    gate["updated_ts"] = time.time()
    return gate


def install(manager):
    global _PATCHED
    manager = balanced.install(manager)
    if getattr(manager, "_candidate_hard_correct_installed", False):
        return manager
    if not _PATCHED:
        conservative._conservative_fine_tune = _hard_correct_fine_tune
        conservative._offline_gate = _hard_offline_gate
        _PATCHED = True
    manager._candidate_hard_correct_installed = True
    manager.candidate_correct_priority_contract = (
        "non_conflicting_explicit_errors_are_hard_fit_targets_conflicts_are_missing_context_not_repeat_evidence"
    )
    return manager
