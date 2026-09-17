"""Explicit, instance-owned Candidate promotion validation contract.

Stage 16 keeps the public ``promotable`` boolean for compatibility, but it is no longer
an authoritative opaque decision in the final runtime.  The source of truth is an ordered
list of named validation results.  Existing named gates are projected into that list and
legacy Candidate summaries without named gates are decomposed into equivalent checks.

The merger is deliberately monotonic: a later layer may add evidence or another veto, but
it cannot turn an earlier failed validation with the same name into a pass.  Custom user
promotion may waive only validations whose existing ``custom_override`` contract permits
it; ``never`` vetoes stay effective.
"""
from __future__ import annotations

import math
from collections import OrderedDict


CONTRACT_VERSION = 1
STRICTNESS = {"custom_evidence": 0, "explicit_offline": 1, "never": 2}


def _finite(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def named_result(name, passed, reason, *, custom_override="never", observed=None,
                 source="unknown"):
    return {
        "name": str(name),
        "passed": bool(passed),
        "effective_passed": bool(passed),
        "reason": str(reason or ("passed" if passed else "validation failed")),
        "custom_override": str(custom_override or "never"),
        "observed": dict(observed or {}),
        "source": str(source or "unknown"),
    }


def _stricter_override(a, b):
    a = str(a or "never")
    b = str(b or "never")
    return a if STRICTNESS.get(a, 2) >= STRICTNESS.get(b, 2) else b


def merge_named_results(*groups):
    """Merge validations without allowing module order to erase a veto."""
    merged = OrderedDict()
    for group in groups:
        for raw in group or ():
            item = dict(raw or {})
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            current = merged.get(name)
            if current is None:
                current = named_result(
                    name,
                    item.get("passed"),
                    item.get("reason"),
                    custom_override=item.get("custom_override"),
                    observed=item.get("observed"),
                    source=item.get("source"),
                )
                merged[name] = current
                continue
            # Monotonic safety: duplicate validators compose with AND, never last-write-wins.
            current["passed"] = bool(current.get("passed")) and bool(item.get("passed"))
            current["effective_passed"] = current["passed"]
            current["custom_override"] = _stricter_override(
                current.get("custom_override"), item.get("custom_override")
            )
            reasons = [str(current.get("reason") or ""), str(item.get("reason") or "")]
            current["reason"] = " | ".join(x for i, x in enumerate(reasons) if x and x not in reasons[:i])
            observed = dict(current.get("observed") or {})
            observed.update(dict(item.get("observed") or {}))
            current["observed"] = observed
            sources = [x for x in str(current.get("source") or "").split("+") if x]
            source = str(item.get("source") or "unknown")
            if source not in sources:
                sources.append(source)
            current["source"] = "+".join(sources)
    return list(merged.values())


def gates_to_results(gates, *, source="promotion_gates"):
    out = []
    for name, raw in (gates or {}).items():
        gate = dict(raw or {})
        out.append(named_result(
            name,
            gate.get("passed"),
            gate.get("reason"),
            custom_override=gate.get("custom_override") or "never",
            observed=gate.get("observed"),
            source=source,
        ))
    return out


def _legacy_results(manager, row, parent, candidate, summary):
    """Decompose the pre-named-gate Candidate bool without changing its thresholds."""
    samples = int(summary.get("samples") or 0)
    required = int(summary.get("required_future_samples") or 40)
    fresh = bool(summary.get("fresh_feedback_revision"))
    model = None
    if candidate and candidate.get("id"):
        model = manager.store.get_model(candidate["id"])
    trained = bool(candidate and candidate.get("training_state") == "qualified" and model)
    per_action = bool(summary.get("per_action_ready", True))
    coverage = bool(samples >= required and per_action)
    live_acc = _finite(summary.get("live_accuracy"))
    cand_acc = _finite(summary.get("candidate_accuracy"))
    quality = bool(live_acc is not None and cand_acc is not None and cand_acc + 0.03 >= live_acc)
    margin = max(2, int(math.ceil(max(0, samples) * 0.10)))
    cand_false = int(summary.get("candidate_false_early") or 0)
    live_false = int(summary.get("live_false_early") or 0)
    false_early = cand_false <= live_false + margin
    return [
        named_result(
            "data_freshness", fresh,
            "feedback/build revision is current" if fresh else "feedback/build revision is stale",
            observed={"feedback_revision": row.get("feedback_revision"),
                      "build_revision": row.get("build_revision"), "dirty": bool(row.get("dirty"))},
            source="legacy_candidate_summary",
        ),
        named_result(
            "action_coverage", coverage,
            f"future evidence {samples}/{required}; per-action coverage {'ready' if per_action else 'insufficient'}",
            custom_override="custom_evidence",
            observed={"samples": samples, "required": required, "per_action_ready": per_action},
            source="legacy_candidate_summary",
        ),
        named_result(
            "quality_regression", quality,
            "Candidate accuracy is within the legacy 3 pp tolerance" if quality
            else "Candidate accuracy exceeds the legacy regression tolerance",
            custom_override="custom_evidence",
            observed={"live_accuracy": live_acc, "candidate_accuracy": cand_acc},
            source="legacy_candidate_summary",
        ),
        named_result(
            "false_early", false_early,
            f"false-early {cand_false} <= Live {live_false} + margin {margin}" if false_early
            else f"false-early veto: Candidate {cand_false} > Live {live_false} + margin {margin}",
            observed={"candidate_false_early": cand_false, "live_false_early": live_false,
                      "margin": margin},
            source="legacy_candidate_summary",
        ),
        named_result(
            "execution_prerequisites", trained,
            "qualified persisted Candidate model is available" if trained
            else "Candidate is not qualified or its model is unavailable",
            observed={"training_state": (candidate or {}).get("training_state"),
                      "model_present": bool(model)},
            source="legacy_candidate_summary",
        ),
    ]


class PromotionValidationService:
    """Per-Candidate-manager validation composition; no module-global mutable state."""

    def __init__(self, manager, *, clock, repository):
        self.manager = manager
        self.clock = clock
        self.repository = repository
        self.contract_version = CONTRACT_VERSION

    def decorate_summary(self, row, parent, candidate, summary):
        out = dict(summary or {})
        existing = list(out.get("promotion_validations") or [])
        gates = gates_to_results(out.get("promotion_gates") or {}, source="named_gate_projection")
        if gates:
            validations = merge_named_results(existing, gates)
        else:
            validations = merge_named_results(
                existing, _legacy_results(self.manager, row, parent, candidate, out)
            )

        override_active = bool(out.get("user_promotion_override"))
        for item in validations:
            policy = str(item.get("custom_override") or "never")
            item["effective_passed"] = bool(item.get("passed")) or bool(
                override_active and policy != "never"
            )
            item["waived_by_explicit_custom_promotion"] = bool(
                not item.get("passed") and item.get("effective_passed")
            )

        # Preserve an unexplained upstream veto rather than accidentally weakening it.
        upstream = out.get("promotable")
        if upstream is False and all(bool(x.get("effective_passed")) for x in validations):
            if not override_active:
                validations = merge_named_results(validations, [named_result(
                    "upstream_compatibility_veto", False,
                    "An upstream compatibility layer vetoed promotion without a named gate",
                    source="compatibility_projection",
                )])

        vetoes = [
            {
                "gate": item["name"],
                "reason": item["reason"],
                "custom_override": item["custom_override"],
                "source": item.get("source"),
            }
            for item in validations if not bool(item.get("effective_passed"))
        ]
        out["promotion_validations"] = validations
        out["promotion_validation_contract"] = {
            "version": CONTRACT_VERSION,
            "source_of_truth": "ordered_named_validation_results",
            "compatibility_projection": "promotable_and_promotion_gates",
            "merge_rule": "duplicate_names_compose_with_and_later_modules_cannot_remove_veto",
            "custom_override": "only_existing_non_never_policies_may_be_waived",
        }
        out["promotion_vetoes"] = vetoes
        out["promotion_veto_reasons"] = [x["reason"] for x in vetoes]
        out["promotable"] = not vetoes
        return out

    def results_for(self, parent_ref):
        status = self.manager.status(parent_ref)
        if not status:
            return []
        comparison = dict(status.get("comparison") or {})
        return list(comparison.get("promotion_validations") or [])

    def descriptor(self):
        return {
            "version": CONTRACT_VERSION,
            "clock_injected": True,
            "repository_injected": True,
            "mutable_module_globals": False,
            "authoritative_result": "promotion_validations[]",
        }


def install(manager, *, clock, repository=None):
    existing = getattr(manager, "promotion_validation_service", None)
    if existing is not None:
        return manager
    service = PromotionValidationService(
        manager, clock=clock, repository=repository if repository is not None else manager.store
    )
    original_summary = manager._comparison_summary

    def comparison_summary(row, parent=None, candidate=None):
        base = original_summary(row, parent, candidate)
        parent_agent = parent or manager.store.get_agent_config(row.get("parent_agent_id"))
        candidate_agent = candidate or manager.store.get_agent(row.get("candidate_id"))
        return service.decorate_summary(row, parent_agent, candidate_agent, base)

    manager._comparison_summary = comparison_summary
    manager.promotion_validation_service = service
    manager.promotion_validation_results = service.results_for
    manager.promotion_validation_contract = service.descriptor()
    return manager
