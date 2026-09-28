"""Composition adapters that feed one EpisodeEvaluator from the current layered runtime.

This module is intentionally narrow: it never constructs ActionIntent and never calls the
Executor. It observes already-existing Live outcomes, experiment completion, Candidate
Shadow decisions and Sensor Tournament predictions. Candidate promotion switches to
independent episode evidence only after the existing coverage thresholds are satisfied;
legacy transition metrics remain an explicit compatibility fallback for older data.
"""
from __future__ import annotations

import hashlib
import time

from context import target_value
from episode_evaluator import EpisodeEvaluator


def _binary(value):
    try:
        return float(value) >= 0.5
    except (TypeError, ValueError):
        return None


def _stable(prefix, *parts):
    raw = "|".join(str(part) for part in parts)
    return prefix + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _safe_event(store, agent_id, kind, message, details=None):
    try:
        store.event(agent_id, "warning", kind, message, details)
    except Exception:
        pass


def _record_live_outcome(engine, evaluator, agent, pending, reason, user_id=None):
    if str(agent.get("target_property") or "") != "power" or not pending:
        return None
    start = float(pending.get("started_ts") or evaluator.clock())
    end = max(start, float(evaluator.clock()))
    action = _binary(pending.get("action_value"))
    acknowledged = pending.get("acknowledged_ts") is not None
    anticipated = bool(pending.get("anticipated"))
    presence_start = presence_end = None
    observable = True
    if anticipated:
        area = pending.get("area_id")
        try:
            forecast = engine.context.home.forecast(area, end)
            known = bool(forecast.get("known"))
            arrival = (engine.context.home.values.get(area, {}) or {}).get("arrival")
            if known:
                presence_start = False
                presence_end = bool(arrival is not None and start < float(arrival) <= end)
            else:
                observable = False
        except Exception:
            observable = False
    episode_id = pending.get("episode_id") or pending.get("decision_id") or _stable(
        "live", agent.get("id"), f"{start:.6f}", pending.get("action_index"), pending.get("action_value")
    )
    observations = [
        {"ts": start, "presence": presence_start, "light_need": None,
         "power": action if acknowledged else None, "observable": observable},
        {"ts": end, "presence": presence_end, "light_need": None,
         "power": action if acknowledged else None, "observable": observable},
    ]
    return evaluator.evaluate_episode(
        episode_id=episode_id,
        agent_id=str(agent.get("id")),
        start_ts=start,
        end_ts=end,
        observations=observations,
        policies=[{
            "policy_key": "live:" + str(agent.get("id")),
            "role": "live",
            "executed": acknowledged,
            "initial_power": action,
            "decisions": [{"ts": start, "power": action, "anticipatory": anticipated,
                           "decision_id": pending.get("decision_id")}],
        }],
        manual_corrections=([{"ts": end}] if str(reason) == "manual correction" else []),
        context={"reward_reason": str(reason), "user_id_present": bool(user_id)},
        end_reason=str(reason),
    )


def _experiment_episode(engine, evaluator, aid, trial, reason):
    start = float(trial.get("observation_start") or trial.get("action_at") or trial.get("started") or evaluator.clock())
    end = max(start, float(evaluator.clock()))
    kind = str(trial.get("kind") or "reference")
    probe = kind == "probe"
    baseline = _binary(trial.get("baseline"))
    value = _binary(trial.get("value"))
    acknowledged = trial.get("ack") is not None
    focus_presence = str(trial.get("focus")) == "presence" and str(trial.get("property")) == "power"
    outcome_sources = trial.get("outcome_sources") or {}
    text = str(reason or "")
    observable = True
    presence_start = presence_end = None
    if focus_presence:
        if "presence confirmed after decision" in text:
            presence_start, presence_end = False, True
        elif outcome_sources and "observed without correction" in text:
            presence_start = presence_end = False
        elif any(token in text for token in ("unobservable", "unavailable", "interrupted", "no device acknowledgement", "deadline")):
            observable = False
        elif outcome_sources:
            observable = False
    physical = value if (probe and acknowledged) else baseline if kind == "reference" else None
    result = evaluator.evaluate_episode(
        episode_id="experiment:" + str(trial.get("trial_id") or _stable("trial", aid, start)),
        agent_id=str(aid),
        start_ts=start,
        end_ts=end,
        observations=[
            {"ts": start, "presence": presence_start, "light_need": None,
             "power": physical, "observable": observable},
            {"ts": end, "presence": presence_end, "light_need": None,
             "power": physical, "observable": observable},
        ],
        policies=[{
            "policy_key": "experiment:" + str(trial.get("trial_id") or aid),
            "role": "experiment_probe" if probe else "experiment_reference",
            "executed": bool(acknowledged or kind == "reference"),
            "initial_power": baseline,
            "decisions": [{
                "ts": float(trial.get("action_at") or start),
                "power": value if probe else baseline,
                "anticipatory": bool(probe and baseline is False and value is True),
            }],
        }],
        automation_replay=[{"ts": start, "power": baseline}, {"ts": end, "power": baseline}],
        automation_initial=baseline,
        manual_corrections=([{"ts": end}] if text == "manual correction" else []),
        context={"trial_id": trial.get("trial_id"), "kind": kind, "focus": trial.get("focus")},
        end_reason=text,
    )
    return result


def resolve_experiment_outcome(engine, evaluator, aid, trial, reward, reason):
    """Resolve one physical/reference trial once and reuse it through wrapper layers."""
    trial = trial if isinstance(trial, dict) else {}
    cached = trial.get('_episode_resolution') if isinstance(trial.get('_episode_resolution'), dict) else None
    if cached and str(cached.get('trial_id') or '') == str(trial.get('trial_id') or ''):
        return cached.get('episode'), cached.get('reward'), str(cached.get('reason') or reason)

    evaluated = None
    resolved_reward, resolved_reason = reward, str(reason)
    if trial and str(trial.get('property')) == 'power':
        evaluated = _experiment_episode(engine, evaluator, aid, trial, resolved_reason)
        metrics = (evaluated.get('policies') or [{}])[0].get('metrics') or {}
        if trial.get('kind') == 'probe' and metrics.get('false_arrival_prediction') == 1:
            resolved_reward = engine.executor.reward_engine.evaluate(
                anticipated=True, observation_complete=True, observation_known=True
            ).value
            resolved_reason = 'presence prediction not confirmed in observable episode'
    resolution = {
        'trial_id': trial.get('trial_id'),
        'episode': evaluated,
        'reward': resolved_reward,
        'reason': resolved_reason,
    }
    # This is runtime-only coordination between composed wrappers. Durable state remains
    # EpisodeEvaluator + TrialRecord; no extra learner or dispatch path is introduced.
    trial['_episode_resolution'] = resolution
    return evaluated, resolved_reward, resolved_reason

def install_core(core, evaluator: EpisodeEvaluator):
    """Attach episode evaluation before any runtime extension workers start."""
    engine = core.ENGINE
    if getattr(engine, "_episode_evaluator_core_installed", False):
        return evaluator
    engine.episode_evaluator = evaluator
    engine.experiments.episode_evaluator = evaluator

    original_reward_pending = engine._reward_pending

    def reward_pending(agent, rt, reward, reason, user_id=None, experience=None):
        pending = dict(experience if experience is not None else (rt.get("pending") or {}))
        result = original_reward_pending(agent, rt, reward, reason, user_id, experience)
        if pending and str(agent.get("target_property") or "") == "power":
            try:
                _record_live_outcome(engine, evaluator, agent, pending, reason, user_id)
            except Exception as exc:
                _safe_event(core.STORE, agent.get("id"), "episode_evaluator_live_gap",
                            "Live outcome could not be persisted as an episode",
                            {"error": f"{type(exc).__name__}: {exc}"})
        return result

    engine._reward_pending = reward_pending

    experiments = engine.experiments
    original_finish = experiments._finish

    def finish(aid, reward, reason):
        trial = experiments._get(aid).get("active") or {}
        if trial and str(trial.get("property")) == "power":
            try:
                _evaluated, reward, reason = resolve_experiment_outcome(
                    engine, evaluator, aid, trial, reward, reason
                )
            except Exception as exc:
                _safe_event(core.STORE, aid, "episode_evaluator_experiment_gap",
                            "Experiment outcome could not be persisted as an episode",
                            {"error": f"{type(exc).__name__}: {exc}"})
        return original_finish(aid, reward, reason)

    experiments._finish = finish
    engine._episode_evaluator_core_installed = True
    return evaluator


def _tournament_keys(aid, challenger):
    base = f"{aid}:{challenger}"
    return "tournament:active:" + base, "tournament:shadow:" + base


def install_tournament(service, evaluator: EpisodeEvaluator):
    """Observe Tournament policy pairs in bounded episodes without changing dispatch."""
    if getattr(service, "_episode_evaluator_installed", False):
        return service
    engine = service.engine
    state = {}
    original_observe = service.observe_shadow
    original_status = service.shadow_status

    def finish_pending(aid, challenger, pending, now, current, reason):
        active_key, shadow_key = _tournament_keys(aid, challenger)
        start = float(pending["start"])
        initial = pending["current"]
        observations = [
            {"ts": start, "presence": None, "light_need": None, "power": initial},
            {"ts": now, "presence": None, "light_need": None, "power": current},
        ]
        automation = [{"ts": start, "power": initial}, {"ts": now, "power": current}]
        return evaluator.evaluate_episode(
            episode_id=_stable("tournament", aid, challenger, pending["event"], f"{start:.6f}"),
            agent_id=str(aid), start_ts=start, end_ts=now,
            observations=observations,
            policies=[
                {"policy_key": active_key, "role": "sensor_tournament_active", "executed": False,
                 "initial_power": initial,
                 "decisions": [{"ts": start, "power": pending["active"],
                                "anticipatory": initial is False and pending["active"] is True}]},
                {"policy_key": shadow_key, "role": "sensor_tournament_challenger", "executed": False,
                 "initial_power": initial,
                 "decisions": [{"ts": start, "power": pending["shadow"],
                                "anticipatory": initial is False and pending["shadow"] is True}]},
            ],
            automation_replay=automation,
            automation_initial=initial,
            context={"challenger": challenger, "evaluation": "sensor_tournament"},
            end_reason=reason,
        )

    def observe(agent, state_map=None, changed_entities=None):
        states = dict(state_map or getattr(engine, "state_map", {}) or {})
        aid = str(agent.get("id"))
        now = time.time()
        current = _binary(target_value(states.get(agent.get("target_entity")), agent.get("target_property")))
        agent_state = state.setdefault(aid, {})
        if current is not None:
            for challenger, pending in list(agent_state.items()):
                elapsed = now - float(pending["start"])
                if current != pending["current"] or elapsed >= float(pending["window"]):
                    try:
                        finish_pending(aid, challenger, pending, now, current,
                                       "target transition" if current != pending["current"] else "bounded no-change opportunity")
                    except Exception as exc:
                        _safe_event(service.store, aid, "episode_evaluator_tournament_gap",
                                    "Tournament episode could not be persisted",
                                    {"challenger": challenger, "error": f"{type(exc).__name__}: {exc}"})
                    agent_state.pop(challenger, None)

        result = original_observe(agent, states, changed_entities)
        if current is None or str(agent.get("target_property") or "") != "power":
            return result
        runtime = engine.runtime.get(aid) or {}
        active = _binary(runtime.get("last_prediction"))
        predictions = dict((getattr(service, "_shadow_runtime", {}).get(aid) or {}).get("predictions") or {})
        event = runtime.get("last_intent", {}).get("intent_id") or runtime.get("last_intent", {}).get("decision_id") or f"{now:.6f}"
        for challenger in list((service.state(aid) or {}).get("challenger_features") or []):
            if challenger in agent_state:
                continue
            shadow = _binary((predictions.get(challenger) or {}).get("shadow_index"))
            if shadow is None:
                shadow = _binary((predictions.get(challenger) or {}).get("shadow_value"))
            if active is None or shadow is None or (active == current and shadow == current):
                continue
            window = 8.0 if current is False and (active is True or shadow is True) else 120.0
            agent_state[challenger] = {
                "start": now, "current": current, "active": active, "shadow": shadow,
                "window": window, "event": event,
            }
        return result

    def status(agent):
        payload = original_status(agent)
        aid = str(agent.get("id"))
        for row in payload.get("challengers") or []:
            challenger = row.get("entity_id")
            active_key, shadow_key = _tournament_keys(aid, challenger)
            try:
                row["episode_comparison"] = evaluator.compare_policies(aid, active_key, shadow_key)
            except Exception:
                row["episode_comparison"] = None
        payload["episode_evaluator_contract"] = "light_power_v1_same_episode_proxy_marked"
        return payload

    service.observe_shadow = observe
    service.shadow_status = status
    service.episode_evaluator = evaluator
    service._episode_evaluator_installed = True
    return service


def _candidate_policy_key(generation_id):
    return "generation:" + str(generation_id)


def _candidate_pair_episode(manager, evaluator, root_id):
    with manager.store.conn() as c:
        pair = c.execute(
            """SELECT * FROM candidate_generation_pairs WHERE root_agent_id=?
               ORDER BY outcome_ts DESC LIMIT 1""", (str(root_id),)
        ).fetchone()
        if not pair:
            return None
        pair = dict(pair)
        decision = c.execute(
            """SELECT current FROM candidate_generation_decisions
               WHERE event_id=? AND generation_id=? ORDER BY ts DESC LIMIT 1""",
            (pair["prediction_event_id"], pair["parent_generation_id"]),
        ).fetchone()
    initial = _binary(decision[0] if decision else (1.0 - float(pair["outcome"])))
    outcome = _binary(pair["outcome"])
    start = float(pair["prediction_ts"])
    end = float(pair["outcome_ts"])
    if end < start:
        return None
    evaluated = evaluator.evaluate_episode(
        episode_id=_stable("candidate-transition", pair["parent_generation_id"], pair["child_generation_id"], f"{end:.6f}"),
        agent_id=str(root_id), start_ts=start, end_ts=end,
        observations=[
            {"ts": start, "presence": None, "light_need": None, "power": initial},
            {"ts": end, "presence": None, "light_need": None, "power": outcome},
        ],
        policies=[
            {"policy_key": _candidate_policy_key(pair["parent_generation_id"]), "role": "candidate_parent",
             "executed": False, "initial_power": initial,
             "decisions": [{"ts": start, "power": pair["parent_prediction"],
                            "anticipatory": initial is False and _binary(pair["parent_prediction"]) is True}]},
            {"policy_key": _candidate_policy_key(pair["child_generation_id"]), "role": "candidate_shadow",
             "executed": False, "initial_power": initial,
             "decisions": [{"ts": start, "power": pair["child_prediction"],
                            "anticipatory": initial is False and _binary(pair["child_prediction"]) is True}]},
        ],
        automation_replay=[{"ts": start, "power": initial}, {"ts": end, "power": outcome}],
        automation_initial=initial,
        context={"prediction_event_id": pair["prediction_event_id"], "evidence": "automation_replay_only"},
        end_reason="observed target transition; not a light-need label",
    )
    # Current Candidate transition episodes deliberately carry no light_need label.
    # If a future/parallel independent EpisodeEvaluator source supplies one for the
    # same episode contract, attach it through the Stage-13 immutable calibration
    # overlay instead of rewriting the raw target transition.
    light_need = str((evaluated.get("labels") or {}).get("light_need") or "")
    recorder = getattr(manager, "record_independent_candidate_label", None)
    if callable(recorder) and light_need in {"true", "false"}:
        recorder(
            parent_generation_id=pair["parent_generation_id"],
            child_generation_id=pair["child_generation_id"],
            prediction_event_id=pair["prediction_event_id"],
            desired_action=1.0 if light_need == "true" else 0.0,
            source_kind="episode_evaluator_independent",
            source_id=str(evaluated.get("episode_id") or ""),
            dependency_cluster=f"episode:{evaluated.get('episode_id')}",
        )
    return evaluated


def _candidate_edge(manager, root_id):
    try:
        from agent_candidate_shadow_runtime import _active_comparison_edge
        return _active_comparison_edge(manager, root_id)
    except Exception:
        return None


def install_candidate(manager, evaluator: EpisodeEvaluator):
    """Feed Candidate Shadow episodes and let named gates consume independent labels."""
    if getattr(manager, "_episode_evaluator_installed", False):
        return manager
    original_before = manager.before_live_process
    original_after = manager.after_live_process
    original_summary = manager._comparison_summary
    false_runtime = {}

    def before(agent, state_map):
        result = original_before(agent, state_map)
        if result:
            try:
                _candidate_pair_episode(manager, evaluator, agent.get("id"))
            except Exception as exc:
                _safe_event(manager.store, agent.get("id"), "episode_evaluator_candidate_gap",
                            "Candidate transition episode could not be persisted",
                            {"error": f"{type(exc).__name__}: {exc}"})
        return result

    def after(agent, state_map):
        bundle = original_after(agent, state_map)
        if not bundle or str(agent.get("target_property") or "") != "power":
            return bundle
        root_id = str(agent.get("id"))
        edge = _candidate_edge(manager, root_id)
        if not edge:
            false_runtime.pop(root_id, None)
            return bundle
        parent_gid = edge["parent_generation_id"]
        child_gid = edge["child_generation_id"]
        parent = (bundle.get("results") or {}).get(parent_gid)
        child = (bundle.get("results") or {}).get(child_gid)
        current = _binary(bundle.get("current"))
        if not parent or not child or current is None:
            return bundle
        p_desired, c_desired = _binary(parent.get("desired")), _binary(child.get("desired"))
        now = float(bundle.get("ts") or time.time())
        pending = false_runtime.get(root_id)
        if pending:
            resolved = (p_desired == current and c_desired == current)
            timed_out = now - pending["start"] >= pending["window"]
            changed = current != pending["current"]
            if resolved or timed_out or changed:
                try:
                    evaluator.evaluate_episode(
                        episode_id=_stable("candidate-nochange", parent_gid, child_gid, pending["event"]),
                        agent_id=root_id, start_ts=pending["start"], end_ts=now,
                        observations=[
                            {"ts": pending["start"], "presence": None, "light_need": None, "power": pending["current"]},
                            {"ts": now, "presence": None, "light_need": None, "power": current},
                        ],
                        policies=[
                            {"policy_key": _candidate_policy_key(parent_gid), "role": "candidate_parent",
                             "executed": False, "initial_power": pending["current"],
                             "decisions": [{"ts": pending["start"], "power": pending["parent"],
                                            "anticipatory": pending["current"] is False and pending["parent"] is True}]},
                            {"policy_key": _candidate_policy_key(child_gid), "role": "candidate_shadow",
                             "executed": False, "initial_power": pending["current"],
                             "decisions": [{"ts": pending["start"], "power": pending["child"],
                                            "anticipatory": pending["current"] is False and pending["child"] is True}]},
                        ],
                        automation_replay=[{"ts": pending["start"], "power": pending["current"]},
                                           {"ts": now, "power": current}],
                        automation_initial=pending["current"],
                        context={"evidence": "negative_no_target_change_proxy"},
                        end_reason="bounded no-change opportunity" if timed_out else "prediction excursion resolved",
                    )
                except Exception as exc:
                    _safe_event(manager.store, root_id, "episode_evaluator_candidate_gap",
                                "Candidate no-change episode could not be persisted",
                                {"error": f"{type(exc).__name__}: {exc}"})
                false_runtime.pop(root_id, None)
                pending = None
        if pending is None and (p_desired != current or c_desired != current):
            window = 8.0 if current is False and (p_desired is True or c_desired is True) else 120.0
            false_runtime[root_id] = {
                "start": now, "current": current, "parent": p_desired, "child": c_desired,
                "window": window, "event": bundle.get("event_id") or f"{now:.6f}",
            }
        return bundle

    def comparison_summary(row, parent=None, candidate=None):
        out = original_summary(row, parent, candidate)
        try:
            with manager.store.conn() as c:
                generation = c.execute(
                    "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
                    (str(row.get("candidate_id")),),
                ).fetchone()
            if not generation or not generation["parent_generation_id"]:
                return out
            generation = dict(generation)
            parent_gid = generation["parent_generation_id"]
            child_gid = generation["generation_id"]
            root_id = generation["root_agent_id"]
            comparison = evaluator.compare_policies(
                str(root_id), _candidate_policy_key(parent_gid), _candidate_policy_key(child_gid)
            )
            out["episode_comparison"] = comparison
            out["episode_evidence_mode"] = comparison.get("evidence_mode")
            independent = int(comparison.get("independently_observed_episodes") or 0)
            required = int(out.get("required_future_samples") or 0)
            per_action = dict((comparison.get("candidate") or {}).get("per_action_episodes") or {})
            required_per_action = int(out.get("required_future_samples_per_action") or 0)
            enough = independent >= required and all(
                int(per_action.get(str(value), 0)) >= required_per_action for value in (0.0, 1.0)
            )
            if enough:
                parent_stats = comparison["parent"]
                candidate_stats = comparison["candidate"]
                p_rate = parent_stats.get("episode_success_rate")
                c_rate = candidate_stats.get("episode_success_rate")
                from agent_candidate_preference_metrics import (
                    _option_float, _preference_confidence, _promotion_gate_report,
                    DEFAULT_MAX_ACCURACY_REGRESSION,
                )
                max_regression = _option_float(
                    "agent_candidate_max_accuracy_regression", DEFAULT_MAX_ACCURACY_REGRESSION, 0.0
                )
                out.update({
                    "comparison_metric": "episode_light_power",
                    "meaningful_opportunities": independent,
                    "future_sample_count_ready": True,
                    "per_action_ready": True,
                    "fast_per_action_samples": per_action,
                    "parent_episode_success_rate": p_rate,
                    "candidate_episode_success_rate": c_rate,
                    "parent_transition_accuracy": p_rate,
                    "candidate_transition_accuracy": c_rate,
                    "accuracy_safety_passed": bool(
                        p_rate is not None and c_rate is not None and c_rate + max_regression >= p_rate
                    ),
                    "manual_corrections_since_generation": int(
                        round((candidate_stats.get("metric_sums") or {}).get("manual_correction_count") or 0.0)
                    ),
                })
                successes = max(0, independent - int(candidate_stats.get("harmful_episodes") or 0))
                failures = int(candidate_stats.get("harmful_episodes") or 0)
                out["preference_confidence"] = _preference_confidence(successes, failures)
                out["no_new_corrections"] = out["manual_corrections_since_generation"] == 0
                parent_agent = parent or manager.store.get_agent_config(row.get("parent_agent_id"))
                candidate_agent = candidate or manager.store.get_agent(row.get("candidate_id"))
                gates, vetoes = _promotion_gate_report(manager, row, parent_agent, candidate_agent, out)
                out["promotion_gates"] = gates
                out["promotion_vetoes"] = vetoes
                out["promotion_veto_reasons"] = [item["reason"] for item in vetoes]
                out["promotable"] = not vetoes
        except Exception as exc:
            out["episode_evaluator_error"] = f"{type(exc).__name__}: {exc}"
        return out

    manager.before_live_process = before
    manager.after_live_process = after
    manager._comparison_summary = comparison_summary
    manager.episode_evaluator = evaluator
    manager._episode_evaluator_installed = True
    manager.candidate_episode_contract = "same_finalized_light_power_episode_independent_labels_preferred"
    return manager
