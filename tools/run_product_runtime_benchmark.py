#!/usr/bin/env python3
"""Executable F24 benchmark for the shipped HomeMind runtime composition.

The library in ``benchmark_product_runtime`` owns the deterministic synthetic world and
metrics. This executable adds the final product composition used by ``trial_queue_main``.
Every seed is evaluated in a fresh subprocess so module-level compatibility installers
cannot leak state between homes/seeds.

The benchmark-only clock bridge makes Engine and Executor share synthetic event-time.
Production TTL semantics and thresholds are unchanged.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import benchmark_product_runtime as core

core.executor_module.now_ts = lambda: core.engine_module.now_ts()

SCENARIOS = core.SCENARIOS
SENSORS = core.SENSORS
DEFAULT_SEEDS = core.DEFAULT_SEEDS
_truth = core._truth
observation = core.observation
build_training_data = core.build_training_data
_quality_view = core._quality_view


class FinalRuntimeShadow:
    """Counterfactual lamp driven by the fully composed shipped Shadow runtime."""

    name = "production_current"

    def __init__(self, engine, agent, policy, feature_runtime):
        self.engine = engine
        self.agent = dict(agent)
        self.policy = policy
        self.runtime = feature_runtime
        self.fallback = core.ConservativeFallback()
        self.action = 0
        self.engine.context = feature_runtime.context
        self.engine.temporal_history = feature_runtime.temporal
        self.engine.models[self.agent["id"]] = policy

    def decide(self, states, tick, ts, *, scenario=None, phase="future"):
        self.runtime.ingest(states, ts, registry=core.registry_for(phase, scenario))
        self.engine.context = self.runtime.context
        self.engine.temporal_history = self.runtime.temporal
        self.engine.state_map = dict(self.runtime.states)
        self.engine.models[self.agent["id"]] = self.policy
        with patch.object(core.engine_module, "now_ts", return_value=float(ts)):
            self.engine.process_agent(self.agent, self.engine.state_map, set(states))
        rt = self.engine.runtime.get(self.agent["id"], {})
        prediction = rt.get("last_prediction")
        intent = rt.get("intent") or {}
        decision_state = rt.get("decision_state")
        confidence = float(rt.get("last_confidence") or 0.0)
        support = float(rt.get("historical_support") or 0.0)
        novelty = float(rt.get("context_novelty") or 1.0)
        manual_action = core._manual_hold_action(states, rt, ts)
        if manual_action is not None:
            self.action = manual_action
            return self.action, {
                "abstained": True, "reason": "explicit_user_manual_hold",
                "shadow_proxy": True, "manual_override_active": True,
                "confidence": confidence, "support": support, "novelty": novelty,
                "decision_state": decision_state,
                "sensor_area": (self.runtime.registry.get(core.SENSORS[1]) or {}).get("area_id"),
            }
        allowed = decision_state == "shadow" or str(intent.get("status") or "").upper() == "SHADOW"
        if allowed and prediction is not None:
            self.action = int(float(prediction) >= 0.5)
            return self.action, {
                "abstained": False, "shadow_proxy": True, "confidence": confidence,
                "support": support, "novelty": novelty, "decision_state": decision_state,
                "sensor_area": (self.runtime.registry.get(core.SENSORS[1]) or {}).get("area_id"),
            }
        value, meta = self.fallback.decide(states, tick, ts)
        self.action = value
        return value, {
            **meta, "shadow_proxy": True, "fallback_used": True,
            "decision_state": decision_state, "confidence": confidence,
            "support": support, "novelty": novelty,
            "sensor_area": (self.runtime.registry.get(core.SENSORS[1]) or {}).get("area_id"),
        }


def _prepare_shipped_runtime(store):
    """Install the same extension root reached by run.sh -> trial_queue_main.py."""
    import trial_queue_main as shipped
    from context import target_options_for_state, default_action_interval
    from qualification import assess_control_qualification
    from control import review_status, set_review_approval

    shipped_core = shipped.core
    shipped_core.STORE = store
    shipped_core.AUTOMATION_KNOWLEDGE = core.AUTOMATION_KNOWLEDGE
    shipped_core.target_options_for_state = target_options_for_state
    shipped_core.default_action_interval = default_action_interval
    shipped_core.assess_control_qualification = assess_control_qualification
    shipped_core.review_status = review_status
    shipped_core.set_review_approval = set_review_approval
    shipped_core.HISTORY = None
    shipped_core.EVENT_STREAM = None
    shipped_core.TRAINING_QUEUE = None

    shipped_core.prepare_runtime_extensions()
    engine = core.Engine()
    shipped_core.ENGINE = engine
    shipped_core.prepare_engine_extensions()
    descriptor = dict(getattr(shipped_core, "RUNTIME_COMPOSITION_CONTRACT", {}) or
                      shipped.RUNTIME_COMPOSITION_ROOT.descriptor())
    return shipped_core, engine, descriptor


def _close_shipped_runtime(engine):
    manager = getattr(engine, "agent_candidates", None)
    if manager is not None:
        try:
            manager.stop()
            if manager.is_alive():
                manager.join(timeout=2.0)
        except Exception:
            pass
    engine.control_workers.shutdown(wait=True)
    engine.poll_worker.shutdown(wait=True)


def _worker_run_seed(seed, replicas=1):
    """One isolated synthetic home/seed with one fresh final runtime composition."""
    tmp = tempfile.TemporaryDirectory(prefix="homemind-f24-final-")
    store = core.Store(Path(tmp.name) / "adaptive_ai.sqlite")
    shipped_core = None
    engine = None
    with ExitStack() as stack:
        stack.enter_context(patch.object(core.engine_module, "STORE", store))
        stack.enter_context(patch.object(core.executor_module, "STORE", store))
        stack.enter_context(patch.object(
            core.executor_module.HA, "service",
            side_effect=AssertionError("F24 Shadow benchmark must never dispatch HA service"),
        ))
        stack.enter_context(patch.object(
            core.AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=(set(), [])
        ))
        try:
            shipped_core, engine, composition = _prepare_shipped_runtime(store)

            train_start = time.perf_counter()
            built = core.build_training_data(seed, replicas)
            current_policy = built["runtime"].policy
            ridge, ridge_selection = core.train_ridge(built["train"], built["validation"])
            train_ms = (time.perf_counter() - train_start) * 1000.0

            created = store.create_agent({
                k: v for k, v in built["agent"].items()
                if k not in ("id", "mode", "training_state")
            })
            agent = dict(created)
            score = built["validation_balanced_accuracy"]
            min_shadow = float(core.OPTIONS.get("candidate_benchmark_threshold", 0.78))
            shadow_qualified = bool(
                score > min_shadow and
                all(v["samples"] >= 12 for v in
                    built["validation_detail"]["counts"]["per_action"].values())
            )
            store.set_training_state(
                agent["id"], "qualified" if shadow_qualified else "paused",
                score=score,
                samples=built["validation_detail"]["counts"]["samples"],
                source="f24_actual_chronological_validation",
                detail=built["validation_detail"],
            )
            agent = store.get_agent_config(agent["id"])
            agent["mode"] = "shadow"
            agent["training_state"] = "qualified" if shadow_qualified else "paused"
            current_policy.agent = agent
            store.save_model(agent["id"], current_policy.serialize())

            current_runtime = built["runtime"]
            current_runtime.agent = agent
            current_runtime.policy.agent = agent
            current = FinalRuntimeShadow(engine, agent, current_policy, current_runtime)

            ridge_runtime, episode_no, base = core.replay_history_for_features(
                seed, current_policy.serialize(), replicas
            )
            ridge_controller = core.RidgeShadow(ridge, ridge_runtime)
            results = {
                "fixed_automation": core.evaluate_controller(
                    seed, core.FixedAutomation(), episode_no, base, replicas
                ),
                "production_current": core.evaluate_controller(
                    seed, current, episode_no, base, replicas
                ),
                "full_ridge_shadow": core.evaluate_controller(
                    seed, ridge_controller, episode_no, base, replicas
                ),
                "conservative_fallback": core.evaluate_controller(
                    seed, core.ConservativeFallback(), episode_no, base, replicas
                ),
            }
            return {
                "seed": seed,
                "metrics": results,
                "train_ms_host": train_ms,
                "validation_balanced_accuracy": score,
                "validation_detail": built["validation_detail"],
                "shadow_qualified": shadow_qualified,
                "control_qualification": built["control_qualification"],
                "ridge_selection": ridge_selection,
                "runtime_composition": composition,
                "split_episode_ids": {
                    phase: [
                        core.split_episode_id(seed, phase, scenario, replica)
                        for scenario, replica in core._phase_sequence(seed, phase, replicas)
                    ]
                    for phase in ("train", "validation", "future")
                },
            }
        finally:
            if engine is not None:
                _close_shipped_runtime(engine)
            if shipped_core is not None:
                shipped_core.ENGINE = None
                shipped_core.STORE = None
            tmp.cleanup()


def _worker_command(seed, replicas):
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker-seed", str(seed),
         "--replicas", str(replicas), "--compact"],
        check=True, capture_output=True, text=True,
    )
    marker = "F24_WORKER_JSON="
    rows = [line[len(marker):] for line in proc.stdout.splitlines() if line.startswith(marker)]
    if not rows:
        raise RuntimeError("F24 worker did not emit a result: " + proc.stdout[-2000:] + proc.stderr[-2000:])
    return json.loads(rows[-1])


def run_seed(seed, replicas=1):
    """Public isolated-seed API; always uses a fresh process and full composition."""
    return _worker_command(int(seed), max(1, int(replicas)))


def run(seeds=DEFAULT_SEEDS, replicas=1):
    seeds = tuple(int(x) for x in seeds)
    replicas = max(1, int(replicas))
    runs = [run_seed(seed, replicas) for seed in seeds]
    for row in runs:
        sets = [set(row["split_episode_ids"][p]) for p in ("train", "validation", "future")]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise AssertionError("train/validation/future episode IDs overlap")
    aggregate = core._aggregate(runs)
    criteria = core._criteria(aggregate, runs)
    unmet = [x["name"] for x in criteria if not x["passed"]]
    descriptors = [r.get("runtime_composition") or {} for r in runs]
    chains = [d.get("entrypoint_chain") for d in descriptors]
    return {
        "benchmark": "HomeMind product runtime benchmark F24 v2",
        "deterministic_quality_contract": 2,
        "seeds": list(seeds),
        "replicas_per_scenario_per_split": replicas,
        "scenarios": list(core.SCENARIOS),
        "ticks_per_episode": core.TICKS,
        "splits": "chronological train demonstrations -> validation demonstrations -> untouched future hidden-truth evaluation",
        "ground_truth": "hidden occupancy and hidden light_need are separate from observations; future truth is evaluation-only",
        "observation_model": "binary/numeric/tracker sensors with delay, noise, missing values; synthetic light changes measured lux; future sensor_moved changes the Entity Registry area mapping",
        "evidence_semantics": {
            "historical_demonstration": "teacher action used to fit policy; not physical outcome and not preference probability",
            "bandit_reward": "backend update for logged demonstrated action only; unchosen action reward remains unknown",
            "presence_model": "production ContextEngine forecast derived causally from observed sensors, never direct hidden truth",
            "correction": "manual_change includes explicit user-origin target provenance plus a hidden desired-state change; it is not relabeled as generic reward",
            "experiment": "not fabricated by this benchmark; production TrialRecord/Experiments tests remain authoritative",
            "shadow_proxy": "production_current and full_ridge_shadow are counterfactual synthetic actions; Candidate/Shadow never dispatch HA",
            "physical_outcome": "only simulated lamp/lux coupling; not evidence of real Home Assistant hardware performance",
        },
        "comparators": [
            "fixed_automation", "production_current", "full_ridge_shadow", "conservative_fallback"
        ],
        "per_seed": runs,
        "aggregate": aggregate,
        "acceptance_criteria": criteria,
        "unmet_criteria": unmet,
        "model_deployment": {
            "automatic": False,
            "full_ridge_default_changed": False,
            "reason": "benchmark evidence is reported; no backend is deployed merely because it exists",
        },
        "runtime_scope": {
            "quality_path": "final RuntimeCompositionRoot + production ContextEngine/policy/decision composition + Engine.process_agent Shadow + ActionIntent/Executor gates",
            "entrypoint_chain": chains[0] if chains and all(x == chains[0] for x in chains) else chains,
            "composition_contract_versions": [d.get("version") for d in descriptors],
            "transport": "deterministic synthetic HA observations; source/image entrypoint boot is verified separately in CI",
            "ha_service_dispatch": "forbidden/asserted in Shadow benchmark",
            "manual_priority": "explicit user target provenance drives the production manual-hold path; benchmark effective action cannot override that hold",
            "seed_isolation": "fresh process per seed prevents module-level compatibility installers leaking between synthetic homes",
        },
        "host_cost_notice": "wall-clock timings are host-specific and are not Raspberry Pi measurements",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default=",".join(str(x) for x in DEFAULT_SEEDS))
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--worker-seed", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_seed is not None:
        row = _worker_run_seed(args.worker_seed, max(1, args.replicas))
        print("F24_WORKER_JSON=" + json.dumps(row, separators=(",", ":"), sort_keys=True))
        return
    result = run([int(x) for x in args.seeds.split(",") if x.strip()], max(1, args.replicas))
    print(json.dumps(result, indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
