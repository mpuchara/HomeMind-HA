#!/usr/bin/env python3
"""Deterministic product-level benchmark for the shipped HomeMind light runtime.

The benchmark keeps hidden occupancy and hidden light need outside the observation map.
Historical *demonstrations* are used for policy fitting; future hidden need is used only
for product evaluation.  The current product controller uses the production
ContextEngine/MultiHorizonPolicy feature path and is exercised through Engine.process_agent
in Shadow, so ActionIntent/Executor safety remains active and HA services are forbidden.

The synthetic actuation loop is not a claim about physical hardware.  CI separately boots
the real source entrypoint and built add-on image.  Wall-clock cost reported here is host
specific; quality metrics are deterministic for a given seed set.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from context import TemporalHistory
from context_engine import ContextEngine
from engine import Engine
import engine as engine_module
import executor as executor_module
from ha import AUTOMATION_KNOWLEDGE
from policy import MultiHorizonPolicy
from policy_full_ridge import FullRidgeLinUCBBackend
from qualification import assess_control_qualification
from settings import OPTIONS
from storage import Store

SCENARIOS = (
    "one_occupant", "two_occupants", "branch", "stillness", "no_arrival",
    "quick_return", "day", "night", "manual_change", "false_sensor",
    "sensor_moved", "habit_shift",
)
DEFAULT_SEEDS = (11, 23, 37)
TICKS = 60
TARGET = "light.benchmark_room"
SENSORS = (
    "binary_sensor.benchmark_entry",
    "binary_sensor.benchmark_presence",
    "binary_sensor.benchmark_motion",
    "device_tracker.benchmark_person",
    "sensor.benchmark_lux",
    "binary_sensor.benchmark_false_hint",
)


def _rng(seed: int, episode: int, tick: int, token: str) -> random.Random:
    # Random.seed(str) is deterministic and independent of Python's process hash seed.
    return random.Random(f"f24:{seed}:{episode}:{tick}:{token}")


def _state(entity_id, value, *, unit=None, device_class=None):
    attrs = {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    if device_class is not None:
        attrs["device_class"] = device_class
    return {"entity_id": entity_id, "state": str(value), "attributes": attrs}


def _truth(scenario: str, tick: int, phase: str):
    """Hidden physical truth.  Nothing from this dict is sent directly to a policy."""
    ambient = 12.0
    occupied = 15 <= tick < 44
    occupants = 1 if occupied else 0
    moving = occupied and (tick < 20 or tick % 11 == 0)
    entry = 9 <= tick < 13

    if scenario == "two_occupants":
        occupied = 12 <= tick < 51
        occupants = 2 if 24 <= tick < 40 else (1 if occupied else 0)
        moving = occupied and tick % 8 < 2
        entry = (7 <= tick < 11) or (21 <= tick < 24)
    elif scenario == "branch":
        occupied = False; occupants = 0; moving = False; entry = 9 <= tick < 13
    elif scenario == "stillness":
        occupied = 13 <= tick < 51; occupants = 1 if occupied else 0
        moving = occupied and (tick < 18 or tick == 39); entry = 8 <= tick < 12
    elif scenario == "no_arrival":
        occupied = False; occupants = 0; moving = False; entry = 8 <= tick < 12
    elif scenario == "quick_return":
        occupied = (12 <= tick < 25) or (30 <= tick < 48)
        occupants = 1 if occupied else 0
        moving = occupied and (tick < 17 or 30 <= tick < 35)
        entry = (7 <= tick < 11) or (27 <= tick < 30)
    elif scenario == "day":
        ambient = 220.0
    elif scenario == "night":
        ambient = 4.0
    elif scenario == "manual_change":
        # User wants darkness for this interval even though occupancy and ambient light
        # would normally imply ON.  This is hidden preference truth, not a sensor field.
        occupied = 12 <= tick < 48; occupants = 1 if occupied else 0
        moving = occupied and tick % 9 < 2; entry = 7 <= tick < 11
    elif scenario == "false_sensor":
        occupied = 14 <= tick < 43; occupants = 1 if occupied else 0
        entry = 8 <= tick < 12
    elif scenario == "sensor_moved":
        occupied = 14 <= tick < 46; occupants = 1 if occupied else 0
        entry = 8 <= tick < 12
    elif scenario == "habit_shift" and phase == "future":
        occupied = 9 <= tick < 38; occupants = 1 if occupied else 0
        moving = occupied and tick % 10 < 2
        # Old precursor habit disappears; entry reports after occupancy starts.
        entry = 13 <= tick < 17

    light_need = bool(occupied and ambient < 80.0)
    if scenario == "manual_change" and 25 <= tick < 36:
        light_need = False
    return {
        "occupied": occupied, "occupants": occupants, "moving": moving,
        "entry": entry, "ambient_lux": ambient, "light_need": light_need,
    }


def _truth_delayed(scenario, tick, phase, delay):
    return _truth(scenario, max(0, tick - delay), phase)


def observation(seed, episode, scenario, phase, tick, physical_light):
    """Observed HA-like states; hidden occupancy/light_need are intentionally absent."""
    truth = _truth(scenario, tick, phase)
    delayed = _truth_delayed(scenario, tick, phase, 1)
    r_presence = _rng(seed, episode, tick, "presence")
    r_motion = _rng(seed, episode, tick, "motion")
    r_lux = _rng(seed, episode, tick, "lux")
    r_false = _rng(seed, episode, tick, "false")

    # A moved sensor is a topology fault: in future it observes an adjacent area rather
    # than benchmark_room.  The entity ID is intentionally unchanged.
    presence_value = delayed["occupied"]
    if scenario == "sensor_moved" and phase == "future":
        presence_value = 8 <= tick < 26 and not delayed["occupied"]
    if r_presence.random() < 0.025:
        presence_state = "unavailable"
    else:
        if r_presence.random() < 0.015:
            presence_value = not presence_value
        presence_state = "on" if presence_value else "off"

    motion_value = delayed["moving"]
    if scenario == "stillness" and tick >= 19:
        motion_value = tick == 39
    if r_motion.random() < 0.04:
        motion_state = "unavailable"
    else:
        motion_state = "on" if motion_value else "off"

    entry_state = "on" if truth["entry"] else "off"
    tracker_state = "home" if truth["occupants"] else "not_home"

    # False hint is deliberately useful in historical data and breaks in future.
    if phase != "future":
        false_value = truth["light_need"]
    elif scenario == "false_sensor":
        false_value = not truth["light_need"]
    else:
        false_value = r_false.random() < 0.18

    # Actuation affects a measurement: emitted light is visible to the lux sensor.
    measured_lux = truth["ambient_lux"] + (185.0 if physical_light else 0.0) + r_lux.gauss(0.0, 3.5)
    lux_state = "unavailable" if r_lux.random() < 0.02 else f"{max(0.0, measured_lux):.2f}"

    states = {
        SENSORS[0]: _state(SENSORS[0], entry_state, device_class="motion"),
        SENSORS[1]: _state(SENSORS[1], presence_state, device_class="occupancy"),
        SENSORS[2]: _state(SENSORS[2], motion_state, device_class="motion"),
        SENSORS[3]: _state(SENSORS[3], tracker_state),
        SENSORS[4]: _state(SENSORS[4], lux_state, unit="lx", device_class="illuminance"),
        SENSORS[5]: _state(SENSORS[5], "on" if false_value else "off", device_class="occupancy"),
        TARGET: _state(TARGET, "on" if physical_light else "off"),
    }
    return states


def registry_for(phase="train", scenario=None):
    out = {
        TARGET: {"entity_id": TARGET, "area_id": "benchmark_room", "device_id": "light-device"},
        SENSORS[0]: {"entity_id": SENSORS[0], "area_id": "hall", "device_id": "entry-device"},
        SENSORS[1]: {"entity_id": SENSORS[1], "area_id": "benchmark_room", "device_id": "presence-device"},
        SENSORS[2]: {"entity_id": SENSORS[2], "area_id": "benchmark_room", "device_id": "motion-device"},
        SENSORS[3]: {"entity_id": SENSORS[3], "area_id": None, "device_id": "phone-device"},
        SENSORS[4]: {"entity_id": SENSORS[4], "area_id": "benchmark_room", "device_id": "lux-device"},
        SENSORS[5]: {"entity_id": SENSORS[5], "area_id": "benchmark_room", "device_id": "false-device"},
    }
    if phase == "future" and scenario == "sensor_moved":
        out[SENSORS[1]] = dict(out[SENSORS[1]], area_id="adjacent_room")
    return out


def agent_template():
    return {
        "id": "product-benchmark", "name": "F24 benchmark light", "target_entity": TARGET,
        "target_property": "power", "input_entities": list(SENSORS), "enabled": True,
        "mode": "shadow", "training_state": "qualified", "min_value": 0.0, "max_value": 1.0,
        "deadband": 0.5, "confidence_threshold": float(OPTIONS.get("control_min_confidence", 0.80)),
        "action_interval": 1.0, "exploration_step": 1.0, "micro_exploration": False,
        "exploration_interval": 21600.0, "ack_timeout": 0.0, "settling_seconds": 0.0,
        "manual_hold_seconds": 0.0, "auto_created": False,
    }


class FeatureRuntime:
    def __init__(self, agent, initial_states, model=None):
        self.agent = dict(agent)
        self.states = copy.deepcopy(initial_states)
        self.registry = registry_for()
        self.context = ContextEngine(OPTIONS)
        self.context.configure(self.states, entities=self.registry)
        self.temporal = TemporalHistory(maxlen=96)
        self.policy = MultiHorizonPolicy(
            self.agent, self.states, self.registry, set(), model=model, context_engine=self.context
        )

    def ingest(self, states, ts, *, learn_presence=True, registry=None):
        if registry is not None:
            self.registry = registry
            self.context.configure(self.states, entities=registry)
        for eid, state in states.items():
            self.states[eid] = state
            self.temporal.add(eid, ts, state)
            self.context.observe(eid, state, ts, learn=learn_presence)
        return self.policy.features(self.states, self.temporal, at_ts=ts)[0]


class Teacher:
    def __init__(self):
        self.action = 0
        self.need_since = None
        self.no_need_since = None

    def step(self, need, tick):
        if need:
            self.no_need_since = None
            if self.need_since is None:
                self.need_since = tick
            if tick - self.need_since >= 2:
                self.action = 1
        else:
            self.need_since = None
            if self.no_need_since is None:
                self.no_need_since = tick
            if tick - self.no_need_since >= 4:
                self.action = 0
        return self.action


class FixedAutomation:
    name = "fixed_automation"
    def __init__(self):
        self.action = 0; self.off_since = None
    def decide(self, states, tick, ts):
        presence = states[SENSORS[1]]["state"] == "on"
        entry = states[SENSORS[0]]["state"] == "on"
        try: lux = float(states[SENSORS[4]]["state"])
        except Exception: lux = None
        dark = lux is not None and lux < 85.0
        if dark and (presence or entry):
            self.action = 1; self.off_since = None
        elif self.action:
            if not presence:
                self.off_since = tick if self.off_since is None else self.off_since
                if tick - self.off_since >= 6:
                    self.action = 0
            else:
                self.off_since = None
        return self.action, {"abstained": False}


class ConservativeFallback:
    name = "conservative_fallback"
    def __init__(self):
        self.action = 0; self.clear_since = None
    def decide(self, states, tick, ts):
        presence_raw = states[SENSORS[1]]["state"]
        try: lux = float(states[SENSORS[4]]["state"])
        except Exception: lux = None
        if presence_raw == "on" and lux is not None and lux < 65.0:
            self.action = 1; self.clear_since = None
            return self.action, {"abstained": False}
        if presence_raw == "unavailable" or lux is None:
            return self.action, {"abstained": True, "reason": "missing_sensor_evidence"}
        if presence_raw == "off":
            self.clear_since = tick if self.clear_since is None else self.clear_since
            if tick - self.clear_since >= 9:
                self.action = 0
        else:
            self.clear_since = None
        return self.action, {"abstained": False}


class RidgeShadow:
    name = "full_ridge_shadow"
    def __init__(self, backend, feature_runtime):
        self.backend = backend; self.runtime = feature_runtime; self.action = 0
    def decide(self, states, tick, ts):
        features = self.runtime.ingest(states, ts, registry=registry_for("future"))
        chosen, confidence, _, _, support, novelty = self.backend.predict(features)
        self.action = int(float(chosen["value"]) >= 0.5)
        return self.action, {"abstained": False, "confidence": confidence, "support": support, "novelty": novelty,
                             "shadow_proxy": True}


class ProductionShadow:
    name = "production_current"
    def __init__(self, store, agent, policy, feature_runtime):
        self.store = store; self.agent = dict(agent); self.policy = policy; self.runtime = feature_runtime
        self.fallback = ConservativeFallback(); self.action = 0; self._stack = ExitStack()
        self._stack.enter_context(patch.object(engine_module, "STORE", store))
        self._stack.enter_context(patch.object(executor_module, "STORE", store))
        self._stack.enter_context(patch.object(executor_module.HA, "service", side_effect=AssertionError("Shadow benchmark must never dispatch HA service")))
        self._stack.enter_context(patch.object(AUTOMATION_KNOWLEDGE, "hints_for_target", return_value=(set(), [])))
        self.engine = Engine()
        self.engine.context = feature_runtime.context
        self.engine.temporal_history = feature_runtime.temporal
        self.engine.models[self.agent["id"]] = policy

    def close(self):
        self.engine.control_workers.shutdown(wait=True); self.engine.poll_worker.shutdown(wait=True); self._stack.close()

    def decide(self, states, tick, ts):
        self.runtime.ingest(states, ts, registry=registry_for("future"))
        self.engine.context = self.runtime.context
        self.engine.temporal_history = self.runtime.temporal
        self.engine.state_map = dict(self.runtime.states)
        self.engine.models[self.agent["id"]] = self.policy
        with patch.object(engine_module, "now_ts", return_value=float(ts)):
            self.engine.process_agent(self.agent, self.engine.state_map, set(states))
        rt = self.engine.runtime.get(self.agent["id"], {})
        prediction = rt.get("last_prediction")
        intent = rt.get("intent") or {}
        decision_state = rt.get("decision_state")
        confidence = float(rt.get("last_confidence") or 0.0)
        support = float(rt.get("historical_support") or 0.0)
        novelty = float(rt.get("context_novelty") or 1.0)
        # Counterfactual product action: only a Shadow intent that passed the normal
        # decision gates is allowed to drive the synthetic lamp.  Otherwise use the
        # conservative fallback rather than weakening a gate for the benchmark.
        allowed = decision_state == "shadow" or str(intent.get("status") or "").upper() == "SHADOW"
        if allowed and prediction is not None:
            self.action = int(float(prediction) >= 0.5)
            return self.action, {"abstained": False, "shadow_proxy": True, "confidence": confidence,
                                 "support": support, "novelty": novelty, "decision_state": decision_state}
        value, meta = self.fallback.decide(states, tick, ts)
        self.action = value
        return value, {**meta, "shadow_proxy": True, "fallback_used": True, "decision_state": decision_state,
                       "confidence": confidence, "support": support, "novelty": novelty}


def split_episode_id(seed, phase, scenario, replica):
    return f"{phase}:{seed}:{scenario}:{replica}"


def _phase_sequence(seed, phase, replicas):
    # Order differs by seed but membership is deterministic and split IDs never overlap.
    rows = [(scenario, rep) for rep in range(replicas) for scenario in SCENARIOS]
    random.Random(seed * 1009 + {"train": 1, "validation": 2, "future": 3}[phase]).shuffle(rows)
    return rows


def build_training_data(seed, replicas=1):
    first = observation(seed, 0, "one_occupant", "train", 0, 0)
    agent = agent_template()
    runtime = FeatureRuntime(agent, first)
    train_samples, validation_samples = [], []
    episode_no = 0; base = 1_700_000_000.0 + seed * 100_000.0
    for phase in ("train", "validation"):
        samples = train_samples if phase == "train" else validation_samples
        for scenario, replica in _phase_sequence(seed, phase, replicas):
            teacher = Teacher(); physical = 0
            episode_no += 1
            for tick in range(TICKS):
                ts = base + episode_no * 120.0 + tick
                states = observation(seed, episode_no, scenario, phase, tick, physical)
                features = runtime.ingest(states, ts, registry=registry_for(phase, scenario))
                desired = teacher.step(_truth(scenario, tick, phase)["light_need"], tick)
                samples.append({"features": dict(features), "action": int(desired), "ts": ts,
                                "episode_id": split_episode_id(seed, phase, scenario, replica), "scenario": scenario})
                if phase == "train":
                    horizon = min(runtime.policy.horizons)
                    runtime.policy.update(horizon, int(desired), features, 1.0, sample_ts=ts)
                physical = int(desired)
    # Chronological held-out demonstration validation: score first, then update only
    # reliability/calibration statistics.  No hidden future light_need is consumed.
    counts = {0: {"samples": 0, "correct": 0}, 1: {"samples": 0, "correct": 0}}
    for row in validation_samples:
        chosen, _, _, horizon, _, _ = runtime.policy.predict(row["features"])
        pred = int(float(chosen["value"]) >= 0.5); label = row["action"]
        counts[label]["samples"] += 1; counts[label]["correct"] += int(pred == label)
        runtime.policy.heads[int(horizon)].validate(label, row["features"], 1.0, sample_ts=row["ts"])
    total = sum(v["samples"] for v in counts.values()); correct = sum(v["correct"] for v in counts.values())
    balanced = statistics.mean((v["correct"] / v["samples"]) if v["samples"] else 0.0 for v in counts.values())
    detail = {"balanced": True, "counts": {"samples": total, "correct": correct,
              "per_action": {str(k): v for k, v in counts.items()}},
              "source": "chronological_validation_demonstrations_not_hidden_future_truth"}
    qualification_agent = dict(agent, benchmark_samples=total, benchmark_score=balanced, benchmark_detail=detail)
    control_qualification = assess_control_qualification(qualification_agent)
    return {
        "agent": agent, "runtime": runtime, "train": train_samples, "validation": validation_samples,
        "validation_balanced_accuracy": balanced, "validation_detail": detail,
        "control_qualification": control_qualification,
    }


def train_ridge(train_rows, validation_rows):
    # Feature set is selected from train only.  Future data is never inspected here.
    activity = {}
    for row in train_rows:
        for idx, value in row["features"].items():
            if abs(float(value)) > 1e-9:
                activity[idx] = activity.get(idx, 0) + 1
    feature_indices = [idx for idx, _ in sorted(activity.items(), key=lambda kv: (-kv[1], kv[0]))[:16]]
    if 0 not in feature_indices:
        feature_indices = [0] + feature_indices[:15]
    candidates = []
    for ridge in (0.25, 1.0, 4.0):
        model = FullRidgeLinUCBBackend(actions=[0.0, 1.0], horizons=[1], feature_indices=feature_indices, ridge=ridge)
        for row in train_rows:
            model.update(1, row["action"], row["features"], 1.0, sample_ts=row["ts"])
        correct = 0
        for row in validation_rows:
            chosen, *_ = model.predict(row["features"])
            correct += int(int(float(chosen["value"]) >= 0.5) == row["action"])
        candidates.append((correct / max(1, len(validation_rows)), ridge, model))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    accuracy, ridge, model = candidates[0]
    for row in validation_rows:
        chosen, _, _, horizon, _, _ = model.predict(row["features"])
        model.validate(horizon, row["action"], row["features"], 1.0, sample_ts=row["ts"])
    return model, {"selected_ridge": ridge, "validation_accuracy": accuracy,
                   "feature_indices": feature_indices, "selection_scope": "train+validation_only"}


def replay_history_for_features(seed, trained_policy_model, replicas=1):
    first = observation(seed, 0, "one_occupant", "train", 0, 0)
    runtime = FeatureRuntime(agent_template(), first, model=trained_policy_model)
    episode_no = 0; base = 1_700_000_000.0 + seed * 100_000.0
    for phase in ("train", "validation"):
        for scenario, replica in _phase_sequence(seed, phase, replicas):
            teacher = Teacher(); physical = 0; episode_no += 1
            for tick in range(TICKS):
                ts = base + episode_no * 120.0 + tick
                states = observation(seed, episode_no, scenario, phase, tick, physical)
                runtime.ingest(states, ts, registry=registry_for(phase, scenario))
                physical = teacher.step(_truth(scenario, tick, phase)["light_need"], tick)
    return runtime, episode_no, base


def evaluate_controller(seed, controller, start_episode_no, base, replicas=1):
    decision_ms = []; needed = 0; needed_on = 0; false_on = 0; premature = 0; chatter = 0
    delays = []; corrections = 0; episodes = 0; abstains = 0; fallback_uses = 0
    episode_no = start_episode_no
    for scenario, replica in _phase_sequence(seed, "future", replicas):
        episode_no += 1; episodes += 1; action = 0; last_action = 0; last_change = -999
        need_prev = False; need_start = None; first_on = None; mismatch_run = 0; correction_latched = False
        for tick in range(TICKS):
            ts = base + episode_no * 120.0 + tick
            truth = _truth(scenario, tick, "future")
            states = observation(seed, episode_no, scenario, "future", tick, action)
            start = time.perf_counter()
            new_action, meta = controller.decide(states, tick, ts)
            decision_ms.append((time.perf_counter() - start) * 1000.0)
            new_action = int(bool(new_action))
            abstains += int(bool(meta.get("abstained"))); fallback_uses += int(bool(meta.get("fallback_used")))
            need = bool(truth["light_need"])
            needed += int(need); needed_on += int(need and new_action); false_on += int((not need) and new_action)
            if last_action == 1 and new_action == 0 and need:
                premature += 1
            if new_action != last_action:
                if tick - last_change <= 4:
                    chatter += 1
                last_change = tick
            if need and not need_prev:
                need_start = tick; first_on = tick if new_action else None
            elif need and first_on is None and new_action:
                first_on = tick
            if (not need) and need_prev and need_start is not None:
                delays.append(float(TICKS if first_on is None else max(0, first_on - need_start)))
                need_start = None; first_on = None
            mismatch_run = mismatch_run + 1 if new_action != int(need) else 0
            if mismatch_run >= 2 and not correction_latched:
                corrections += 1; correction_latched = True
            if new_action == int(need):
                correction_latched = False
            need_prev = need; last_action = new_action; action = new_action
        if need_prev and need_start is not None:
            delays.append(float(TICKS if first_on is None else max(0, first_on - need_start)))
    ordered = sorted(decision_ms)
    p95 = ordered[min(len(ordered)-1, int(math.ceil(0.95 * len(ordered))) - 1)] if ordered else 0.0
    return {
        "needed_light_fraction": needed_on / max(1, needed),
        "missed_needed_light_seconds": needed - needed_on,
        "false_on_seconds": false_on,
        "premature_off_events": premature,
        "mean_on_delay_seconds": statistics.mean(delays) if delays else 0.0,
        "chatter_events": chatter,
        "corrections_per_100_episodes": 100.0 * corrections / max(1, episodes),
        "abstain_ticks": abstains, "fallback_ticks": fallback_uses,
        "episodes": episodes, "decision_calls": len(decision_ms), "inference_p95_ms_host": p95,
        "inference_mean_ms_host": statistics.mean(decision_ms) if decision_ms else 0.0,
    }


def _quality_view(metrics):
    return {k: v for k, v in metrics.items() if not k.endswith("_ms_host")}


def run_seed(seed, replicas=1):
    train_start = time.perf_counter()
    built = build_training_data(seed, replicas)
    current_policy = built["runtime"].policy
    ridge, ridge_selection = train_ridge(built["train"], built["validation"])
    train_ms = (time.perf_counter() - train_start) * 1000.0

    # Use the real validation result when persisting the synthetic Store state.  There is
    # no fixture score and no lowered threshold.  Control qualification is independently
    # computed above and may legitimately fail.
    tmp = tempfile.TemporaryDirectory(prefix="homemind-f24-")
    store = Store(Path(tmp.name) / "adaptive_ai.sqlite")
    created = store.create_agent({k: v for k, v in built["agent"].items() if k not in ("id", "mode", "training_state")})
    agent = dict(created)
    score = built["validation_balanced_accuracy"]
    min_shadow = float(OPTIONS.get("candidate_benchmark_threshold", 0.78))
    shadow_qualified = bool(score > min_shadow and all(v["samples"] >= 12 for v in built["validation_detail"]["counts"]["per_action"].values()))
    store.set_training_state(agent["id"], "qualified" if shadow_qualified else "paused",
                             score=score, samples=built["validation_detail"]["counts"]["samples"],
                             source="f24_actual_chronological_validation",
                             detail=built["validation_detail"])
    agent = store.get_agent_config(agent["id"])
    # Evaluation needs Shadow predictions even when Control proof is insufficient.  A
    # failed historical qualification is reported and the production comparator uses the
    # conservative fallback whenever runtime decision gates abstain.
    agent["mode"] = "shadow"
    agent["training_state"] = "qualified" if shadow_qualified else "paused"
    current_policy.agent = agent
    store.save_model(agent["id"], current_policy.serialize())

    current_runtime = built["runtime"]
    current_runtime.agent = agent; current_runtime.policy.agent = agent
    current, ridge_runtime = None, None
    try:
        current = ProductionShadow(store, agent, current_policy, current_runtime)
        ridge_runtime, episode_no, base = replay_history_for_features(seed, current_policy.serialize(), replicas)
        ridge_controller = RidgeShadow(ridge, ridge_runtime)
        results = {
            "fixed_automation": evaluate_controller(seed, FixedAutomation(), episode_no, base, replicas),
            "production_current": evaluate_controller(seed, current, episode_no, base, replicas),
            "full_ridge_shadow": evaluate_controller(seed, ridge_controller, episode_no, base, replicas),
            "conservative_fallback": evaluate_controller(seed, ConservativeFallback(), episode_no, base, replicas),
        }
    finally:
        if current is not None:
            current.close()
        tmp.cleanup()
    return {
        "seed": seed, "metrics": results, "train_ms_host": train_ms,
        "validation_balanced_accuracy": score, "shadow_qualified": shadow_qualified,
        "control_qualification": built["control_qualification"], "ridge_selection": ridge_selection,
        "split_episode_ids": {
            phase: [split_episode_id(seed, phase, s, r) for s, r in _phase_sequence(seed, phase, replicas)]
            for phase in ("train", "validation", "future")
        },
    }


def _aggregate(seed_runs):
    out = {}
    controllers = seed_runs[0]["metrics"].keys()
    for controller in controllers:
        keys = seed_runs[0]["metrics"][controller].keys()
        out[controller] = {}
        for key in keys:
            values = [float(run["metrics"][controller][key]) for run in seed_runs]
            mean = statistics.mean(values); sd = statistics.stdev(values) if len(values) > 1 else 0.0
            half = 1.96 * sd / math.sqrt(len(values)) if len(values) > 1 else 0.0
            out[controller][key] = {"mean": mean, "ci95_low": mean-half, "ci95_high": mean+half,
                                    "min": min(values), "max": max(values), "seeds": len(values)}
    return out


def _criteria(aggregate, seed_runs):
    fixed = aggregate["fixed_automation"]; current = aggregate["production_current"]
    checks = [
        ("needed_light_not_worse_than_fixed_by_more_than_2pp",
         current["needed_light_fraction"]["mean"] >= fixed["needed_light_fraction"]["mean"] - 0.02),
        ("false_on_not_worse_than_fixed",
         current["false_on_seconds"]["mean"] <= fixed["false_on_seconds"]["mean"]),
        ("premature_off_not_worse_than_fixed",
         current["premature_off_events"]["mean"] <= fixed["premature_off_events"]["mean"]),
        ("corrections_not_worse_than_fixed",
         current["corrections_per_100_episodes"]["mean"] <= fixed["corrections_per_100_episodes"]["mean"]),
        ("all_seeds_have_future_control_qualification", all(bool(r["control_qualification"].get("passed")) for r in seed_runs)),
        ("all_required_scenarios_present", all(set(SCENARIOS) == {x.split(":")[-2] for x in r["split_episode_ids"]["future"]} for r in seed_runs)),
    ]
    return [{"name": name, "passed": bool(passed)} for name, passed in checks]


def run(seeds=DEFAULT_SEEDS, replicas=1):
    seeds = tuple(int(x) for x in seeds)
    runs = [run_seed(seed, replicas) for seed in seeds]
    # Split identities are explicit and disjoint; this is checked here as a benchmark
    # invariant in addition to unit tests.
    for row in runs:
        sets = [set(row["split_episode_ids"][p]) for p in ("train", "validation", "future")]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise AssertionError("train/validation/future episode IDs overlap")
    aggregate = _aggregate(runs)
    criteria = _criteria(aggregate, runs)
    unmet = [x["name"] for x in criteria if not x["passed"]]
    return {
        "benchmark": "HomeMind product runtime benchmark F24 v1",
        "deterministic_quality_contract": 1,
        "seeds": list(seeds), "replicas_per_scenario_per_split": replicas,
        "scenarios": list(SCENARIOS), "ticks_per_episode": TICKS,
        "splits": "chronological train demonstrations -> validation demonstrations -> untouched future hidden-truth evaluation",
        "ground_truth": "hidden occupancy and hidden light_need are separate from observations; future truth is evaluation-only",
        "observation_model": "binary/numeric/tracker sensors with delay, noise, missing values; synthetic light changes measured lux",
        "evidence_semantics": {
            "historical_demonstration": "teacher action used to fit policy; not physical outcome and not preference probability",
            "bandit_reward": "backend update for logged demonstrated action only; unchosen action reward remains unknown",
            "presence_model": "production ContextEngine forecast derived causally from observed sensors, never direct hidden truth",
            "correction": "manual_change scenario changes hidden desired light state; no synthetic correction is relabeled as generic reward",
            "experiment": "not fabricated by this benchmark; production TrialRecord/Experiments tests remain authoritative",
            "shadow_proxy": "production_current and full_ridge_shadow are counterfactual synthetic actions; Candidate/Shadow never dispatch HA",
            "physical_outcome": "only simulated lamp/lux coupling; not evidence of real Home Assistant hardware performance",
        },
        "comparators": ["fixed_automation", "production_current", "full_ridge_shadow", "conservative_fallback"],
        "per_seed": runs, "aggregate": aggregate,
        "acceptance_criteria": criteria, "unmet_criteria": unmet,
        "model_deployment": {"automatic": False, "full_ridge_default_changed": False,
                             "reason": "benchmark evidence is reported; no backend is deployed merely because it exists"},
        "runtime_scope": {
            "quality_path": "production ContextEngine + MultiHorizonPolicy + Engine.process_agent Shadow + ActionIntent/Executor gates",
            "transport": "deterministic synthetic HA observations; source/image entrypoint boot is verified separately in CI",
            "ha_service_dispatch": "forbidden/asserted in Shadow benchmark",
        },
        "host_cost_notice": "wall-clock timings are host-specific and are not Raspberry Pi measurements",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default=",".join(str(x) for x in DEFAULT_SEEDS))
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run([int(x) for x in args.seeds.split(",") if x.strip()], max(1, args.replicas))
    print(json.dumps(result, indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
