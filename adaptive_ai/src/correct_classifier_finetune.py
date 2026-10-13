"""Bounded Correct updates to an existing binary classifier, preserving its basis.

Accepted-outcome reservoir refits are appropriate for chronological base training.
Error-selected Correct labels are different: refitting that reservoir from zero on
every copied label can replace a good parent boundary, especially outside Recorder
transport conditions. Keep the parent's normalization and weights as the starting
point, and constrain the update with historical and recent observed agreement.
"""
import copy
import json
import time
import uuid

import agent_candidate_conservative_correct as conservative
import replay
from context import target_value
from training_budget import TRAINING_BUDGET


MAX_RECENT_ROWS = 512
MAX_RECENT_PER_CLASS = 24
MAX_PROTECTED_PER_CLASS = 64
MAX_STEPS = 240
TARGET_MARGIN = .02


def applies(policy):
    heads = getattr(policy, "heads", {})
    return bool(len(policy.actions) == 2 and heads and all(
        getattr(head, "state_classifier", None) is not None
        and head.state_classifier.ready for head in heads.values()
    ))


def recent_agreement(manager, candidate, policy, teach_times):
    """Reconstruct bounded observed parent/current agreement, never invent labels."""
    edge = manager._row_by_candidate(candidate["id"])
    if not edge or getattr(manager.engine, "context", None) is None:
        return []
    parent_id = str(edge["parent_agent_id"])
    end = time.time()
    with manager.store.conn() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "candidate_generation_decisions" in tables:
            rows = [dict(r) for r in db.execute(
                """SELECT ts,current,desired FROM candidate_generation_decisions
                   WHERE generation_id=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT ?""",
                ("candidate:" + parent_id, end - 6 * 3600, end, MAX_RECENT_ROWS),
            )]
        else:
            rows = []
        if not rows and "decision_history" in tables:
            rows = [dict(r) for r in db.execute(
                """SELECT ts,current,desired FROM decision_history
                   WHERE agent_id=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT ?""",
                (parent_id, end - 6 * 3600, end, MAX_RECENT_ROWS),
            )]
    by_class = {0: [], 1: []}
    for row in rows:
        if row["current"] is None or row["desired"] is None:
            continue
        if abs(float(row["current"]) - float(row["desired"])) > .01:
            continue
        if any(abs(float(row["ts"]) - ts) <= 5. for ts in teach_times):
            continue
        action = conservative._nearest_action(policy, row["desired"])
        by_class[action].append(row)
    selected = []
    for bucket in by_class.values():
        # Evenly cover the available timeline rather than selecting one plateau only.
        n = min(len(bucket), MAX_RECENT_PER_CLASS)
        selected.extend(bucket[(i * len(bucket)) // n] for i in range(n))
    if not selected:
        return []
    selected.sort(key=lambda row: row["ts"])
    tracker = replay.SQLiteTemporalTracker(
        manager.store, set(policy.schema.entities) | {candidate["target_entity"]},
        manager.engine.context, selected[0]["ts"] - 600., selected[-1]["ts"],
    )
    samples = []
    try:
        for row in selected:
            TRAINING_BUDGET.checkpoint("candidate_recent_agreement", thread_name=conservative._BUDGET_THREAD_NAME)
            ts = float(row["ts"])
            tracker.advance(ts)
            current = target_value(tracker.state_map.get(candidate["target_entity"]), candidate["target_property"])
            if current is None or abs(float(current) - float(row["current"])) > .01:
                continue
            features, _, meta = policy.features(tracker.state_map, tracker.history, at_ts=ts)
            if not meta.get("reconstruction_complete", False):
                continue
            action = conservative._nearest_action(policy, row["desired"])
            if conservative._prediction_index(policy, features)[0] != action:
                continue
            samples.append({"features": features, "desired_idx": action, "source": "recent_parent_current_agreement"})
            TRAINING_BUDGET.checkpoint("candidate_recent_agreement", thread_name=conservative._BUDGET_THREAD_NAME)
    finally:
        tracker.close()
    return samples


def _margin(policy, sample):
    chosen, _, arms, _, _, _ = policy.predict(sample["features"])
    means = {int(row["index"]): float(row["mean"]) for row in arms}
    desired = sample["desired_idx"]
    return int(chosen["index"]), means[desired] - means[1 - desired]


def optimize(policy, corrections, protected):
    """Warm-start gradient steps; commit only if corrections and retention both fit."""
    import numpy as np

    originals = {h: copy.deepcopy(head.state_classifier.export()) for h, head in policy.heads.items()}
    rows = list(corrections) + list(protected)
    masses = np.array([.5 / max(1, len(corrections))] * len(corrections)
                      + [.5 / max(1, len(protected))] * len(protected), dtype=np.float64)
    labels = np.array([row["desired_idx"] for row in rows], dtype=np.float64)
    learners = []
    for head in policy.heads.values():
        clf = head.state_classifier
        matrix = np.zeros((len(rows), clf.dims), dtype=np.float64)
        for i, row in enumerate(rows):
            for index, value in row["features"].items():
                if 0 < int(index) < clf.dims:
                    matrix[i, int(index)] = value
        full_original = np.array([clf.weights] + clf.hinge_weights)
        varying = (np.ptp(matrix, axis=0) > 1e-10) | np.any(full_original != 0., axis=0)
        varying[0] = False  # The intercept is clf.bias, never four hidden bias columns.
        z = np.clip((matrix[:, varying] - np.array(clf.center)[varying])
                    / np.array(clf.scale)[varying], -6, 6)
        design = np.concatenate([z] + [np.clip(z - knot, 0, 1) for knot in (-1, 0, 1)], axis=1)
        original = full_original[:, varying].reshape(-1)
        learners.append((clf, design, varying, original, original.copy(), clf.bias, clf.bias))
    for step in range(MAX_STEPS):
        updated = []
        for clf, design, varying, original, weights, original_bias, bias in learners:
            logits = np.clip(design @ weights + bias, -30, 30)
            error = (1. / (1. + np.exp(-logits)) - labels) * masses
            weights = weights - .2 * (design.T @ error + .02 * (weights - original))
            bias = bias - .2 * (float(error.sum()) + .02 * (bias - original_bias))
            expanded = np.zeros((4, clf.dims))
            expanded[:, varying] = weights.reshape(4, int(varying.sum()))
            clf.weights = expanded[0].tolist()
            clf.hinge_weights = expanded[1:].tolist()
            clf.bias = float(bias)
            clf._refresh_active()
            updated.append((clf, design, varying, original, weights, original_bias, bias))
        learners = updated
        if step % 4 == 0 and (all(_margin(policy, row)[1] >= TARGET_MARGIN for row in corrections)
                and all(_margin(policy, row)[0] == row["desired_idx"] for row in protected)):
            # Retain only newly unresolved examples for subsequent training. Do not
            # call add(): it would refit from zero and discard this accepted update.
            for head in policy.heads.values():
                clf = head.state_classifier
                for row in corrections:
                    action = row["desired_idx"]
                    clf.seen[action] += 1
                    record = {"x": {int(k): float(v) for k, v in row["features"].items()
                                     if 0 < int(k) < clf.dims}, "weight": 1., "id": clf.seen[action]}
                    clf.recent[action].append(record)
                clf.fits += 1
            return True, step + 1
        if step % 8 == 0:
            TRAINING_BUDGET.checkpoint("candidate_classifier_finetune", thread_name=conservative._BUDGET_THREAD_NAME)
    from binary_state_classifier import BinaryStateClassifier
    for horizon, raw in originals.items():
        policy.heads[horizon].state_classifier = BinaryStateClassifier(policy.dims, raw)
    return False, MAX_STEPS


def fine_tune(manager, candidate, policy):
    import agent_candidate_balanced_correct as balanced
    service = manager.engine.rl_teaching
    usable = []
    for label in conservative._teach_rows(service, candidate):
        features = service._label_context(candidate, policy, label["sample_ts"])
        if features is None:
            continue
        desired = conservative._nearest_action(policy, label["desired"])
        chosen = conservative._prediction_index(policy, features)[0]
        usable.append({"label": label, "features": features, "desired_idx": desired,
                       "chosen_idx": chosen, "was_correct": chosen == desired})
    errors = [row for row in usable if _margin(policy, row)[1] < TARGET_MARGIN]
    historical = balanced._anchor_pool(manager, candidate, policy, [row["label"]["sample_ts"] for row in usable])
    anchors, balance = balanced._select_balancing_anchors(manager, candidate, policy, usable, pool=historical)
    for row in anchors:
        row["desired_idx"] = row["action_idx"]
    # Already fitted explicit labels are retention constraints, not new optimizer data.
    diverse = []
    for action in (0, 1):
        bucket = [row for row in historical if row["action_idx"] == action]
        n = min(len(bucket), MAX_PROTECTED_PER_CLASS)
        diverse.extend({**bucket[(i * len(bucket)) // n], "desired_idx": action} for i in range(n))
    recent = recent_agreement(manager, candidate, policy, [row["label"]["sample_ts"] for row in usable])
    protected = [row for row in usable if row not in errors] + anchors + diverse + recent
    base_revision = policy.model_revision
    before = sum(row["was_correct"] for row in usable)
    accepted, steps = (True, 0) if not errors else optimize(policy, errors, protected)
    if accepted and errors:
        policy.model_revision = str(uuid.uuid4())
        for row in errors:
            manager.store.add_feedback(candidate["id"], row["desired_idx"], policy.actions[row["desired_idx"]],
                                       1., "Candidate Correct incremental classifier update", row["features"],
                                       "teach-ui", source="candidate_correct")
    manager.store.save_model(candidate["id"], policy.serialize())
    manager.engine.models[candidate["id"]] = policy
    after = balanced._fit_count(policy, usable)
    unresolved = [row for row in usable if _margin(policy, row)[1] < TARGET_MARGIN]
    return {
        "mode": "conservative_snapshot_finetune", "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
        "classifier_incremental": True, "classifier_update_accepted": accepted, "classifier_optimizer_steps": steps,
        "classifier_unresolved_updates": len(errors), "classifier_already_fitted_labels": len(usable) - len(errors),
        "classifier_recent_stability_samples": len(recent), "classifier_protected_samples": len(protected),
        "labels_applied": len(usable), "teach_fit_before_count": before, "teach_fit_after_count": after,
        "teach_fit_total": len(usable), "teach_fit_before": before / len(usable) if usable else None,
        "teach_fit_after": after / len(usable) if usable else None,
        "correction_class_counts": balance["correction_class_counts"],
        "stability_anchor_class_counts": balance["anchor_class_counts"], "stability_anchor_total": len(anchors),
        "stability_anchor_retained": balanced._fit_count(policy, anchors), "stability_anchor_shortfall": balance["shortfall"],
        "class_balance_required": balance.get("required", False), "schema_changed": False,
        "base_model_revision": base_revision, "candidate_model_revision": policy.model_revision,
        "hard_fit_target_total": len(usable), "hard_fit_before_count": before, "hard_fit_after_count": after,
        "hard_correction_satisfied": not unresolved, "hard_margin_target": TARGET_MARGIN,
        "hard_margin_after_min": min((_margin(policy, row)[1] for row in usable), default=None),
        "hard_unresolved_supervision_ids": [row["label"].get("supervision_event_id") or str(row["label"]["id"]) for row in unresolved],
        "hard_repair_rounds": 0, "hard_repair_updates": 0,
        "hard_repair_stop_reason": "incremental_constraints_satisfied" if accepted else "parent_restored_after_unstable_update",
    }


def comparison_collapse(comparison):
    """Require repeated loss of one observed class with correct opposite outputs."""
    rows = list((comparison.get("per_action") or {}).values())
    if len(rows) != 2:
        return False
    for missing, opposite in (rows, list(reversed(rows))):
        n = int(missing.get("samples") or 0)
        other_n = int(opposite.get("samples") or 0)
        if (n >= 8 and int(missing.get("live_correct") or 0) == n
                and int(missing.get("candidate_correct") or 0) == 0
                and other_n >= 8 and int(opposite.get("candidate_correct") or 0) == other_n):
            return True
    return False


def recover_collapsed_correct(manager):
    """Requeue only unapproved legacy Corrects with observed binary class collapse."""
    import correct_schema_evolution as evolution
    with manager.store.conn() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM agent_candidates WHERE state='comparing' AND discard_requested=0")]
    recovered = 0
    for row in rows:
        gate = evolution._json(row.get("offline_gate_json"), {})
        comparison = evolution._json(row.get("comparison_json"), {})
        if (row.get("reason") not in conservative._CORRECT_REASONS or not gate.get("passed")
                or gate.get("correct_optimizer") != "pairwise_desired_plus_wrong_arm_penalty"
                or not comparison_collapse(comparison)):
            continue
        parent = manager.store.get_model(row["parent_agent_id"])
        if not evolution._stable_model_schema_compatible(parent):
            continue
        if manager.store.get_model(row["candidate_id"]) is None:
            continue
        queue = manager._queue()
        status = queue.status_for(row["candidate_id"]) if queue is not None else None
        if status and status.get("state") in ("active", "building", "queued"):
            continue
        # Invalidate the old pass before resolving a snapshot: a collapsed child
        # must never be chosen as the latest offline-safe correction base.
        gate.update(passed=False, status="failed", classifier_collapse_recovery=True)
        gate["reasons"] = list(gate.get("reasons") or []) + ["Observed binary class collapse after legacy Correct"]
        now = time.time()
        with manager.lock, manager.store.lock, manager.store.conn() as db:
            changed = db.execute(
                """UPDATE agent_candidates SET state='queued',reason='feedback',dirty=1,
                   queued_ts=?,build_started_ts=NULL,build_finished_ts=NULL,
                   comparison_started_ts=NULL,comparison_json='{}',offline_gate_json=?,
                   last_error=NULL,updated_ts=? WHERE candidate_id=? AND state='comparing'
                   AND discard_requested=0""", (now, json.dumps(gate), now, row["candidate_id"])).rowcount
        if not changed:
            continue
        from agent_candidate_lineage import _refresh_generation_metadata
        _refresh_generation_metadata(manager.store, row["candidate_id"], lifecycle_state="queued", comparison_json="{}")
        manager.runtime.pop(str(row["parent_agent_id"]), None)
        manager.engine.models.pop(str(row["candidate_id"]), None)
        manager.store.event(row["parent_agent_id"], "warning", "candidate_correct_class_collapse_requeued",
                            "Legacy Correct lost an observed state class; requeued from a stable parent",
                            {"candidate_id": row["candidate_id"], "previous_comparison": comparison,
                             "previous_optimizer": gate["correct_optimizer"]})
        recovered += 1
    if recovered:
        manager.wake_event.set()
    return recovered
