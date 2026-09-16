"""User-defined Candidate promotion criteria without weakening hard runtime safety.

The standard Candidate gates remain the default. This extension adds an explicit manual
promotion path where the user chooses how much paired future evidence is enough and may
explicitly override a failed/insufficient *offline* regression gate.

Hard invariants are still owned by the existing atomic promoter: the Candidate must have
a qualified model, Root/Candidate configuration must still match, Control promotion must
still pass Control qualification, the generation swap remains atomic, and a Candidate
never dispatches before the commit.

A failed offline gate is also advisory for passive Shadow observation only. That lets
future A/B evidence keep accumulating instead of freezing forever at zero samples. The
observation wrapper always resolves the active lineage leaf, including G2/G3 descendants;
it does not assume that the active edge is rooted directly at Live G0.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import threading
import time


_BLOCKED_STATES = {"offline_blocked", "insufficient_evidence"}
_PROMOTION_TLS = threading.local()
DEFAULT_CUSTOM_RULES = {
    "min_future_samples": 6,
    "min_per_binary_action": 2,
    "max_future_regression_pp": 15.0,
    "allow_offline_gate_override": False,
}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value, default, low=0, high=10000):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(low), min(int(high), value))


def _bounded_float_or_none(value, default=15.0, low=0.0, high=100.0):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(float(low), min(float(high), value))


def normalize_rules(raw):
    raw = dict(raw or {})
    return {
        "min_future_samples": _bounded_int(
            raw.get("min_future_samples"), DEFAULT_CUSTOM_RULES["min_future_samples"], 0, 10000
        ),
        "min_per_binary_action": _bounded_int(
            raw.get("min_per_binary_action"), DEFAULT_CUSTOM_RULES["min_per_binary_action"], 0, 5000
        ),
        "max_future_regression_pp": _bounded_float_or_none(
            raw.get("max_future_regression_pp"), DEFAULT_CUSTOM_RULES["max_future_regression_pp"], 0.0, 100.0
        ),
        "allow_offline_gate_override": _bool(
            raw.get("allow_offline_gate_override", DEFAULT_CUSTOM_RULES["allow_offline_gate_override"])
        ),
    }


def evaluate_custom_rules(status, raw_rules=None):
    """Return a transparent pass/fail report against the user's explicit criteria."""
    status = dict(status or {})
    rules = normalize_rules(raw_rules)
    comparison = dict(status.get("comparison") or {})
    gate = dict(status.get("offline_gate") or {})
    failures = []

    state = str(status.get("state") or "")
    training_state = str(status.get("training_state") or "")
    if training_state != "qualified":
        failures.append("Candidate model is not qualified yet")
    if state in {"queued", "building", "exploring", "failed", "discarding"}:
        failures.append(f"Candidate state {state or 'unknown'} cannot be promoted")

    gate_passed = bool(gate.get("passed"))
    if not gate_passed and not rules["allow_offline_gate_override"]:
        failures.append("offline gate did not pass (enable explicit offline-gate override to accept this risk)")

    samples = int(comparison.get("samples") or 0)
    if samples < rules["min_future_samples"]:
        failures.append(
            f"future samples {samples} < required {rules['min_future_samples']}"
        )

    binary = "required_future_samples_per_action" in comparison
    on_events = int(comparison.get("on_events") or 0)
    off_events = int(comparison.get("off_events") or 0)
    if binary and rules["min_per_binary_action"] > 0:
        need = int(rules["min_per_binary_action"])
        if on_events < need or off_events < need:
            failures.append(
                f"binary future evidence ON/OFF {on_events}/{off_events} < {need}/{need}"
            )

    max_regression_pp = rules["max_future_regression_pp"]
    gain = comparison.get("accuracy_gain")
    if max_regression_pp is not None and samples > 0:
        if gain is None:
            failures.append("future accuracy regression cannot be evaluated yet")
        elif float(gain) * 100.0 < -float(max_regression_pp) - 1e-12:
            failures.append(
                f"future accuracy regression {float(gain) * 100.0:.1f} pp exceeds allowed {-float(max_regression_pp):.1f} pp"
            )

    return {
        "passed": not failures,
        "rules": rules,
        "failures": failures,
        "observed": {
            "state": state,
            "training_state": training_state,
            "offline_gate_status": gate.get("status") or "pending",
            "offline_gate_passed": gate_passed,
            "future_samples": samples,
            "on_events": on_events,
            "off_events": off_events,
            "accuracy_gain": gain,
        },
    }


def _active_observation_row(manager, root_id):
    """Resolve the current leaf edge for a Root Live agent.

    `agent_candidates.parent_agent_id` is the direct parent of an edge. From G2 onward
    that parent is a hidden Candidate id, not the Root Live id. Looking up only
    `_candidate_row(root_id)` therefore returns the old G0->G1 parent edge and leaves a
    blocked G1->G2/G2->G3 leaf invisible to the observation override. The Shadow runtime
    then rejects the real leaf and `Future samples` stays at zero forever.
    """
    try:
        with manager.store.conn() as c:
            row = c.execute(
                """SELECT e.*
                   FROM agent_candidates e
                   JOIN agent_candidate_generations child ON child.agent_id=e.candidate_id
                   WHERE child.root_agent_id=?
                     AND child.generation_type='candidate'
                     AND child.lifecycle_state NOT IN ('discarded','pruned','promoted')
                   ORDER BY child.generation_number DESC,child.created_ts DESC
                   LIMIT 1""",
                (str(root_id),),
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return manager._candidate_row(str(root_id))


def _generation_state(manager, candidate_id):
    try:
        with manager.store.conn() as c:
            row = c.execute(
                "SELECT generation_id,lifecycle_state FROM agent_candidate_generations WHERE agent_id=?",
                (str(candidate_id),),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


@contextmanager
def _temporary_offline_gate_pass(manager, row, *, purpose):
    """Let passive comparison / atomic promoter cross only the offline evidence gate.

    Both the active-edge row and lineage ledger state are restored unless a successful
    atomic promotion consumes the Candidate. No model/config/control checks are bypassed.
    """
    if not row:
        yield
        return
    parent_id = str(row["parent_agent_id"])
    original_state = str(row.get("state") or "")
    original_gate_raw = row.get("offline_gate_json") or "{}"
    original_gate = _json(original_gate_raw, {})
    generation = _generation_state(manager, row.get("candidate_id"))
    original_generation_state = str((generation or {}).get("lifecycle_state") or "")
    if original_gate.get("passed") and original_state not in _BLOCKED_STATES:
        yield
        return

    temporary_gate = {
        **original_gate,
        "passed": True,
        "status": "passed_for_user_override" if purpose == "promote" else "passed_for_observation_only",
        "user_override": purpose == "promote",
        "observation_only": purpose != "promote",
        "temporary_override_ts": time.time(),
    }
    temporary_state = "comparing" if original_state in _BLOCKED_STATES else original_state
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            "UPDATE agent_candidates SET state=?,offline_gate_json=?,updated_ts=? WHERE parent_agent_id=?",
            (temporary_state, json.dumps(temporary_gate, separators=(",", ":")), time.time(), parent_id),
        )
        if generation and original_generation_state in _BLOCKED_STATES:
            c.execute(
                "UPDATE agent_candidate_generations SET lifecycle_state='comparing',updated_ts=? WHERE generation_id=?",
                (time.time(), generation["generation_id"]),
            )
    try:
        yield
    finally:
        fresh = manager._candidate_row(parent_id)
        if fresh:
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    "UPDATE agent_candidates SET state=?,offline_gate_json=?,updated_ts=? WHERE parent_agent_id=?",
                    (original_state, original_gate_raw, time.time(), parent_id),
                )
                if generation and original_generation_state:
                    c.execute(
                        "UPDATE agent_candidate_generations SET lifecycle_state=?,updated_ts=? WHERE generation_id=?",
                        (original_generation_state, time.time(), generation["generation_id"]),
                    )


def install(manager):
    if getattr(manager, "_candidate_user_promotion_installed", False):
        return manager

    original_summary = manager._comparison_summary
    original_before = manager.before_live_process
    original_after = manager.after_live_process
    original_promote = manager.promote
    handler = manager.core.Handler
    original_post = handler.do_POST

    def comparison_summary(row, parent=None, candidate=None):
        out = original_summary(row, parent, candidate)
        if bool(getattr(_PROMOTION_TLS, "active", False)):
            out["promotable"] = True
            out["user_promotion_override"] = True
        return out

    def _observation_call(fn, agent, state_map):
        row = _active_observation_row(manager, str(agent.get("id") or ""))
        if not row:
            return fn(agent, state_map)
        gate = _json(row.get("offline_gate_json"), {})
        if gate.get("passed") and str(row.get("state") or "") not in _BLOCKED_STATES:
            return fn(agent, state_map)
        # Passive A/B observation is safe to continue even when the offline benchmark is
        # blocked. Resolve the lineage leaf first: from G2 onward parent_agent_id is not
        # the Root Live id. The gate remains visible and still blocks normal Promote.
        with _temporary_offline_gate_pass(manager, row, purpose="observation"):
            return fn(agent, state_map)

    def before_live_process(agent, state_map):
        return _observation_call(original_before, agent, state_map)

    def after_live_process(agent, state_map):
        return _observation_call(original_after, agent, state_map)

    def promote_custom(parent_id, target_mode=None, rules=None):
        status = manager.status(parent_id)
        if not status:
            raise ValueError("Candidate not found")
        report = evaluate_custom_rules(status, rules)
        if not report["passed"]:
            raise ValueError("Custom promotion conditions not met: " + "; ".join(report["failures"]))
        row = manager._candidate_row(str(status.get("parent_agent_id") or parent_id))
        if not row:
            raise ValueError("Candidate disappeared before custom Promote")

        manager.store.event(
            str(status.get("root_agent_id") or status.get("parent_agent_id") or parent_id),
            "warning" if report["rules"]["allow_offline_gate_override"] and not report["observed"]["offline_gate_passed"] else "info",
            "candidate_custom_promotion_requested",
            "User accepted explicit custom Candidate promotion conditions",
            report,
        )
        previous = bool(getattr(_PROMOTION_TLS, "active", False))
        _PROMOTION_TLS.active = True
        try:
            with _temporary_offline_gate_pass(manager, row, purpose="promote"):
                result = original_promote(parent_id, target_mode)
        finally:
            _PROMOTION_TLS.active = previous
        result = dict(result or {})
        result["custom_promotion"] = report
        return result

    def do_post(http):
        path, _, _ = http.path.partition("?")
        if path.startswith("/api/agents/") and path.endswith("/candidate/promote-custom"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            parent_id = path.split("/")[3]
            try:
                body = http.read_json()
                body = body if isinstance(body, dict) else {}
                requested = body.get("target_mode")
                result = promote_custom(parent_id, requested, body.get("conditions"))
                return http.send_json(200, result)
            except (ValueError, RuntimeError) as exc:
                return http.send_json(409, {"error": str(exc), "candidate": manager.status(parent_id)})
        return original_post(http)

    manager._comparison_summary = comparison_summary
    manager.before_live_process = before_live_process
    manager.after_live_process = after_live_process
    manager.promote_custom = promote_custom
    handler.do_POST = do_post
    manager._candidate_user_promotion_installed = True
    manager.candidate_custom_promotion_contract = (
        "user_defined_future_thresholds_optional_offline_override_hard_atomic_control_guards_preserved"
    )
    manager.candidate_offline_gate_observation_contract = (
        "active_lineage_leaf_offline_gate_blocks_standard_promotion_but_not_passive_future_ab_collection"
    )
    return manager
