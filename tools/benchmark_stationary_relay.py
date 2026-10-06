"""Actual isolated history worker and persisted policy: numeric radar + light relay."""
from pathlib import Path
import json
import tempfile

import benchmark_feature_snapshot_reuse as fixture
from context import ExplicitFeatureSchema, TemporalHistory
from policy import MultiHorizonPolicy
from training_process import descriptor_checksum
from settings import OPTIONS
from observation_contract import FeatureSchemaV12, install_training_contract
from context_engine import ContextEngine

TARGET = "switch.shellyplus1pm_441793a613bc_switch_0"
RADAR = "sensor.espen4_stationary_energy"


def run(feature_contract=3, neural=False, contradictory_binary=False):
    contract = install_training_contract()
    try:
        return _run(feature_contract, neural, contradictory_binary)
    finally:
        contract["restore"]()


def _run(feature_contract, neural, contradictory_binary):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db, aid, base, end, _, _ = fixture.seed_database(root)
        phases = 240
        durations = [600 + (phase % 3) * 120 for phase in range(phases + 1)]
        end = base + sum(durations[:phases])
        store = fixture.Store(db)
        inputs = [RADAR]
        if contradictory_binary:
            inputs += ["binary_sensor.espen4_still_target", "binary_sensor.espen4_moving_target"]
        with store.conn() as conn:
            conn.execute("DELETE FROM entity_history")
            conn.execute("UPDATE agents SET target_entity=?, input_entities=? WHERE id=?",
                         (TARGET, json.dumps(inputs), aid))
        rows = []
        attrs = {"friendly_name": "ESPEN4 Stationary Energy", "unit_of_measurement": "%"}
        t = base
        for phase in range(phases + 1):
            on = bool(phase % 2)
            rows.append((TARGET, t + 1, "on" if on else "off", {}, None, "test", t + 1.1))
            for offset in range(0, durations[phase], 10):
                value = (55 if offset < 20 else 24) if on else 8
                rows.append((RADAR, t + offset, str(value), attrs, None, "test", t + offset + .05))
                for eid in inputs[1:]:
                    rows.append((eid, t + offset, "off", {}, None, "test", t + offset + .05))
            t += durations[phase]
        store.archive_batch(sorted(rows, key=lambda row: row[1]))
        states = {TARGET: {"entity_id": TARGET, "state": "off", "attributes": {}},
                  RADAR: {"entity_id": RADAR, "state": "8", "attributes": attrs}}
        registry = {TARGET: {"area_id": "lazienka", "device_id": "relay"},
                    RADAR: {"area_id": "lazienka", "device_id": "radar"}}
        for eid in inputs[1:]:
            states[eid] = {"entity_id": eid, "state": "off", "attributes": {}}
            registry[eid] = {"area_id": "lazienka", "device_id": "radar"}
        original = fixture.worker_job

        def job(*args, **kwargs):
            path = original(*args, **kwargs)
            data = json.loads(path.read_text())
            rules = [{"entity_id": "automation.on", "enabled": True, "action_services": ["switch.turn_on"],
                      "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": RADAR, "above": 22}]},
                     {"entity_id": "automation.off", "enabled": True, "action_services": ["switch.turn_off"],
                      "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": RADAR, "below": 12, "for_seconds": 3}]}]
            data["options"]["prediction_horizons_seconds"] = "1"
            data["train_kwargs"]["qualify"] = True
            data["options"]["tiny_mlp_supervised_training_enabled"] = neural
            data["schema_cache_item"] = {"input_fingerprint": sorted(inputs), "model": {
                "version": MultiHorizonPolicy.VERSION, "dims": 128, "actions": [0, 1], "horizons": [1],
                "schema": FeatureSchemaV12(128, inputs, feature_contract).export(), "heads": {},
                "selection_meta": {"selection_reasons": {RADAR: ["automation"]},
                                   "automation_baseline_automations": rules}}}
            data["checksum"] = descriptor_checksum(data)
            path.write_text(json.dumps(data))
            return path

        fixture.worker_job = job
        try:
            result = fixture.run_worker(root, aid, base, end, states, registry, True)
        finally:
            fixture.worker_job = original
        raw = result["store"].get_model(aid)
        agent = result["store"].get_agent(aid)
        previous = OPTIONS.get("prediction_horizons_seconds")
        previous_dims = OPTIONS.get("feature_dimensions")
        OPTIONS["prediction_horizons_seconds"] = "1"
        OPTIONS["feature_dimensions"] = int(raw["dims"])
        try:
            policy = MultiHorizonPolicy(agent, states, registry, [RADAR], raw)
        finally:
            OPTIONS["prediction_horizons_seconds"] = previous
            OPTIONS["feature_dimensions"] = previous_dims
        predictions = []
        at = end + 600
        for expected, energy, phase in [(0, 8, "empty"), (1, 55, "entry"),
                                        (1, 24, "stationary"), (1, 28, "washbasin"),
                                        (0, 8, "exit")]:
            state = {"entity_id": RADAR, "state": str(energy), "attributes": attrs}
            temporal = TemporalHistory()
            home = ContextEngine(OPTIONS)
            home.configure({**states, RADAR: state}, registry)
            temporal.home_context = home
            for offset in range(-600, 1, 10):
                sample_time = at + offset
                observed_energy = (8 if phase == "entry" and offset < 0 else
                                   24 if phase == "exit" and offset < 0 else energy)
                sample = {**state, "state": str(observed_energy), "attributes": {**attrs, "__hm_event_time": sample_time,
                          "__hm_received_time": sample_time, "__hm_quality": 1.0}}
                temporal.add(RADAR, sample_time, sample)
                home.observe(RADAR, sample, sample_time, learn=False,
                             event_ts=sample_time, received_ts=sample_time)
                for eid in inputs[1:]:
                    temporal.add(eid, sample_time, states[eid])
                    home.observe(eid, states[eid], sample_time, learn=False,
                                 event_ts=sample_time, received_ts=sample_time)
            states[RADAR] = state
            features, _, _ = policy.features(states, temporal, at)
            chosen, _, arms, *_ = policy.predict(features)
            predictions.append({"phase": phase, "energy": energy, "expected": expected,
                                "predicted": chosen["value"],
                                "means": [round(arm["mean"], 5) for arm in arms]})
            at += 60
        return {"scope": "real isolated HistoryManager + persisted Ridge; synthetic observations, no HA dispatch",
                "target": TARGET, "primary": RADAR, "predictions": predictions,
                "updates": policy.total_updates,
                "policy_contract": [policy.dims, policy.VERSION, raw.get("version"), policy.actions, agent["min_value"], agent["max_value"]],
                "raw_heads": {key: {"counts": head.get("counts"), "updates": head.get("total_updates"),
                                      "dims": head.get("dims")} for key, head in raw.get("heads", {}).items()},
                "experience_rewards": [row.get("reward") for row in result["store"].list_historical_experiences(aid, limit=3)],
                "training_quality": raw.get("_training_quality"),
                "maintenance_holdout": (agent.get("benchmark_detail") or {}).get("maintenance_holdout"),
                "frozen_onset": (agent.get("benchmark_detail") or {}).get("frozen_holdout"),
                "feature_contract": feature_contract,
                "persisted_feature_contract": raw["schema"]["feature_contract_version"],
                "worker_wall_seconds": result["wall_seconds"],
                "neural_tournament": (fixture.load_training_record(result["store"], aid) or {}).get("tournament"),
                "pass": all(row["expected"] == row["predicted"] for row in predictions)}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["pass"] else 1)
