"""Stage-3 residual-targeted Correct schema evolution.

Stage 1 made explicit supervision and broad historical context durable. Stage 2 fixed the
optimizer so a strong wrong arm can actually be crossed. Stage 3 is entered only when a
bounded Stage-2 repair still leaves explicit supervision unresolved.

The contract is deliberately conservative:
* binary Correct only;
* rank context against the remaining residual supervision, not generic whole-home
  correlation;
* keep controllable-target/device and electrical-unit exclusions from Stage 1;
* preserve semantic evidence roles;
* fast presence-driven targets may promote LOCAL, BOUNDARY or TRAJECTORY evidence, never
  humidity/temperature RELIABILITY context as occupancy proof;
* add at most two entities per build and never exceed the normal schema capacity;
* schema changes create a migrated challenger model. Existing feature/head statistics are
  remapped by semantic feature label, so adding an entity cannot reinterpret old weights;
* calibration evidence is reset after a schema change;
* if broad context cannot provide a sufficiently supported discriminator, report
  missing_context instead of repeating the same model training.

All work runs inside Candidate build/fine-tune. Nothing is added to event->intent inference,
ActionIntent or Executor.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import copy
import json
import math
import time
import uuid

import agent_candidate_conservative_correct as conservative
import correct_data_foundation as foundation
import correct_margin_repair as margin
from context import ExplicitFeatureSchema, archived_state, context_scalar, is_fast_reactive_agent
from settings import OPTIONS


_PATCHED = False
_BASE_MARGIN_FINE_TUNE = margin._margin_correct_fine_tune
_BASE_MARGIN_GATE = margin._margin_offline_gate

_MAX_ADDITIONS = 2
_MIN_ROWS = 8
_MIN_CLASS_ROWS = 3
_MIN_COVERAGE = 0.60
_MIN_RESIDUAL_COVERAGE = 0.70
_MIN_CV_BALANCED_ACCURACY = 0.62
_MIN_RESIDUAL_ACCURACY = 0.60


def _json(raw, default=None):
    if isinstance(raw, dict):
        return dict(raw)
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


def _event_id(label):
    value = (label or {}).get("supervision_event_id")
    if value:
        return str(value)
    return foundation.supervision_event_id(
        (label or {}).get("fingerprint"),
        (label or {}).get("sample_ts"),
        (label or {}).get("desired"),
    )


def _schema_limit(policy, candidate):
    by_dims = max(1, (int(policy.dims) - 16) // 4)
    if is_fast_reactive_agent(candidate):
        return min(by_dims, max(2, int(OPTIONS.get("fast_max_context_entities", 8) or 8)))
    return min(by_dims, max(4, int(OPTIONS.get("max_context_entities", 28) or 28)))


def _requested_entity_allowed(candidate, entity_id):
    requested = set(candidate.get("input_entities") or ["*"])
    return "*" in requested or str(entity_id) in requested


def _automatic_role_allowed(candidate, role):
    role = str(role or foundation.ROLE_OTHER)
    if is_fast_reactive_agent(candidate):
        return role in {
            foundation.ROLE_LOCAL,
            foundation.ROLE_BOUNDARY,
            foundation.ROLE_TRAJECTORY,
        }
    return role in {
        foundation.ROLE_LOCAL,
        foundation.ROLE_BOUNDARY,
        foundation.ROLE_TRAJECTORY,
        foundation.ROLE_RELIABILITY,
    }


def _manual_snapshots(store, agent_id, event_ids):
    if not event_ids:
        return {}
    with store.conn() as c:
        if not foundation._table_exists(c, "manual_context_feedback"):
            return {}
        cols = foundation._table_columns(c, "manual_context_feedback")
        if "supervision_event_id" not in cols:
            return {}
        marks = ",".join("?" for _ in event_ids)
        rows = c.execute(
            f"""SELECT * FROM manual_context_feedback
                WHERE agent_id=? AND supervision_event_id IN ({marks})
                ORDER BY created_ts,id""",
            (str(agent_id), *sorted(event_ids)),
        ).fetchall()
    latest = {}
    for raw in rows:
        row = dict(raw)
        event_id = str(row.get("supervision_event_id") or "")
        if event_id:
            latest[event_id] = _json(row.get("snapshot_json"), {})
    return latest


def _historical_fallback_snapshots(core, candidate, labels):
    """Bulk as-of reconstruction for pre-0.14.63 Correct facts."""
    labels = list(labels or ())
    if not labels:
        return {}
    entity_ids, _states, registry = foundation._eligible_entities(core.ENGINE, candidate)
    entity_ids = [
        entity_id for entity_id in entity_ids
        if _requested_entity_allowed(candidate, entity_id)
    ]
    if not entity_ids:
        return {}

    sample_times = sorted(
        {
            float(label["sample_ts"])
            for label in labels
            if label.get("sample_ts") is not None
        }
    )
    if not sample_times:
        return {}
    minimum, maximum = sample_times[0], sample_times[-1]
    out = {
        _event_id(label): {}
        for label in labels
        if label.get("sample_ts") is not None
    }

    with core.STORE.conn() as c:
        for entity_id in entity_ids:
            predecessor = c.execute(
                """SELECT * FROM entity_history
                   WHERE entity_id=? AND ts<?
                   ORDER BY ts DESC,id DESC LIMIT 1""",
                (str(entity_id), float(minimum)),
            ).fetchone()
            rows = []
            if predecessor:
                rows.append(dict(predecessor))
            rows.extend(
                dict(row)
                for row in c.execute(
                    """SELECT * FROM entity_history
                       WHERE entity_id=? AND ts>=? AND ts<=?
                       ORDER BY ts,id""",
                    (str(entity_id), float(minimum), float(maximum)),
                ).fetchall()
            )
            if not rows:
                continue
            times = [float(row["ts"]) for row in rows]
            for label in labels:
                sample_ts = float(label["sample_ts"])
                pos = bisect_right(times, sample_ts) - 1
                if pos < 0:
                    continue
                state = archived_state(rows[pos])
                value = _finite(context_scalar(entity_id, state, candidate))
                if value is None:
                    continue
                previous_value = None
                if pos > 0:
                    previous_state = archived_state(rows[pos - 1])
                    previous_value = _finite(context_scalar(entity_id, previous_state, candidate))
                role = foundation.semantic_source_role(
                    core.ENGINE, candidate, entity_id, state, registry
                )
                event_id = _event_id(label)
                out.setdefault(event_id, {})[entity_id] = {
                    "v": value,
                    "age": max(0.0, sample_ts - float(rows[pos]["ts"])),
                    "role": role,
                    "area_id": core.ENGINE.context.area_for(entity_id),
                    "source_role": (
                        core.ENGINE.context.evidence_metadata(entity_id) or {}
                    ).get("role"),
                    "recent_delta": None if previous_value is None else value - previous_value,
                    "history_source": rows[pos].get("source"),
                }
    return out


def _supervision_rows(core, manager, candidate, policy, unresolved_ids):
    service = getattr(manager.engine, "rl_teaching", None)
    if service is None:
        return [], {"durable": 0, "historical_fallback": 0, "missing": 0}
    labels = conservative._teach_rows(service, candidate)
    event_ids = {_event_id(label) for label in labels}
    durable = _manual_snapshots(core.STORE, candidate["id"], event_ids)
    missing_labels = [label for label in labels if _event_id(label) not in durable]
    fallback = _historical_fallback_snapshots(core, candidate, missing_labels)
    unresolved_set = set(unresolved_ids or ())
    rows = []
    for label in labels:
        event_id = _event_id(label)
        desired_idx = conservative._nearest_action(policy, float(label["desired"]))
        snapshot = durable.get(event_id) or fallback.get(event_id) or {}
        rows.append({
            "supervision_event_id": event_id,
            "sample_ts": float(label["sample_ts"]),
            "desired_idx": int(desired_idx),
            "unresolved": event_id in unresolved_set,
            "snapshot": snapshot,
        })
    return rows, {
        "durable": sum(1 for row in rows if row["supervision_event_id"] in durable),
        "historical_fallback": sum(1 for row in rows if row["supervision_event_id"] in fallback),
        "missing": sum(1 for row in rows if not row["snapshot"]),
    }


def _weighted_balanced_accuracy(rows, predictor):
    by_class = {}
    for row in rows:
        weight = 3.0 if row.get("unresolved") else 1.0
        bucket = by_class.setdefault(int(row["desired_idx"]), [0.0, 0.0])
        bucket[1] += weight
        if int(predictor(float(row["x"]))) == int(row["desired_idx"]):
            bucket[0] += weight
    recalls = [correct / total for correct, total in by_class.values() if total > 0]
    return sum(recalls) / len(recalls) if len(recalls) >= 2 else 0.0


def _best_stump(rows):
    rows = list(rows or ())
    classes = {int(row["desired_idx"]) for row in rows}
    if len(classes) != 2:
        return None
    values = sorted({float(row["x"]) for row in rows})
    if not values:
        return None
    thresholds = [values[0] - 1e-9, values[-1] + 1e-9]
    thresholds.extend(
        (left + right) / 2.0
        for left, right in zip(values, values[1:])
        if right > left
    )
    best = None
    for threshold in thresholds:
        for high_class in sorted(classes):
            low_class = next(value for value in classes if value != high_class)
            predictor = (
                lambda x, t=threshold, hi=high_class, lo=low_class:
                hi if x >= t else lo
            )
            score = _weighted_balanced_accuracy(rows, predictor)
            candidate = (score, -abs(float(threshold)), int(high_class), float(threshold))
            if best is None or candidate > best[0]:
                best = (candidate, predictor)
    if best is None:
        return None
    candidate, predictor = best
    return {
        "balanced_accuracy": float(candidate[0]),
        "threshold": float(candidate[3]),
        "high_class": int(candidate[2]),
        "predictor": predictor,
    }


def _cross_validated_stump(rows):
    ordered = sorted(rows, key=lambda row: (
        float(row.get("sample_ts") or 0.0),
        str(row.get("supervision_event_id") or ""),
    ))
    if len(ordered) < _MIN_ROWS:
        return None
    folds = [ordered[index::3] for index in range(3)]
    fold_scores = []
    residual_hits = residual_total = 0
    for fold_index, test in enumerate(folds):
        train = [
            row
            for index, fold in enumerate(folds)
            if index != fold_index
            for row in fold
        ]
        if not test or len({row["desired_idx"] for row in train}) < 2:
            continue
        fit = _best_stump(train)
        if not fit:
            continue
        predictor = fit["predictor"]
        fold_scores.append(_weighted_balanced_accuracy(test, predictor))
        for row in test:
            if row.get("unresolved"):
                residual_total += 1
                residual_hits += int(
                    int(predictor(float(row["x"]))) == int(row["desired_idx"])
                )
    if not fold_scores:
        return None
    full = _best_stump(ordered)
    return {
        "cv_balanced_accuracy": sum(fold_scores) / len(fold_scores),
        "cv_folds": len(fold_scores),
        "residual_accuracy": residual_hits / residual_total if residual_total else 0.0,
        "threshold": full.get("threshold") if full else None,
        "high_class": full.get("high_class") if full else None,
    }


def rank_residual_context(rows, current_entities, candidate):
    """Rank entity-level context using residual-weighted cross-validation."""
    rows = list(rows or ())
    current = set(current_entities or ())
    total = len(rows)
    unresolved_total = sum(1 for row in rows if row.get("unresolved"))
    entities = set()
    for row in rows:
        entities.update((row.get("snapshot") or {}).keys())

    ranking = []
    rejected = []
    for entity_id in sorted(entities):
        if entity_id in current or not _requested_entity_allowed(candidate, entity_id):
            continue
        role_counts = Counter()
        for row in rows:
            item = (row.get("snapshot") or {}).get(entity_id)
            if not item:
                continue
            role_counts[str(item.get("role") or foundation.ROLE_OTHER)] += 1
        role = role_counts.most_common(1)[0][0] if role_counts else foundation.ROLE_OTHER
        if not _automatic_role_allowed(candidate, role):
            rejected.append({
                "entity_id": entity_id,
                "role": role,
                "reason": "semantic_role_not_eligible_for_automatic_schema_promotion",
            })
            continue

        best = None
        for channel in ("v", "recent_delta"):
            usable = []
            for row in rows:
                item = (row.get("snapshot") or {}).get(entity_id)
                if not item:
                    continue
                value = _finite(item.get(channel))
                if value is None:
                    continue
                usable.append({**row, "x": value})
            class_counts = Counter(int(row["desired_idx"]) for row in usable)
            coverage = len(usable) / total if total else 0.0
            residual_rows = [row for row in usable if row.get("unresolved")]
            residual_coverage = (
                len(residual_rows) / unresolved_total if unresolved_total else 0.0
            )
            if (
                len(usable) < _MIN_ROWS
                or len(class_counts) < 2
                or min(class_counts.values()) < _MIN_CLASS_ROWS
                or coverage < _MIN_COVERAGE
                or residual_coverage < _MIN_RESIDUAL_COVERAGE
            ):
                continue
            cv = _cross_validated_stump(usable)
            if not cv:
                continue
            role_bonus = {
                foundation.ROLE_LOCAL: 0.05,
                foundation.ROLE_BOUNDARY: 0.035,
                foundation.ROLE_TRAJECTORY: 0.015,
                foundation.ROLE_RELIABILITY: 0.0,
            }.get(role, 0.0)
            composite = min(
                1.0,
                0.68 * float(cv["cv_balanced_accuracy"])
                + 0.20 * float(cv["residual_accuracy"])
                + 0.07 * float(coverage)
                + role_bonus,
            )
            item = {
                "entity_id": entity_id,
                "role": role,
                "channel": channel,
                "score": composite,
                "cv_balanced_accuracy": float(cv["cv_balanced_accuracy"]),
                "residual_accuracy": float(cv["residual_accuracy"]),
                "coverage": float(coverage),
                "residual_coverage": float(residual_coverage),
                "samples": len(usable),
                "class_counts": {
                    str(key): int(value) for key, value in sorted(class_counts.items())
                },
                "threshold": cv.get("threshold"),
                "high_class": cv.get("high_class"),
            }
            if best is None or (
                item["score"], item["cv_balanced_accuracy"],
                item["residual_accuracy"], item["channel"] == "v"
            ) > (
                best["score"], best["cv_balanced_accuracy"],
                best["residual_accuracy"], best["channel"] == "v"
            ):
                best = item

        if best is None:
            rejected.append({
                "entity_id": entity_id,
                "role": role,
                "reason": "insufficient_class_coverage_or_cross_validated_signal",
            })
            continue
        if (
            best["cv_balanced_accuracy"] < _MIN_CV_BALANCED_ACCURACY
            or best["residual_accuracy"] < _MIN_RESIDUAL_ACCURACY
        ):
            rejected.append({**best, "reason": "below_residual_promotion_threshold"})
            continue
        ranking.append(best)

    ranking.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["cv_balanced_accuracy"]),
            -float(item["residual_accuracy"]),
            str(item["entity_id"]),
        )
    )
    return ranking, rejected


def _feature_index_map(schema):
    out = {}
    for index, labels in schema.labels().items():
        for label in labels or ():
            out[str(label)] = int(index)
    return out


def migrate_model_schema(raw_model, new_entities, evolution_meta=None):
    """Remap head dimensions by semantic feature label; never reinterpret an old index."""
    raw = copy.deepcopy(raw_model or {})
    dims = int(raw.get("dims") or (raw.get("schema") or {}).get("dims") or 128)
    old_schema = ExplicitFeatureSchema.from_export(raw.get("schema"), dims)
    if old_schema is None:
        raise ValueError("Stable correction base schema is incompatible")
    new_schema = ExplicitFeatureSchema(dims, list(new_entities or ()))
    old_map = _feature_index_map(old_schema)
    new_map = _feature_index_map(new_schema)
    mapping = {
        int(old_index): int(new_map[label])
        for label, old_index in old_map.items()
        if label in new_map
    }
    for index in range(max(0, dims - 7), dims):
        mapping[index] = index

    heads = {}
    for horizon, original in (raw.get("heads") or {}).items():
        head = copy.deepcopy(original or {})
        action_count = len(raw.get("actions") or [])
        if action_count <= 0:
            action_count = len(head.get("a") or [])
        old_a_all = head.get("a") or [[] for _ in range(action_count)]
        old_b_all = head.get("b") or [[] for _ in range(action_count)]
        old_sum_all = head.get("ctx_sum") or [[] for _ in range(action_count)]
        old_sq_all = head.get("ctx_sq") or [[] for _ in range(action_count)]
        new_a = [[1.0] * dims for _ in range(action_count)]
        new_b = [[0.0] * dims for _ in range(action_count)]
        new_sum = [[0.0] * dims for _ in range(action_count)]
        new_sq = [[0.0] * dims for _ in range(action_count)]
        for arm in range(action_count):
            old_a = old_a_all[arm] if arm < len(old_a_all) else []
            old_b = old_b_all[arm] if arm < len(old_b_all) else []
            old_sum = old_sum_all[arm] if arm < len(old_sum_all) else []
            old_sq = old_sq_all[arm] if arm < len(old_sq_all) else []
            for old_index, new_index in mapping.items():
                if old_index < len(old_a):
                    new_a[arm][new_index] = old_a[old_index]
                if old_index < len(old_b):
                    new_b[arm][new_index] = old_b[old_index]
                if old_index < len(old_sum):
                    new_sum[arm][new_index] = old_sum[old_index]
                if old_index < len(old_sq):
                    new_sq[arm][new_index] = old_sq[old_index]
        head["a"] = new_a
        head["b"] = new_b
        head["ctx_sum"] = new_sum
        head["ctx_sq"] = new_sq
        head["validation_weight"] = 0.0
        head["validation_correct_weight"] = 0.0
        head["validation_samples"] = 0.0
        head["validation_pred_weight"] = [0.0] * action_count
        head["validation_pred_correct_weight"] = [0.0] * action_count
        heads[str(horizon)] = head

    revision = str(uuid.uuid4())
    raw["schema"] = new_schema.export()
    raw["heads"] = heads
    raw["model_revision"] = revision
    raw["tournament_revision"] = revision
    selection = dict(raw.get("selection_meta") or {})
    selection["schema_evolution"] = dict(evolution_meta or {})
    selection["schema_evolution"]["old_entities"] = list(old_schema.entities)
    selection["schema_evolution"]["new_entities"] = list(new_schema.entities)
    selection["schema_evolution"]["migrated_feature_dimensions"] = len(mapping)
    raw["selection_meta"] = selection
    return raw


def _feedback_watermark(store, candidate_id):
    with store.conn() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(id),0) FROM rl_feedback WHERE agent_id=?",
            (str(candidate_id),),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def _discard_probe_feedback(store, candidate_id, watermark):
    with store.lock, store.conn() as c:
        c.execute(
            "DELETE FROM rl_feedback WHERE agent_id=? AND id>?",
            (str(candidate_id), int(watermark)),
        )


def _schema_fine_tune(core, manager, candidate):
    """Run Stage 2 once; evolve only when its bounded repair remains unresolved."""
    initial_model = manager.store.get_model(candidate["id"])
    feedback_watermark = _feedback_watermark(manager.store, candidate["id"])
    report = dict(_BASE_MARGIN_FINE_TUNE(manager, candidate) or {})
    unresolved = list(report.get("hard_unresolved_supervision_ids") or [])
    report.setdefault("schema_evolution_status", "not_needed")
    report.setdefault("schema_changed", False)
    if not unresolved:
        report["schema_evolution_reason"] = "stage2_resolved_all_supervision"
        return report

    policy = manager.engine.models.get(candidate["id"])
    if policy is None:
        manager.engine.models.pop(candidate["id"], None)
        policy = manager.engine.policy(candidate)
    if len(policy.actions) != 2:
        report["schema_evolution_status"] = "not_applicable_nonbinary"
        report["schema_evolution_reason"] = "automatic_residual_schema_evolution_is_binary_only"
        return report
    if not initial_model:
        report["schema_evolution_status"] = "missing_context"
        report["schema_evolution_reason"] = "stable_base_model_missing"
        return report

    current_entities = list(policy.schema.entities)
    limit = _schema_limit(policy, candidate)
    capacity = max(0, limit - len(current_entities))
    if capacity <= 0:
        report["schema_evolution_status"] = "missing_context"
        report["schema_evolution_reason"] = "schema_capacity_exhausted"
        report["schema_capacity"] = int(limit)
        return report

    rows, snapshot_sources = _supervision_rows(core, manager, candidate, policy, unresolved)
    ranking, rejected = rank_residual_context(rows, current_entities, candidate)
    additions = ranking[:min(_MAX_ADDITIONS, capacity)]
    report["schema_evolution_snapshot_sources"] = snapshot_sources
    report["schema_evolution_ranked_context"] = ranking[:12]
    report["schema_evolution_rejected_context"] = rejected[:12]
    report["schema_evolution_residual_before"] = len(unresolved)
    report["schema_before_entities"] = current_entities
    report["schema_capacity"] = int(limit)

    if not additions:
        report["schema_evolution_status"] = "missing_context"
        report["schema_evolution_reason"] = (
            "no_semantically_eligible_cross_validated_context_separates_residual_supervision"
        )
        return report

    new_entities = current_entities + [
        item["entity_id"] for item in additions
        if item["entity_id"] not in current_entities
    ]
    evolution_meta = {
        "contract": "residual_targeted_cross_validated_context",
        "selected": additions,
        "residual_before": len(unresolved),
        "created_ts": time.time(),
    }

    _discard_probe_feedback(manager.store, candidate["id"], feedback_watermark)
    challenger = migrate_model_schema(initial_model, new_entities, evolution_meta=evolution_meta)
    manager.store.save_model(candidate["id"], challenger)
    manager.engine.models.pop(candidate["id"], None)

    final = dict(_BASE_MARGIN_FINE_TUNE(manager, candidate) or {})
    final_unresolved = list(final.get("hard_unresolved_supervision_ids") or [])
    final.update({
        "schema_changed": True,
        "schema_evolution_status": (
            "enriched" if len(final_unresolved) < len(unresolved)
            else "challenger_unresolved"
        ),
        "schema_evolution_reason": (
            "residual_count_improved"
            if len(final_unresolved) < len(unresolved)
            else "selected_context_did_not_reduce_residual_count"
        ),
        "schema_evolution_selected": additions,
        "schema_evolution_ranked_context": ranking[:12],
        "schema_evolution_rejected_context": rejected[:12],
        "schema_evolution_snapshot_sources": snapshot_sources,
        "schema_evolution_residual_before": len(unresolved),
        "schema_evolution_residual_after": len(final_unresolved),
        "schema_before_entities": current_entities,
        "schema_after_entities": new_entities,
        "schema_capacity": int(limit),
    })
    manager.store.event(
        candidate["id"],
        "info" if len(final_unresolved) < len(unresolved) else "warning",
        "candidate_correct_schema_challenger",
        (
            "Residual Correct context expanded Candidate schema"
            if len(final_unresolved) < len(unresolved)
            else "Residual schema challenger did not reduce unresolved Correct supervision"
        ),
        {
            "selected": additions,
            "residual_before": len(unresolved),
            "residual_after": len(final_unresolved),
            "schema_before": current_entities,
            "schema_after": new_entities,
        },
    )
    return final


def _schema_offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
    report = dict(teach_report or {})
    gate = dict(_BASE_MARGIN_GATE(parent, parent_stats, candidate_stats, report) or {})
    for key in (
        "schema_evolution_status",
        "schema_evolution_reason",
        "schema_evolution_selected",
        "schema_evolution_ranked_context",
        "schema_evolution_snapshot_sources",
        "schema_evolution_residual_before",
        "schema_evolution_residual_after",
        "schema_before_entities",
        "schema_after_entities",
        "schema_capacity",
    ):
        if key in report:
            gate[key] = report[key]

    status = str(report.get("schema_evolution_status") or "")
    if status == "missing_context":
        gate["passed"] = False
        gate["status"] = "missing_context"
        reasons = list(gate.get("reasons") or [])
        reason = "Correct residuals require additional discriminative context"
        if reason not in reasons:
            reasons.append(reason)
        gate["reasons"] = reasons
        gate["missing_context"] = True
    else:
        gate["missing_context"] = False
    return gate


def install(core, manager):
    global _PATCHED
    if getattr(manager, "_correct_schema_evolution_installed", False):
        return manager
    if not getattr(manager, "_correct_margin_repair_installed", False):
        manager = margin.install(core, manager)

    if not _PATCHED:
        conservative._conservative_fine_tune = (
            lambda manager_obj, candidate: _schema_fine_tune(core, manager_obj, candidate)
        )
        conservative._offline_gate = _schema_offline_gate
        _PATCHED = True

    original_status = manager.status

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        row = manager._candidate_row(parent_id)
        gate = _json((row or {}).get("offline_gate_json"), {})
        result["schema_evolution_status"] = gate.get("schema_evolution_status")
        result["schema_evolution_reason"] = gate.get("schema_evolution_reason")
        result["schema_evolution_selected"] = list(gate.get("schema_evolution_selected") or [])
        result["missing_context"] = bool(gate.get("missing_context"))
        return result

    manager.status = status
    manager._correct_schema_evolution_installed = True
    manager.correct_schema_evolution_contract = (
        "unresolved_correct_residuals_rank_broad_context_then_bounded_schema_challenger_or_missing_context"
    )
    core.STORE.event(
        None,
        "info",
        "correct_schema_evolution_ready",
        "Residual-targeted Candidate schema evolution is active",
        {
            "max_additions": _MAX_ADDITIONS,
            "binary_only": True,
            "fast_roles": [
                foundation.ROLE_LOCAL,
                foundation.ROLE_BOUNDARY,
                foundation.ROLE_TRAJECTORY,
            ],
            "reliability_is_not_presence_proof": True,
            "hot_path": False,
        },
    )
    return manager
