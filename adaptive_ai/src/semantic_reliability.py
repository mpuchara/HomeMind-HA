"""Correct-driven semantic reliability for local presence evidence.

The model is deliberately small and bounded. Explicit Correct feedback supplies the only
supervision. A local source first has to show a repeatable relationship to the desired
binary action. Reliability context (humidity, temperature, or local-sensor disagreement)
may then *down-weight* that source only when cross-validated evidence shows that the source
is less trustworthy in a particular context.

No reliability context is presence evidence by itself. Profiles never boost a source above
its unconditioned quality, and lack of support is a neutral factor of 1.0.
"""
from __future__ import annotations

import copy
import math
import threading

VERSION = 1
SYNTHETIC_DISAGREEMENT = "__local_disagreement__"
MAX_EVENTS_PER_AREA = 128

LOCAL_SOURCE_ROLES = {
    "pir", "radar_occupancy", "occupancy_binary",
    "radar_activity", "auxiliary", "auxiliary_probability",
}
MIN_SOURCE_SAMPLES = 8
MIN_SOURCE_CLASS = 3
MIN_SOURCE_CV = 0.55
MIN_CONTEXT_SAMPLES = 8
MIN_CONTEXT_CLASS = 2
MIN_CONTEXT_CV = 0.60
MIN_CONTEXT_EFFECT = 0.12


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clamp(value, lo=0.0, hi=1.0):
    return max(float(lo), min(float(hi), float(value)))


def _label(value):
    value = _finite(value)
    if value is None:
        return None
    if value <= .25:
        return 0
    if value >= .75:
        return 1
    return None


def _balanced_accuracy(rows, predictor):
    by_class = {0: [0, 0], 1: [0, 0]}
    for row in rows:
        y = int(row["label"])
        pred = int(predictor(float(row["value"])))
        by_class[y][1] += 1
        by_class[y][0] += int(pred == y)
    recalls = [
        float(correct) / total
        for correct, total in by_class.values()
        if total > 0
    ]
    return sum(recalls) / len(recalls) if recalls else 0.0


def _fit_threshold(rows):
    rows = [
        {"value": _finite(row.get("value")), "label": int(row.get("label") or 0)}
        for row in rows
        if _finite(row.get("value")) is not None and int(row.get("label") or 0) in (0, 1)
    ]
    labels = {row["label"] for row in rows}
    if len(rows) < 2 or labels != {0, 1}:
        return None
    values = sorted(set(float(row["value"]) for row in rows))
    if len(values) < 2:
        return None
    thresholds = [(a + b) / 2.0 for a, b in zip(values[:-1], values[1:])]
    best = None
    for threshold in thresholds:
        for high_class in (0, 1):
            predictor = (
                lambda value, t=threshold, h=high_class:
                h if float(value) >= float(t) else 1 - h
            )
            balanced = _balanced_accuracy(rows, predictor)
            accuracy = sum(
                int(predictor(row["value"]) == row["label"]) for row in rows
            ) / len(rows)
            candidate = (
                float(balanced), float(accuracy),
                -abs(float(threshold) - .5), int(high_class), float(threshold),
            )
            if best is None or candidate > best[0]:
                best = (candidate, {
                    "threshold": float(threshold),
                    "high_class": int(high_class),
                    "balanced_accuracy": float(balanced),
                    "accuracy": float(accuracy),
                })
    return None if best is None else best[1]


def _stratified_folds(rows, count=4):
    count = max(2, min(int(count), len(rows)))
    folds = [[] for _ in range(count)]
    by_class = {0: [], 1: []}
    for idx, row in enumerate(rows):
        by_class[int(row["label"])].append(idx)
    for indexes in by_class.values():
        for pos, idx in enumerate(indexes):
            folds[pos % count].append(idx)
    return [sorted(fold) for fold in folds if fold]


def _cross_validated_predictions(rows, folds=4):
    rows = list(rows or [])
    if len(rows) < 4:
        return {}, 0.0, 0.0
    predictions = {}
    fold_scores = []
    for test_indexes in _stratified_folds(rows, folds):
        test_set = set(test_indexes)
        train = [row for idx, row in enumerate(rows) if idx not in test_set]
        test = [rows[idx] for idx in test_indexes]
        fit = _fit_threshold(train)
        if fit is None:
            continue
        predictor = (
            lambda value, p=fit:
            p["high_class"] if float(value) >= p["threshold"] else 1 - p["high_class"]
        )
        if {int(row["label"]) for row in test} == {0, 1}:
            fold_scores.append(_balanced_accuracy(test, predictor))
        for idx in test_indexes:
            predictions[idx] = int(predictor(rows[idx]["value"]))
    coverage = len(predictions) / max(1, len(rows))
    score = sum(fold_scores) / len(fold_scores) if fold_scores else 0.0
    return predictions, float(score), float(coverage)


def _smoothed_rate(correct, total):
    # Beta(2,2) keeps tiny explicit-feedback sets conservative.
    return (float(correct) + 2.0) / (float(total) + 4.0)


def _side_stats(rows, fit):
    out = {
        "low": {"support": 0, "correct": 0},
        "high": {"support": 0, "correct": 0},
    }
    threshold = float(fit["threshold"])
    for row in rows:
        side = "high" if float(row["value"]) >= threshold else "low"
        out[side]["support"] += 1
        out[side]["correct"] += int(row["label"])
    for side in out.values():
        side["rate"] = _smoothed_rate(side["correct"], side["support"])
    return out


def _source_disagreement(event, source_id):
    local = dict(event.get("local") or {})
    source = local.get(source_id)
    if not isinstance(source, dict):
        return None
    value = _finite(source.get("value"))
    if value is None:
        return None
    others = []
    for entity_id, item in local.items():
        if entity_id == source_id or not isinstance(item, dict):
            continue
        other = _finite(item.get("value"))
        if other is not None:
            others.append(abs(value - other))
    return max(others) if others else None


def _profile_area(events):
    profiles = {}
    source_ids = sorted({
        source_id
        for event in events
        for source_id in (event.get("local") or {})
    })
    for source_id in source_ids:
        source_rows = []
        source_events = []
        source_role = None
        for event in events:
            item = (event.get("local") or {}).get(source_id)
            if not isinstance(item, dict):
                continue
            value = _finite(item.get("value"))
            desired = int(event.get("desired") or 0)
            if value is None or desired not in (0, 1):
                continue
            source_rows.append({
                "value": value,
                "label": desired,
                "ts": float(event.get("ts") or 0.0),
            })
            source_events.append(event)
            source_role = source_role or item.get("source_role")

        class_counts = {
            0: sum(1 for row in source_rows if row["label"] == 0),
            1: sum(1 for row in source_rows if row["label"] == 1),
        }
        if (
            len(source_rows) < MIN_SOURCE_SAMPLES
            or min(class_counts.values()) < MIN_SOURCE_CLASS
        ):
            continue
        source_fit = _fit_threshold(source_rows)
        source_oof, source_cv, source_coverage = _cross_validated_predictions(source_rows)
        if (
            source_fit is None
            or source_cv < MIN_SOURCE_CV
            or source_coverage < .70
        ):
            continue

        correctness = []
        for idx, pred in sorted(source_oof.items()):
            event = source_events[idx]
            correctness.append({
                "event": event,
                "correct": int(pred == int(source_rows[idx]["label"])),
            })
        base_support = len(correctness)
        base_correct = sum(row["correct"] for row in correctness)
        if base_support < MIN_CONTEXT_SAMPLES:
            continue
        base_rate = _smoothed_rate(base_correct, base_support)

        context_ids = sorted({
            context_id
            for row in correctness
            for context_id in (row["event"].get("context") or {})
        })
        context_ids.append(SYNTHETIC_DISAGREEMENT)
        candidates = []
        for context_id in context_ids:
            context_rows = []
            for row in correctness:
                event = row["event"]
                if context_id == SYNTHETIC_DISAGREEMENT:
                    value = _source_disagreement(event, source_id)
                else:
                    value = _finite((event.get("context") or {}).get(context_id))
                if value is None:
                    continue
                context_rows.append({
                    "value": value,
                    "label": int(row["correct"]),
                    "ts": float(event.get("ts") or 0.0),
                })
            correctness_counts = {
                0: sum(1 for row in context_rows if row["label"] == 0),
                1: sum(1 for row in context_rows if row["label"] == 1),
            }
            if (
                len(context_rows) < MIN_CONTEXT_SAMPLES
                or min(correctness_counts.values()) < MIN_CONTEXT_CLASS
            ):
                continue
            fit = _fit_threshold(context_rows)
            _predictions, cv_score, coverage = _cross_validated_predictions(context_rows)
            if fit is None or cv_score < MIN_CONTEXT_CV or coverage < .70:
                continue
            sides = _side_stats(context_rows, fit)
            if min(sides["low"]["support"], sides["high"]["support"]) < 2:
                continue
            effect = abs(float(sides["high"]["rate"]) - float(sides["low"]["rate"]))
            if effect < MIN_CONTEXT_EFFECT:
                continue
            support_factor = min(1.0, len(context_rows) / 20.0)
            score = max(0.0, cv_score - .5) * effect * coverage * support_factor
            candidates.append({
                "context_id": context_id,
                "threshold": float(fit["threshold"]),
                "high_class": int(fit["high_class"]),
                "cv_balanced_accuracy": float(cv_score),
                "coverage": float(coverage),
                "effect_size": float(effect),
                "support": len(context_rows),
                "correctness_counts": correctness_counts,
                "sides": sides,
                "score": float(score),
            })
        candidates.sort(
            key=lambda row: (
                -float(row["score"]),
                -float(row["cv_balanced_accuracy"]),
                -float(row["effect_size"]),
                str(row["context_id"]),
            )
        )
        selected = candidates[0] if candidates else None
        profiles[source_id] = {
            "version": VERSION,
            "source_id": source_id,
            "source_role": source_role,
            "support": len(source_rows),
            "desired_counts": class_counts,
            "source_threshold": float(source_fit["threshold"]),
            "source_high_class": int(source_fit["high_class"]),
            "source_cv_balanced_accuracy": float(source_cv),
            "source_cv_coverage": float(source_coverage),
            "base_correct": int(base_correct),
            "base_support": int(base_support),
            "base_correct_rate": float(base_rate),
            "reliability_context": selected,
            "candidate_contexts": candidates[:5],
        }
    return profiles


class SemanticReliabilityModel:
    """Bounded per-area source reliability learned only from explicit Correct."""

    def __init__(self, raw=None):
        self.lock = threading.RLock()
        self.revision = 0
        self.feedback = {}
        raw = dict(raw or {})
        if int(raw.get("version") or 0) == VERSION:
            for area, rows in (raw.get("feedback") or {}).items():
                clean = []
                seen = set()
                for row in list(rows or [])[-MAX_EVENTS_PER_AREA:]:
                    row = copy.deepcopy(dict(row or {}))
                    event_id = str(row.get("event_id") or "")
                    if not event_id or event_id in seen:
                        continue
                    seen.add(event_id)
                    desired = _label(row.get("desired"))
                    if desired is None:
                        continue
                    row["desired"] = desired
                    clean.append(row)
                if clean:
                    self.feedback[str(area)] = clean
        self.profiles = {
            area: _profile_area(rows)
            for area, rows in self.feedback.items()
        }

    @staticmethod
    def _compact_snapshot(snapshot):
        local = {}
        context = {}
        for entity_id, item in (snapshot or {}).items():
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            value = _finite(item.get("normalized_value"))
            if value is None:
                continue
            value = _clamp(value)
            if role == "LOCAL_EVIDENCE":
                source_role = str(item.get("source_role") or "")
                if source_role not in LOCAL_SOURCE_ROLES:
                    continue
                local[str(entity_id)] = {
                    "value": value,
                    "source_role": source_role,
                }
            elif role == "RELIABILITY_CONTEXT":
                context[str(entity_id)] = value
        return local, context

    def record_feedback(self, area, desired, snapshot, event_id, ts):
        desired = _label(desired)
        area = str(area or "")
        event_id = str(event_id or "")
        if desired is None or not area or not event_id:
            return {
                "recorded": False,
                "reason": "binary_area_supervision_required",
                "version": VERSION,
            }
        local, context = self._compact_snapshot(snapshot)
        if not local:
            return {
                "recorded": False,
                "reason": "no_local_presence_evidence",
                "version": VERSION,
            }
        with self.lock:
            rows = self.feedback.setdefault(area, [])
            if any(str(row.get("event_id") or "") == event_id for row in rows):
                return {
                    "recorded": False,
                    "reason": "duplicate_supervision_event",
                    "version": VERSION,
                    "area_id": area,
                }
            rows.append({
                "event_id": event_id,
                "ts": float(ts),
                "desired": int(desired),
                "local": local,
                "context": context,
            })
            if len(rows) > MAX_EVENTS_PER_AREA:
                del rows[:-MAX_EVENTS_PER_AREA]
            self.profiles[area] = _profile_area(rows)
            self.revision += 1
            return {
                "recorded": True,
                "version": VERSION,
                "area_id": area,
                "events": len(rows),
                "profiled_sources": len(self.profiles.get(area) or {}),
                "reliability_context_entities": sorted(context),
                "local_sources": sorted(local),
            }

    @staticmethod
    def _fresh(source, ts, freshness_fn):
        if not source or not source.get("available"):
            return False
        if freshness_fn is None:
            return True
        try:
            return float(freshness_fn(source, ts)) >= .25
        except Exception:
            return False

    def evaluate(self, area, source_id, sources, ts, freshness_fn=None):
        area = str(area or "")
        source_id = str(source_id or "")
        with self.lock:
            profile = copy.deepcopy((self.profiles.get(area) or {}).get(source_id))
        if not profile:
            return {
                "factor": 1.0,
                "calibrated": False,
                "reason": "no_correct_driven_profile",
                "source_id": source_id,
                "area_id": area,
            }
        context = dict(profile.get("reliability_context") or {})
        if not context:
            return {
                "factor": 1.0,
                "calibrated": False,
                "reason": "no_supported_reliability_context",
                "source_id": source_id,
                "area_id": area,
                "support": profile.get("support"),
                "source_cv_balanced_accuracy": profile.get("source_cv_balanced_accuracy"),
            }

        context_id = str(context.get("context_id") or "")
        value = None
        context_fresh = True
        if context_id == SYNTHETIC_DISAGREEMENT:
            source = dict((sources or {}).get(source_id) or {})
            if self._fresh(source, ts, freshness_fn):
                source_value = _finite(source.get("value"))
                distances = []
                if source_value is not None:
                    for other_id, other in (sources or {}).items():
                        if str(other_id) == source_id or not isinstance(other, dict):
                            continue
                        if str(other.get("role") or "") not in LOCAL_SOURCE_ROLES:
                            continue
                        if not self._fresh(other, ts, freshness_fn):
                            continue
                        other_value = _finite(other.get("value"))
                        if other_value is not None:
                            distances.append(abs(source_value - other_value))
                value = max(distances) if distances else None
        else:
            row = dict((sources or {}).get(context_id) or {})
            context_fresh = self._fresh(row, ts, freshness_fn)
            if context_fresh:
                value = _finite(row.get("value"))

        if value is None or not context_fresh:
            return {
                "factor": 1.0,
                "calibrated": True,
                "applied": False,
                "reason": "reliability_context_unavailable",
                "source_id": source_id,
                "area_id": area,
                "context_id": context_id,
                "support": profile.get("support"),
            }

        side_name = "high" if float(value) >= float(context["threshold"]) else "low"
        side = dict((context.get("sides") or {}).get(side_name) or {})
        side_rate = _finite(side.get("rate"))
        base_rate = _finite(profile.get("base_correct_rate"))
        if side_rate is None or base_rate is None or base_rate <= 1e-9:
            factor = 1.0
            evidence = 0.0
        else:
            raw_factor = _clamp(side_rate / base_rate, .50, 1.0)
            cv_strength = _clamp(
                (float(context.get("cv_balanced_accuracy") or .5) - .5) / .25
            )
            support_strength = min(1.0, float(profile.get("base_support") or 0) / 20.0)
            side_strength = min(1.0, float(side.get("support") or 0) / 8.0)
            evidence = cv_strength * support_strength * side_strength
            factor = 1.0 - evidence * (1.0 - raw_factor)
            factor = _clamp(factor, .50, 1.0)
        return {
            "factor": float(factor),
            "calibrated": True,
            "applied": bool(factor < .999999),
            "reason": (
                "correct_driven_context_downweight"
                if factor < .999999 else
                "context_does_not_reduce_trust"
            ),
            "source_id": source_id,
            "area_id": area,
            "context_id": context_id,
            "context_value": float(value),
            "context_side": side_name,
            "base_correct_rate": base_rate,
            "context_correct_rate": side_rate,
            "evidence_strength": float(evidence),
            "source_support": int(profile.get("support") or 0),
            "context_support": int(context.get("support") or 0),
            "side_support": int(side.get("support") or 0),
            "source_cv_balanced_accuracy": profile.get("source_cv_balanced_accuracy"),
            "context_cv_balanced_accuracy": context.get("cv_balanced_accuracy"),
            "effect_size": context.get("effect_size"),
        }

    def export(self):
        with self.lock:
            return {
                "version": VERSION,
                "feedback": copy.deepcopy(self.feedback),
                "profiles": copy.deepcopy(self.profiles),
                "revision": int(self.revision),
                "contract": (
                    "explicit_correct_only_context_conditioned_downweight_no_presence_from_reliability"
                ),
            }

    def diagnostics(self):
        with self.lock:
            return {
                "version": VERSION,
                "revision": int(self.revision),
                "areas": len(self.feedback),
                "events": sum(len(rows) for rows in self.feedback.values()),
                "profiled_sources": sum(len(rows) for rows in self.profiles.values()),
                "profiles": copy.deepcopy(self.profiles),
                "contract": (
                    "humidity_environment_and_disagreement_can_only_modulate_supported_local_evidence"
                ),
            }
