"""Preference-aware Candidate metrics and named promotion gates for fast targets.

The Candidate Shadow runtime records future transition evidence and explicit corrections.
For fast binary devices the physical automation is a baseline, not ground truth, so the
preference metric may replace legacy evidence-count thresholds. It must not erase
independent safety vetoes such as false-early behaviour, stale feedback/configuration, or
missing execution prerequisites.
"""
import math
from urllib.parse import urlsplit

from agent_candidate_config_guard import config_signature
from fast_runtime import is_fast_target
from settings import OPTIONS


DEFAULT_ON_WINDOW_SECONDS = 8.0
DEFAULT_OFF_WINDOW_SECONDS = 120.0
DEFAULT_HALF_LIFE_OPPORTUNITIES = 20.0
DEFAULT_MIN_OPPORTUNITIES = 12
DEFAULT_MIN_PER_ACTION = 4
DEFAULT_MIN_PREFERENCE_CONFIDENCE = 0.65
DEFAULT_MAX_ACCURACY_REGRESSION = 0.03
DEFAULT_CORRECTION_PENALTY = 2.0
DECISION_STALE_SECONDS = 95.0

CUSTOM_OVERRIDE_NEVER = "never"
CUSTOM_OVERRIDE_EVIDENCE = "custom_evidence"
CUSTOM_OVERRIDE_OFFLINE = "explicit_offline"


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _option_float(name, default, minimum=None):
    value = _finite(OPTIONS.get(name, default), default)
    return max(float(minimum), value) if minimum is not None else value


def _option_int(name, default, minimum=None):
    try:
        value = int(OPTIONS.get(name, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), value) if minimum is not None else value


def _binary(value):
    return 1.0 if _finite(value) >= 0.5 else 0.0


def _false_early_details(summary):
    explicit = summary.get("false_early_safety_passed")
    try:
        samples = int(summary.get("samples") or summary.get("meaningful_opportunities") or 0)
    except (TypeError, ValueError):
        samples = 0
    margin = max(2, int(math.ceil(max(0, samples) * 0.10)))
    try:
        candidate_false_early = int(summary.get("candidate_false_early") or 0)
        live_false_early = int(summary.get("live_false_early") or 0)
    except (TypeError, ValueError):
        return False, margin, None, None
    derived = candidate_false_early <= live_false_early + margin
    return (bool(explicit) if explicit is not None else derived), margin, candidate_false_early, live_false_early


def _false_early_safety(summary):
    """Compatibility helper used by regression probes/tests."""
    return _false_early_details(summary)[0]


def _gate(passed, reason, custom_override, observed=None):
    return {
        "passed": bool(passed),
        "reason": str(reason),
        "custom_override": str(custom_override),
        "observed": observed or {},
    }


def _promotion_gate_report(manager, row, parent, candidate, summary):
    """Build the authoritative standard fast-target promotion decision.

    Legacy sample-count thresholds are intentionally not inherited as one opaque boolean.
    The preference contract owns its own evidence coverage while independent safety gates
    remain visible and non-removable by later metric composition.
    """
    model = manager.store.get_model(candidate["id"]) if candidate and candidate.get("id") else None
    row_state = str(row.get("state") or "comparing")
    fresh = bool(summary.get("fresh_feedback_revision"))
    config_matches = bool(parent and candidate and config_signature(parent) == config_signature(candidate))
    model_version = model.get("version") if isinstance(model, dict) else None
    version_config_ok = bool(config_matches and model_version is not None)
    execution_ok = bool(
        candidate
        and candidate.get("training_state") == "qualified"
        and model
        and row_state not in {"queued", "building", "exploring", "failed", "discarding"}
    )

    count = int(summary.get("meaningful_opportunities") or 0)
    required = int(summary.get("required_future_samples") or DEFAULT_MIN_OPPORTUNITIES)
    per_action_ready = bool(summary.get("per_action_ready"))
    coverage_ok = bool(count >= required and per_action_ready)

    regression_ok = bool(summary.get("accuracy_safety_passed"))
    false_ok, false_margin, candidate_false, live_false = _false_early_details(summary)
    corrections_ok = bool(summary.get("teach_anchor_passed")) and bool(summary.get("no_new_corrections"))
    timing_ok = bool(summary.get("timing_safety_passed"))
    enough_preference = float(summary.get("preference_confidence") or 0.0) >= float(
        summary.get("preference_confidence_threshold") or DEFAULT_MIN_PREFERENCE_CONFIDENCE
    )
    offline_ok = bool(summary.get("offline_gate_passed"))

    gates = {
        "data_freshness": _gate(
            fresh,
            "feedback/build revision is current and Candidate is not dirty" if fresh
            else "feedback revision changed after build or Candidate is dirty",
            CUSTOM_OVERRIDE_NEVER,
            {"feedback_revision": row.get("feedback_revision"), "build_revision": row.get("build_revision"),
             "dirty": bool(row.get("dirty"))},
        ),
        "version_configuration": _gate(
            version_config_ok,
            "Candidate model is versioned and policy configuration matches its direct parent" if version_config_ok
            else "Candidate model version/configuration does not match the current direct parent",
            CUSTOM_OVERRIDE_NEVER,
            {"config_matches": config_matches, "model_version": model_version},
        ),
        "offline_gate": _gate(
            offline_ok,
            "offline gate passed" if offline_ok else "offline gate did not pass",
            CUSTOM_OVERRIDE_OFFLINE,
        ),
        "action_coverage": _gate(
            coverage_ok,
            f"future evidence {count}/{required}; per-action coverage {'ready' if per_action_ready else 'insufficient'}",
            CUSTOM_OVERRIDE_EVIDENCE,
            {"opportunities": count, "required": required, "per_action_ready": per_action_ready,
             "per_action": summary.get("fast_per_action_samples")},
        ),
        "quality_regression": _gate(
            regression_ok,
            "Candidate transition accuracy is within the allowed regression" if regression_ok
            else "Candidate transition accuracy exceeds the allowed regression",
            CUSTOM_OVERRIDE_EVIDENCE,
            {"parent_accuracy": summary.get("parent_transition_accuracy"),
             "candidate_accuracy": summary.get("candidate_transition_accuracy")},
        ),
        "false_early": _gate(
            false_ok,
            (
                f"false-early {candidate_false} <= Live {live_false} + margin {false_margin}"
                if false_ok else
                f"false-early veto: Candidate {candidate_false} > Live {live_false} + margin {false_margin}"
            ),
            CUSTOM_OVERRIDE_NEVER,
            {"candidate_false_early": candidate_false, "live_false_early": live_false,
             "margin": false_margin},
        ),
        "corrections": _gate(
            corrections_ok,
            "Teach anchors fit and no new explicit corrections exist" if corrections_ok
            else "Teach anchors do not fit or a new explicit correction exists",
            CUSTOM_OVERRIDE_NEVER,
            {"teach_anchor_passed": bool(summary.get("teach_anchor_passed")),
             "manual_corrections": int(summary.get("manual_corrections_since_generation") or 0)},
        ),
        "timing": _gate(
            timing_ok,
            "timing objective is within the standard safety tolerance" if timing_ok
            else "timing objective is below the standard safety tolerance",
            CUSTOM_OVERRIDE_EVIDENCE,
            {"timing_objective_gain": summary.get("timing_objective_gain")},
        ),
        "preference_evidence": _gate(
            enough_preference,
            "preference confidence reached the standard threshold" if enough_preference
            else "preference confidence is below the standard threshold",
            CUSTOM_OVERRIDE_EVIDENCE,
            {"confidence": summary.get("preference_confidence"),
             "threshold": summary.get("preference_confidence_threshold")},
        ),
        "execution_prerequisites": _gate(
            execution_ok,
            "qualified persisted Candidate model is ready for atomic promotion" if execution_ok
            else "Candidate is not in an executable qualified/model-ready lifecycle state",
            CUSTOM_OVERRIDE_NEVER,
            {"training_state": (candidate or {}).get("training_state"), "row_state": row_state,
             "model_present": bool(model)},
        ),
    }
    vetoes = [
        {"gate": name, "reason": gate["reason"], "custom_override": gate["custom_override"]}
        for name, gate in gates.items() if not gate["passed"]
    ]
    return gates, vetoes


def _window_for(outcome):
    if _binary(outcome) >= 0.5:
        return _option_float("fast_precursor_on_seconds", DEFAULT_ON_WINDOW_SECONDS, 1.0)
    return _option_float("fast_precursor_off_seconds", DEFAULT_OFF_WINDOW_SECONDS, 5.0)


def _timing_utility(correct, lead_seconds, window_seconds):
    """Fast-target utility in [-1, 1]. Wrong direction is a hard failure."""
    if not bool(correct):
        return -1.0
    lead = max(0.0, _finite(lead_seconds))
    window = max(0.25, _finite(window_seconds, 1.0))
    return max(0.0, min(1.0, lead / window))


def _wilson_lower(success_weight, failure_weight, z=1.0):
    success = max(0.0, _finite(success_weight))
    failure = max(0.0, _finite(failure_weight))
    total = success + failure
    if total <= 1e-12:
        return 0.0
    p = success / total
    z2 = float(z) * float(z)
    den = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    spread = float(z) * math.sqrt(max(0.0, p * (1.0 - p) / total + z2 / (4.0 * total * total)))
    return max(0.0, min(1.0, (centre - spread) / den))


def _opportunity_weight(index, total, half_life=None):
    half_life = max(1.0, _finite(
        half_life if half_life is not None else OPTIONS.get(
            "candidate_preference_half_life_opportunities", DEFAULT_HALF_LIFE_OPPORTUNITIES
        ),
        DEFAULT_HALF_LIFE_OPPORTUNITIES,
    ))
    age = max(0, int(total) - 1 - int(index))
    return 0.5 ** (float(age) / half_life)


def _preference_confidence(success_weight, failure_weight):
    success = max(0.0, _finite(success_weight))
    failure = max(0.0, _finite(failure_weight))
    evidence = success + failure
    if evidence <= 1e-12:
        return 0.0
    lower = _wilson_lower(success, failure, z=1.0)
    support_target = _option_float("candidate_preference_support_opportunities", 6.0, 1.0)
    support = 1.0 - math.exp(-evidence / support_target)
    return max(0.0, min(0.995, lower * support))


def _generation_for_candidate(store, candidate_id):
    with store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
            (str(candidate_id),),
        ).fetchone()
    return dict(row) if row else None


def _pair_rows(store, parent_generation_id, child_generation_id):
    with store.conn() as c:
        return [dict(row) for row in c.execute(
            """SELECT * FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=?
               ORDER BY outcome_ts""",
            (str(parent_generation_id), str(child_generation_id)),
        ).fetchall()]


def _observed_lead(store, generation_id, outcome, outcome_ts, window_seconds):
    outcome_ts = float(outcome_ts)
    window = max(1.0, float(window_seconds))
    start = outcome_ts - max(window, DECISION_STALE_SECONDS)
    with store.conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT ts,desired FROM candidate_generation_decisions
               WHERE generation_id=? AND ts<=? AND ts>=? ORDER BY ts DESC""",
            (str(generation_id), outcome_ts, start),
        ).fetchall()]
    if not rows or _binary(rows[0].get("desired")) != _binary(outcome):
        return None
    earliest = float(rows[0]["ts"])
    newer = outcome_ts
    for row in rows:
        ts = float(row["ts"])
        if newer - ts > DECISION_STALE_SECONDS:
            break
        if _binary(row.get("desired")) != _binary(outcome):
            break
        earliest = ts
        newer = ts
    return max(0.0, min(window, outcome_ts - earliest))


def _manual_corrections(store, generation, since_ts):
    agent_id = str(generation.get("agent_id") or "")
    if not agent_id:
        return []
    rows = []
    with store.conn() as c:
        tables = {str(r[0]) for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('teaching_rl_labels','teaching_labels')"
        ).fetchall()}
        for table in sorted(tables):
            try:
                query = (
                    f"SELECT created_ts,sample_ts,desired FROM {table} "
                    "WHERE agent_id=? AND undone_ts IS NULL AND created_ts>=? ORDER BY created_ts"
                )
                rows.extend({**dict(r), "source": table} for r in c.execute(query, (agent_id, float(since_ts))).fetchall())
            except Exception:
                continue
    unique = {}
    for row in rows:
        try:
            key = (round(float(row["sample_ts"]), 3), round(float(row["desired"]), 6))
        except (TypeError, ValueError, KeyError):
            continue
        previous = unique.get(key)
        if previous is None or float(row["created_ts"]) > float(previous["created_ts"]):
            unique[key] = row
    return sorted(unique.values(), key=lambda row: float(row["created_ts"]))


def _teach_anchor_status(candidate_status):
    total = int(candidate_status.get("teach_fit_total") or 0)
    after = candidate_status.get("teach_fit_after_count")
    if total <= 0:
        return {"total": 0, "fit": None, "passed": True}
    try:
        after = int(after)
    except (TypeError, ValueError):
        after = 0
    fit = float(after) / max(1, total)
    return {"total": total, "fit": fit, "passed": after >= total}


def _fast_metrics(manager, row, parent, candidate, base_summary, candidate_status=None):
    generation = _generation_for_candidate(manager.store, row.get("candidate_id"))
    if not generation or not generation.get("parent_generation_id"):
        return None
    parent_gid = str(generation["parent_generation_id"])
    child_gid = str(generation["generation_id"])
    pairs = _pair_rows(manager.store, parent_gid, child_gid)

    half_life = _option_float(
        "candidate_preference_half_life_opportunities", DEFAULT_HALF_LIFE_OPPORTUNITIES, 1.0
    )
    success_weight = 0.0
    failure_weight = 0.0
    parent_utility_sum = 0.0
    child_utility_sum = 0.0
    parent_correct = 0
    child_correct = 0
    per_action = {"0.0": 0, "1.0": 0}
    on_leads = []
    off_leads = []

    for index, pair in enumerate(pairs):
        outcome = _binary(pair.get("outcome"))
        key = str(outcome)
        per_action[key] = int(per_action.get(key, 0)) + 1
        p_ok = bool(pair.get("parent_correct"))
        c_ok = bool(pair.get("child_correct"))
        parent_correct += int(p_ok)
        child_correct += int(c_ok)
        window = _window_for(outcome)
        p_lead = _observed_lead(manager.store, parent_gid, outcome, pair["outcome_ts"], window)
        c_lead = _observed_lead(manager.store, child_gid, outcome, pair["outcome_ts"], window)
        if p_lead is None:
            p_lead = pair.get("parent_lead_seconds")
        if c_lead is None:
            c_lead = pair.get("child_lead_seconds")
        p_lead = max(0.0, _finite(p_lead))
        c_lead = max(0.0, _finite(c_lead))
        parent_utility_sum += _timing_utility(p_ok, p_lead, window)
        child_utility_sum += _timing_utility(c_ok, c_lead, window)
        if c_ok:
            success_weight += _opportunity_weight(index, len(pairs), half_life)
        else:
            failure_weight += _opportunity_weight(index, len(pairs), half_life)
        (on_leads if outcome >= 0.5 else off_leads).append((p_lead, c_lead))

    created_ts = float(generation.get("created_ts") or row.get("queued_ts") or 0.0)
    corrections = _manual_corrections(manager.store, generation, created_ts)
    correction_penalty = _option_float("candidate_preference_correction_penalty", DEFAULT_CORRECTION_PENALTY, 0.0)
    correction_weight = 0.0
    for correction in corrections:
        created = float(correction["created_ts"])
        later = sum(1 for pair in pairs if float(pair["outcome_ts"]) > created)
        correction_weight += correction_penalty * (0.5 ** (float(later) / half_life))
    failure_weight += correction_weight

    count = len(pairs)
    parent_accuracy = float(parent_correct) / count if count else None
    child_accuracy = float(child_correct) / count if count else None
    parent_utility = parent_utility_sum / count if count else None
    child_utility = child_utility_sum / count if count else None
    timing_gain = None if parent_utility is None or child_utility is None else child_utility - parent_utility
    preference = _preference_confidence(success_weight, failure_weight)

    status_for_anchor = candidate_status or {}
    anchor = _teach_anchor_status(status_for_anchor)
    min_samples = _option_int("candidate_fast_min_opportunities", DEFAULT_MIN_OPPORTUNITIES, 1)
    min_per_action = _option_int("candidate_fast_min_per_action", DEFAULT_MIN_PER_ACTION, 1)
    min_preference = _option_float(
        "candidate_fast_min_preference_confidence", DEFAULT_MIN_PREFERENCE_CONFIDENCE, 0.0
    )
    max_regression = _option_float(
        "agent_candidate_max_accuracy_regression", DEFAULT_MAX_ACCURACY_REGRESSION, 0.0
    )
    accuracy_safety = bool(
        child_accuracy is not None
        and parent_accuracy is not None
        and child_accuracy + max_regression >= parent_accuracy
    )
    timing_safety = bool(timing_gain is not None and timing_gain >= -0.02)
    count_ready = count >= min_samples
    per_action_ready = all(int(per_action.get(str(v), 0)) >= min_per_action for v in (0.0, 1.0))
    no_new_corrections = len(corrections) == 0

    return {
        "comparison_metric": "fast_timing_preference",
        "meaningful_opportunities": count,
        "required_future_samples": min_samples,
        "future_sample_count_ready": count_ready,
        "required_future_samples_per_action": min_per_action,
        "per_action_ready": per_action_ready,
        "fast_per_action_samples": per_action,
        "parent_transition_accuracy": parent_accuracy,
        "candidate_transition_accuracy": child_accuracy,
        "timing_parent_utility": parent_utility,
        "timing_candidate_utility": child_utility,
        "timing_objective_gain": timing_gain,
        "preference_confidence": preference,
        "preference_confidence_threshold": min_preference,
        "preference_success_weight": success_weight,
        "preference_failure_weight": failure_weight,
        "manual_corrections_since_generation": len(corrections),
        "manual_correction_weight": correction_weight,
        "corrections_per_100_opportunities": (100.0 * len(corrections) / count) if count else None,
        "teach_anchor_fit": anchor["fit"],
        "teach_anchor_total": anchor["total"],
        "teach_anchor_passed": anchor["passed"],
        "accuracy_safety_passed": accuracy_safety,
        "timing_safety_passed": timing_safety,
        "no_new_corrections": no_new_corrections,
        "fast_on_parent_lead_seconds": sum(x[0] for x in on_leads) / len(on_leads) if on_leads else None,
        "fast_on_candidate_lead_seconds": sum(x[1] for x in on_leads) / len(on_leads) if on_leads else None,
        "fast_off_parent_lead_seconds": sum(x[0] for x in off_leads) / len(off_leads) if off_leads else None,
        "fast_off_candidate_lead_seconds": sum(x[1] for x in off_leads) / len(off_leads) if off_leads else None,
    }


def install(manager):
    if getattr(manager, "_candidate_preference_metrics_installed", False):
        return manager

    original_summary = manager._comparison_summary
    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)
    handler = manager.core.Handler
    original_get = handler.do_GET
    original_static = handler.static

    def comparison_summary(row, parent=None, candidate=None):
        out = original_summary(row, parent, candidate)
        parent_agent = parent or manager.store.get_agent_config(row.get("parent_agent_id"))
        candidate_agent = candidate or manager.store.get_agent_config(row.get("candidate_id"))
        if not parent_agent or not is_fast_target(parent_agent) or str(parent_agent.get("target_property")) != "power":
            return out

        try:
            import json
            offline = json.loads(row.get("offline_gate_json") or "{}")
        except Exception:
            offline = {}
        anchor_status = {
            "teach_fit_total": offline.get("teach_fit_total"),
            "teach_fit_after_count": offline.get("teach_fit_after_count"),
        }
        metrics = _fast_metrics(manager, row, parent_agent, candidate_agent, out, anchor_status)
        if not metrics:
            return out
        out.update(metrics)
        if out.get("offline_gate_passed") is None:
            out["offline_gate_passed"] = bool(offline.get("passed"))

        false_ok, _, _, _ = _false_early_details(out)
        out["false_early_safety_passed"] = false_ok
        gates, vetoes = _promotion_gate_report(manager, row, parent_agent, candidate_agent, out)
        out["promotion_gates"] = gates
        out["promotion_vetoes"] = vetoes
        out["promotion_veto_reasons"] = [item["reason"] for item in vetoes]
        out["promotable"] = not vetoes
        return out

    manager._comparison_summary = comparison_summary

    def _decorate(result):
        if not result:
            return result
        result["model_confidence"] = result.get("candidate_confidence")
        row = None
        parent_id = result.get("parent_agent_id")
        if parent_id and callable(getattr(manager, "_candidate_row", None)):
            row = manager._candidate_row(parent_id)
        if row:
            parent = manager.store.get_agent_config(row.get("parent_agent_id"))
            candidate = manager.store.get_agent_config(row.get("candidate_id"))
            summary = manager._comparison_summary(row, parent, candidate)
            result["comparison"] = summary
            result["preference_confidence"] = summary.get("preference_confidence")
            result["preference_metric"] = summary.get("comparison_metric")
            result["meaningful_opportunities"] = summary.get("meaningful_opportunities")
            result["manual_corrections_since_generation"] = summary.get("manual_corrections_since_generation")
            result["promotion_gates"] = summary.get("promotion_gates") or {}
            result["promotion_vetoes"] = summary.get("promotion_vetoes") or []
            result["promotion_veto_reasons"] = summary.get("promotion_veto_reasons") or []
            result["promotable"] = bool(summary.get("promotable"))
            if result.get("state") == "comparing" and result["promotable"]:
                result["state"] = "ready"
        return result

    def status(parent_id):
        return _decorate(original_status(parent_id))

    def list_status():
        return [_decorate(dict(item)) for item in (original_list_status() or []) if item]

    def lineage_status(ref):
        result = original_lineage_status(ref) if original_lineage_status is not None else None
        return _decorate(result)

    def do_get(http):
        path = urlsplit(http.path).path
        if path == "/candidate_preference_ui.js":
            if not http.require_trusted_client():
                return
            return http.static("candidate_preference_ui.js", "application/javascript; charset=utf-8")
        return original_get(http)

    def static(http, name, content_type):
        if name == "index.html":
            path = manager.core.STATIC_DIR / name
            if path.exists():
                body = path.read_text(encoding="utf-8")
                marker = '<script src="candidate_preference_ui.js?v=0.14.6"></script>'
                if marker not in body:
                    body = body.replace('</body>', marker + '\n</body>')
                return http.send_bytes(200, body.encode("utf-8"), content_type)
        return original_static(http, name, content_type)

    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    handler.do_GET = do_get
    handler.static = static
    manager._candidate_preference_metrics_installed = True
    manager.candidate_preference_contract = (
        "named_fast_promotion_gates_preserve_hard_vetoes_and_expose_reasons"
    )
    manager.candidate_fast_metric = "timing_utility_with_transition_accuracy_safety"
    manager.candidate_preference_half_life_opportunities = _option_float(
        "candidate_preference_half_life_opportunities", DEFAULT_HALF_LIFE_OPPORTUNITIES, 1.0
    )
    return manager
