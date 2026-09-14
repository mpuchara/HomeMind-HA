"""Invalidate stale Control proof after Teach RL and collect a fresh Shadow benchmark.

Teach RL first performs the normal chronological offline rebuild and then changes that
rebuilt policy again with weighted supervised examples.  The held-out benchmark from the
rebuild therefore cannot certify the final fine-tuned policy.  This extension makes that
boundary explicit:

    offline rebuild -> Teach fine tuning -> Control qualification STALE -> Shadow benchmark

The stale transition clears the old benchmark counts and leaves the agent immediately
usable in Shadow.  Future target transitions are evaluated prequentially: the prediction
stored before a transition is scored before ``Engine.process_agent`` can learn from that
transition.  Control is never enabled automatically; after enough fresh Shadow evidence
passes the existing Wilson qualification contract, the proof is marked current while the
agent remains in Shadow for an explicit Control transition.
"""
import math
import threading
import time

from context import action_values, target_value
from qualification import assess_control_qualification


BENCHMARK_SOURCE = "teach-rl-shadow"
STALE_REASON = "Teach RL changed the final policy after the offline benchmark"


def _blank_counts():
    return {"samples": 0, "correct": 0, "per_action": {}, "origin_counts": {}}


def _is_binary(agent, actions):
    return len(actions) <= 2 or str(agent.get("target_property") or "") == "power"


def _score_from_counts(agent, counts, actions):
    counts = dict(counts or {})
    samples = int(counts.get("samples") or 0)
    per_action = dict(counts.get("per_action") or {})
    if _is_binary(agent, actions):
        accuracies = [
            float((row or {}).get("correct") or 0) / max(1, int((row or {}).get("samples") or 0))
            for row in per_action.values()
            if int((row or {}).get("samples") or 0) > 0
        ]
        return (sum(accuracies) / len(accuracies)) if accuracies else None
    return (float(counts.get("correct") or 0) / samples) if samples else None


def _score_sample(agent, counts, actions, predicted_value, actual_value, origin="external"):
    """Add one paired future outcome using the same correctness contract as history.py."""
    actions = [float(x) for x in actions]
    if not actions:
        return dict(counts or _blank_counts())
    predicted = min(range(len(actions)), key=lambda i: abs(actions[i] - float(predicted_value)))
    actual = min(range(len(actions)), key=lambda i: abs(actions[i] - float(actual_value)))
    if _is_binary(agent, actions):
        correct = predicted == actual
    else:
        tolerance = max(
            float(agent.get("deadband") or 0.0),
            (float(agent["max_value"]) - float(agent["min_value"])) * 0.03,
        )
        correct = abs(actions[predicted] - actions[actual]) <= tolerance

    out = {
        "samples": int((counts or {}).get("samples") or 0),
        "correct": int((counts or {}).get("correct") or 0),
        "per_action": {
            str(k): {"samples": int((v or {}).get("samples") or 0),
                     "correct": int((v or {}).get("correct") or 0)}
            for k, v in dict((counts or {}).get("per_action") or {}).items()
        },
        "origin_counts": {
            str(k): int(v or 0) for k, v in dict((counts or {}).get("origin_counts") or {}).items()
        },
    }
    out["samples"] += 1
    out["correct"] += 1 if correct else 0
    slot = out["per_action"].setdefault(str(actual), {"samples": 0, "correct": 0})
    slot["samples"] += 1
    slot["correct"] += 1 if correct else 0
    origin = str(origin or "external")
    out["origin_counts"][origin] = int(out["origin_counts"].get(origin) or 0) + 1
    return out


def _benchmark_detail(agent, counts, *, stale, model_revision, started_ts=None, reason=None):
    actions = [float(x) for x in action_values(agent)]
    per_action = dict((counts or {}).get("per_action") or {})
    per_action_accuracy = {
        str(key): float((row or {}).get("correct") or 0) / max(1, int((row or {}).get("samples") or 0))
        for key, row in per_action.items()
        if int((row or {}).get("samples") or 0) > 0
    }
    return {
        "control_qualification": "stale" if stale else "current",
        "qualification_stale": bool(stale),
        "rebenchmark_required": bool(stale),
        "rebenchmark_contract": "prequential_shadow",
        "rebenchmark_model_revision": None if model_revision is None else str(model_revision),
        "rebenchmark_started_ts": float(started_ts if started_ts is not None else time.time()),
        "balanced": bool(_is_binary(agent, actions)),
        "class_coverage": (len(per_action_accuracy) >= 2) if _is_binary(agent, actions) else bool(per_action_accuracy),
        "per_action_accuracy": per_action_accuracy,
        "counts": dict(counts or _blank_counts()),
        "reason": str(reason or (STALE_REASON if stale else "Fresh Shadow benchmark is current")),
    }


def mark_control_qualification_stale(store, engine, agent, policy):
    """Invalidate the rebuild proof after Teach fine tuning without disabling Shadow."""
    aid = str(agent["id"])
    previous = store.get_agent_config(aid) or dict(agent)
    previous_evidence = {
        "score": previous.get("benchmark_score"),
        "samples": int(previous.get("benchmark_samples") or 0),
        "source": previous.get("benchmark_source"),
    }

    # Store.save_model intentionally carries _benchmark_counts forward for normal resume.
    # Teach has changed the final policy after those counts were produced, so explicitly
    # write an empty set and prevent the old held-out proof from being inherited.
    raw = policy.serialize()
    raw["_benchmark_counts"] = {}
    store.save_model(aid, raw)

    detail = _benchmark_detail(
        previous, _blank_counts(), stale=True,
        model_revision=getattr(policy, "model_revision", None), reason=STALE_REASON,
    )
    store.set_training_state(
        aid, "qualified", score=None, samples=0, source=BENCHMARK_SOURCE, detail=detail,
    )
    fresh = store.get_agent_config(aid) or previous
    try:
        policy.agent = fresh
    except Exception:
        pass

    runtime = getattr(engine, "runtime", None)
    if isinstance(runtime, dict):
        rt = runtime.setdefault(aid, {})
        rt["decision_state"] = "shadow"
        rt["decision_reason"] = "Teach RL changed policy; fresh Shadow Control benchmark required"
        rt["teach_rl_rebenchmark"] = {
            "state": "stale", "samples": 0, "score": None,
            "source": BENCHMARK_SOURCE,
            "model_revision": detail["rebenchmark_model_revision"],
        }

    # Normally the training queue already released Control before the rebuild.  Keep the
    # safety ordering robust for direct/internal callers as well: persisted Shadow first,
    # then release any leftover Control handoff for this agent only.
    release_error = None
    if previous.get("mode") == "control":
        executor = getattr(engine, "executor", None)
        if executor is not None and hasattr(executor, "release_control"):
            try:
                executor.release_control(previous, reason="teach_rl_rebenchmark_stale")
            except Exception as exc:
                release_error = f"{type(exc).__name__}: {exc}"

    store.event(
        aid, "warning" if release_error else "info", "teach_rl_control_qualification_stale",
        "Teach RL changed the final policy; old Control qualification invalidated and Shadow rebenchmark started",
        {
            "previous_benchmark": previous_evidence,
            "model_revision": detail["rebenchmark_model_revision"],
            "mode": "shadow",
            "benchmark_source": BENCHMARK_SOURCE,
            "release_error": release_error,
        },
    )
    return detail


def _prediction_ttl(agent):
    interval = max(0.0, float(agent.get("action_interval") or 0.0))
    prop = str(agent.get("target_property") or "")
    if prop in ("power", "option_index"):
        return max(30.0, min(300.0, 30.0 + 10.0 * interval))
    return max(300.0, min(3600.0, 300.0 + 10.0 * interval))


class TeachRLShadowRebenchmark:
    def __init__(self, store, engine):
        self.store = store
        self.engine = engine
        self.lock = threading.RLock()
        self.last_target = {}
        self.pending = {}

    @staticmethod
    def active(agent):
        detail = dict((agent or {}).get("benchmark_detail") or {})
        return bool(
            agent
            and agent.get("training_state") == "qualified"
            and agent.get("benchmark_source") == BENCHMARK_SOURCE
            and (detail.get("qualification_stale") or detail.get("control_qualification") == "stale")
        )

    def _persist(self, agent, counts, model_revision, started_ts):
        aid = str(agent["id"])
        score = _score_from_counts(agent, counts, action_values(agent))
        detail = _benchmark_detail(
            agent, counts, stale=True, model_revision=model_revision,
            started_ts=started_ts, reason="Fresh post-Teach Shadow benchmark in progress",
        )
        self.store.set_training_state(
            aid, "qualified", score=score, samples=int(counts.get("samples") or 0),
            source=BENCHMARK_SOURCE, detail=detail,
        )
        fresh = self.store.get_agent_config(aid) or agent

        # Ask the existing Control qualification contract whether this fresh evidence is
        # sufficient. Evaluate a temporary non-stale view; a stale marker itself must
        # remain a hard blocker until the evidence passes.
        candidate = dict(fresh)
        candidate_detail = dict(detail)
        candidate_detail["qualification_stale"] = False
        candidate_detail["control_qualification"] = "current"
        candidate["benchmark_detail"] = candidate_detail
        qualification = assess_control_qualification(candidate)
        if qualification.get("passed"):
            candidate_detail["rebenchmark_required"] = False
            candidate_detail["qualified_ts"] = time.time()
            candidate_detail["reason"] = qualification.get("reason")
            self.store.set_training_state(
                aid, "qualified", score=score, samples=int(counts.get("samples") or 0),
                source=BENCHMARK_SOURCE, detail=candidate_detail,
            )
            self.store.event(
                aid, "info", "teach_rl_rebenchmark_qualified",
                "Fresh post-Teach Shadow benchmark passed Control qualification; agent remains in Shadow",
                {
                    "samples": int(counts.get("samples") or 0),
                    "score": score,
                    "lower_bound": qualification.get("lower_bound"),
                    "per_action": qualification.get("per_action"),
                    "model_revision": model_revision,
                },
            )
        rt = getattr(self.engine, "runtime", {}).setdefault(aid, {})
        rt["teach_rl_rebenchmark"] = {
            "state": "qualified" if qualification.get("passed") else "stale",
            "samples": int(counts.get("samples") or 0),
            "score": score,
            "source": BENCHMARK_SOURCE,
            "qualification": qualification,
            "model_revision": model_revision,
        }
        return bool(qualification.get("passed"))

    def before_process(self, agent, state_map):
        fresh = self.store.get_agent_config(agent["id"]) or agent
        aid = str(fresh["id"])
        if not self.active(fresh):
            with self.lock:
                self.last_target.pop(aid, None)
                self.pending.pop(aid, None)
            return fresh

        current = target_value((state_map or {}).get(fresh["target_entity"]), fresh["target_property"])
        if current is None or not math.isfinite(float(current)):
            return fresh
        current = float(current)
        now = time.time()
        with self.lock:
            previous = self.last_target.get(aid)
            pending = dict(self.pending.get(aid) or {})
            self.last_target[aid] = current

        deadband = max(0.01, float(fresh.get("deadband") or 0.01) * 0.05)
        changed = previous is not None and abs(current - float(previous)) > deadband
        if not changed or not pending:
            return fresh
        age = now - float(pending.get("ts") or 0.0)
        if age < 0 or age > _prediction_ttl(fresh):
            return fresh

        target_state = (state_map or {}).get(fresh["target_entity"])
        try:
            own_echo = bool(self.engine.own_command_echo(fresh, target_state, current))
        except Exception:
            own_echo = False
        if own_echo:
            return fresh

        detail = dict(fresh.get("benchmark_detail") or {})
        counts = dict(detail.get("counts") or _blank_counts())
        context = (target_state or {}).get("context") or {}
        origin = "manual" if context.get("user_id") and not context.get("parent_id") else "external"
        counts = _score_sample(
            fresh, counts, action_values(fresh), pending["prediction"], current, origin=origin,
        )
        self._persist(
            fresh, counts, pending.get("model_revision"),
            float(detail.get("rebenchmark_started_ts") or now),
        )
        return self.store.get_agent_config(aid) or fresh

    def after_process(self, agent):
        fresh = self.store.get_agent_config(agent["id"]) or agent
        aid = str(fresh["id"])
        if not self.active(fresh):
            with self.lock:
                self.pending.pop(aid, None)
            return
        rt = (getattr(self.engine, "runtime", {}) or {}).get(aid) or {}
        prediction = rt.get("last_prediction")
        try:
            prediction = float(prediction)
        except (TypeError, ValueError):
            return
        if not math.isfinite(prediction):
            return
        policy = (getattr(self.engine, "models", {}) or {}).get(aid)
        revision = getattr(policy, "model_revision", None) if policy is not None else None
        with self.lock:
            self.pending[aid] = {
                "ts": time.time(), "prediction": prediction,
                "model_revision": None if revision is None else str(revision),
            }


def install_teach_rl_rebenchmark(store, engine, teaching_service=None):
    """Install finalizer invalidation and an outer prequential Shadow benchmark wrapper."""
    existing = getattr(engine, "teach_rl_rebenchmark", None)
    if existing is not None:
        return existing

    service = TeachRLShadowRebenchmark(store, engine)
    original_process = getattr(engine, "process_agent", None)
    if callable(original_process):
        def process_with_teach_rebenchmark(agent, state_map, changed_entities=None):
            # Score the previous prediction before inner Engine/Tournament code can learn
            # from this target transition. Then let normal Shadow inference run and store
            # its new prediction for the next future outcome.
            service.before_process(agent, state_map)
            result = original_process(agent, state_map, changed_entities)
            service.after_process(agent)
            return result
        engine.process_agent = process_with_teach_rebenchmark

    teaching = teaching_service or getattr(engine, "rl_teaching", None)
    if teaching is not None and not getattr(teaching, "_rebenchmark_finalize_wrapped", False):
        original_finalize = teaching.finalize_retrain

        def finalize_with_rebenchmark(agent_id):
            report = original_finalize(agent_id)
            if report is None:
                return None
            agent = store.get_agent_config(agent_id)
            if not agent:
                return report
            policy = (getattr(engine, "models", {}) or {}).get(str(agent_id))
            if policy is None:
                policy = engine.policy(agent)
            detail = mark_control_qualification_stale(store, engine, agent, policy)
            updated = teaching._set_job_stage(
                agent_id, state="done",
                benchmark_score=None,
                benchmark_samples=0,
                benchmark_source=BENCHMARK_SOURCE,
                control_qualification="stale",
                control_qualification_reason=detail.get("reason"),
                rebenchmark_contract="prequential_shadow",
                rebenchmark_model_revision=detail.get("rebenchmark_model_revision"),
                stage="done",
            )
            try:
                engine.wake_event.set()
            except Exception:
                pass
            return updated

        teaching.finalize_retrain = finalize_with_rebenchmark
        teaching._rebenchmark_finalize_wrapped = True

    engine.teach_rl_rebenchmark = service
    return service
