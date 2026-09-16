"""Prioritize explicit user Correct labels over soft stability anchors.

A user marks points because the Candidate/Parent decision at those exact observed
contexts was wrong.  Those labels are therefore correction constraints, not a
representative sample of the natural ON/OFF class distribution.

The 0.14.3 balancing layer correctly added opposite-class historical anchors, but it
also treated preservation of *every* selected anchor as a hard requirement.  That could
leave an explicitly marked error uncorrected or block an otherwise healthy Candidate
because one local anchor moved.

This layer makes the objective hierarchical:

1. every usable explicit Correct label is a hard fit target;
2. class balancing anchors remain soft regularization/evidence;
3. the ordinary same-row historical regression gate, per-action accuracy checks and
   one-class-collapse protection remain authoritative for broad regressions;
4. if the model cannot fit every explicit correction after bounded repair rounds, the
   offline gate fails with an explicit reason instead of pretending the correction was
   successful.

No ActionIntent is created and no Executor authority is changed here.
"""
from __future__ import annotations

import time
import uuid

import agent_candidate_balanced_correct as balanced
import agent_candidate_conservative_correct as conservative


# 0.14.3 already performs up to three balanced rounds.  If explicit errors still do not
# fit, allow a bounded set of targeted positive repair rounds.  These rounds do not add
# more negative pressure to the opposite arm; the global offline regression gate remains
# responsible for rejecting broad damage.
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


def _hard_correct_fine_tune(manager, candidate):
    """Run balanced Correct, then force all marked errors to fit if necessary."""
    report = dict(_BASE_BALANCED_FINE_TUNE(manager, candidate) or {})
    if report.get("balance_mode") != "error_only_labels_plus_context_matched_historical_anchors":
        return report

    total = int(report.get("teach_fit_total") or 0)
    if total <= 0:
        report["hard_correction_required"] = False
        report["hard_correction_satisfied"] = True
        report["hard_repair_rounds"] = 0
        report["hard_repair_updates"] = 0
        return report

    policy = manager.engine.models.get(candidate["id"])
    if policy is None:
        manager.engine.models.pop(candidate["id"], None)
        policy = manager.engine.policy(candidate)

    usable = _collect_explicit_corrections(manager, candidate, policy)
    if not usable:
        report["hard_correction_required"] = True
        report["hard_correction_satisfied"] = False
        report["hard_repair_rounds"] = 0
        report["hard_repair_updates"] = 0
        report["teach_fit_after_count"] = 0
        report["teach_fit_after"] = 0.0
        return report

    fit = balanced._fit_count(policy, usable)
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

        # Explicit user labels have priority.  Only the desired arm receives additional
        # positive evidence here; the one-time rejected-action penalty was already applied
        # by the balanced layer.  This avoids repeatedly erasing the opposite class while
        # still making the selected wrong points actual correction constraints.
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
    report["teach_fit_after_count"] = int(fit)
    report["teach_fit_after"] = fit / len(usable) if usable else None
    report["hard_correction_required"] = int(report.get("teach_fit_before_count") or 0) < len(usable)
    report["hard_correction_satisfied"] = bool(fit == len(usable))
    report["hard_repair_rounds"] = int(repair_rounds)
    report["hard_repair_updates"] = int(repair_updates)
    report["correction_rounds"] = int(report.get("correction_rounds") or 0) + int(repair_rounds)
    return report


def _hard_offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    """Require every explicit correction; treat anchor loss as diagnostic, not veto.

    Broad safety remains enforced by the original conservative gate: balanced held-out
    evidence, per-action regression limits and one-class-collapse detection are unchanged.
    """
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

    reasons = list(gate.get("reasons") or [])
    total = int(report.get("teach_fit_total") or 0)
    after = int(report.get("teach_fit_after_count") or 0)

    # Explicit user corrections are the hard requirement.  A Candidate is not considered
    # successfully Corrected while even one selected wrong point remains wrong.
    if total > 0 and after < total:
        gate["passed"] = False
        gate["status"] = "failed"
        if "Correct did not satisfy all marked corrections" not in reasons:
            reasons.append("Correct did not satisfy all marked corrections")

    # Missing balancing evidence is still an evidence-quality problem: we do not know if
    # a one-sided correction can be safely regularized against the opposite class.
    shortfall = sum(int(v or 0) for v in (report.get("stability_anchor_shortfall") or {}).values())
    if bool(report.get("class_balance_required")) and shortfall > 0:
        gate["passed"] = False
        gate["status"] = "insufficient_evidence"
        if "insufficient opposite-class historical stability anchors" not in reasons:
            reasons.append("insufficient opposite-class historical stability anchors")

    # Anchors are soft regularization.  Losing one selected local anchor is reported, but
    # it is not by itself a veto.  The ordinary held-out per-class/global regression gate
    # decides whether the change actually damaged the policy materially.
    anchor_total = int(report.get("stability_anchor_total") or 0)
    anchor_retained = int(report.get("stability_anchor_retained") or 0)
    retention = (anchor_retained / anchor_total) if anchor_total else None
    gate["stability_anchor_retention"] = retention
    gate["stability_anchor_warning"] = bool(anchor_total and anchor_retained < anchor_total)
    gate["hard_correction_satisfied"] = bool(total <= 0 or after == total)

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
        "explicit_marked_errors_are_hard_fit_targets_while_opposite_class_anchors_are_soft_regularization"
    )
    return manager
