"""Real isolated history training: presence AND darkness, with lamp light feedback.

Synthetic HA observations and automation metadata; no device dispatch. The probes
test the persisted model's own predictions, without a hard illumination override.
"""
import json
import tempfile
from pathlib import Path

import benchmark_feature_snapshot_reuse as fixture
from context import TemporalHistory
from context_engine import ContextEngine
from lighting_conditions import condition_tree
from observation_contract import FeatureSchemaV12, install_training_contract
from policy import MultiHorizonPolicy
from settings import OPTIONS, iso_from_ts
from training_process import descriptor_checksum

TARGET = "light.kitchen"
ENERGY = "sensor.kitchen_stationary_energy"
PRESENCE = "binary_sensor.kitchen_has_target"
LIGHT = "sensor.kitchen_light"


def run(*, additional_signal=None, motion_only=False, wildcard=False, long_stay=False):
    contract = install_training_contract()
    try:
        return _run(additional_signal=additional_signal, motion_only=motion_only, wildcard=wildcard, long_stay=long_stay)
    finally:
        contract["restore"]()


def _run(*, additional_signal=None, motion_only=False, wildcard=False, long_stay=False):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db, aid, base, _, _, _ = fixture.seed_database(root)
        store = fixture.Store(db)
        inputs = [ENERGY, PRESENCE] if motion_only else [ENERGY, PRESENCE, LIGHT]
        schema_inputs = list(dict.fromkeys(inputs + ([additional_signal["entity_id"]] if additional_signal else [])))
        if additional_signal:
            store.update_agent(aid, {"additional_signal": additional_signal})
        with store.conn() as conn:
            conn.execute("DELETE FROM entity_history")
            conn.execute("UPDATE agents SET target_entity=?, input_entities=? WHERE id=?", (TARGET, json.dumps(["*"] if wildcard else inputs), aid))
        attrs = {ENERGY: {"unit_of_measurement": "%"}, PRESENCE: {"device_class": "occupancy"}, LIGHT: {}}
        rows = [(TARGET, base-30, "off", {}, None, "test", base-30)]
        cycles, duration = 60, 180
        last_on = False
        for phase in range(cycles * 4 + 1):
            slot = phase % 4
            occupied = slot in (1, 3)
            bright = slot in (2, 3)
            on = occupied if motion_only else occupied and not bright
            t = base + phase * duration
            if on != last_on:
                rows.append((TARGET, t+1, "on" if on else "off", {}, None, "test", t+1.01))
                last_on = on
            ambient = (100 + phase % 3 * 25) if bright else (15 + phase % 3 * 5)
            for offset in range(0, duration, 5):
                values = {ENERGY: 55 if occupied and offset < 10 else 24 if occupied else 8,
                          PRESENCE: "on" if occupied else "off",
                          LIGHT: 200 if on and offset >= 5 and not motion_only else ambient}
                for eid, value in values.items():
                    rows.append((eid, t+offset, str(value), attrs[eid], None, "test", t+offset+.01))
        store.archive_batch(sorted(rows, key=lambda row: (row[1], row[0])))
        end = base + cycles*4*duration
        states = {TARGET: {"entity_id": TARGET, "state": "off", "attributes": {}}}
        for eid, value in [(ENERGY, 8), (PRESENCE, "off"), (LIGHT, 20)]:
            states[eid] = {"entity_id": eid, "state": str(value), "attributes": attrs[eid]}
        registry = {eid: {"area_id": "kitchen", "device_id": "bulb" if eid == TARGET else "radar"} for eid in states}
        on_rule = {"entity_id": "automation.kitchen_on", "enabled": True,
                   "action_services": ["light.turn_on"], "context_entities": inputs,
                   "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": ENERGY, "above": 22}],
                   "condition_tree": condition_tree([] if motion_only else [{"condition": "numeric_state", "entity_id": LIGHT, "below": 40}]),
                   "direct_on_conditions": True}
        off_rule = {"entity_id": "automation.kitchen_off", "enabled": True,
                    "action_services": ["light.turn_off"], "context_entities": [ENERGY],
                    "baseline_rules": [{"source": "trigger", "kind": "numeric_state", "entity_id": ENERGY, "below": 12, "for_seconds": 3}]}
        original = fixture.worker_job
        def job(*args, **kwargs):
            path = original(*args, **kwargs)
            data = json.loads(path.read_text())
            data["options"]["prediction_horizons_seconds"] = "1"
            data["options"]["tiny_mlp_supervised_training_enabled"] = False
            data["automation_infos"] = [on_rule, off_rule]
            data["automation_hints"] = inputs
            data["schema_cache_item"] = {"input_fingerprint": ["*"] if wildcard else sorted(inputs), "model": {
                "version": MultiHorizonPolicy.VERSION, "dims": 128, "actions": [0, 1], "horizons": [1],
                "schema": FeatureSchemaV12(128, schema_inputs).export(), "heads": {},
                "selection_meta": {"automation_baseline_candidates": inputs, "automation_baseline_entities": inputs,
                                   "automation_baseline_automations": [on_rule, off_rule]}}}
            if additional_signal:
                data["schema_cache_item"]["input_fingerprint"].append(json.dumps(additional_signal, sort_keys=True))
                data["schema_cache_item"]["model"]["selection_meta"]["additional_signal"] = additional_signal
            data["checksum"] = descriptor_checksum(data)
            path.write_text(json.dumps(data))
            return path
        fixture.worker_job = job
        try:
            result = fixture.run_worker(root, aid, base, end, states, registry, True)
        except RuntimeError as exc:
            detail = root / "result.json"
            raise RuntimeError(str(exc) + (detail.read_text() if detail.exists() else "")) from exc
        finally:
            fixture.worker_job = original
        raw = result["store"].get_model(aid)
        config = result["store"].get_agent(aid)
        saved = OPTIONS.get("prediction_horizons_seconds")
        OPTIONS["prediction_horizons_seconds"] = "1"
        try:
            policy = MultiHorizonPolicy(config, states, registry, inputs, raw)
        finally:
            OPTIONS["prediction_horizons_seconds"] = saved
        predictions = []
        at = end + 600
        scenarios = [
            (False, 20, False, False, "empty_dark"), (True, 20, False, False, "occupied_dark_entry"),
            (False, 150, False, False, "empty_bright"), (True, 150, False, False, "occupied_daylight"),
            (True, 20, True, True, "stationary_with_emitted_light")]
        if additional_signal:
            scenarios.append((True,150,True,False,"already_on_bright_held"))
        for occupied, ambient, target_on, emitted, name in scenarios:
            temporal = TemporalHistory(maxlen=256)
            probe = dict(states)
            home = ContextEngine(OPTIONS)
            home.configure(states, registry)
            temporal.home_context = home
            for offset in range(-120, 1, 5):
                for eid, value in [(TARGET, "on" if target_on and offset >= -60 else "off"),
                                   (ENERGY, 24 if occupied else 8), (PRESENCE, "on" if occupied else "off"),
                                   (LIGHT, 200 if emitted and offset >= -55 else ambient)]:
                    stamp = at + offset
                    st = {"entity_id": eid, "state": str(value), "attributes": {
                        **attrs.get(eid, {}), "__hm_received_time": stamp, "__hm_event_time": stamp, "__hm_quality": 1}}
                    temporal.add(eid, stamp, st)
                    home.observe(eid, st, stamp, learn=False, event_ts=stamp, received_ts=stamp)
                    probe[eid] = st
            features, _, _ = policy.features(probe, temporal, at)
            chosen, _, arms, *_ = policy.predict(features)
            expected = int(occupied and ambient < 40)
            desired = chosen["value"]
            if additional_signal:
                from additional_signal import evaluate, apply
                # The configured ON preference is separate from learned history.
                evidence = evaluate(config,probe,at,temporal)
                desired, _ = apply(config,int(target_on),desired,evidence)
                if target_on: expected = int(occupied)
            predictions.append({"phase": name, "expected": expected, "predicted": desired,
                                "raw_prediction": chosen["value"],
                                "means": [arm["mean"] for arm in arms],
                                "values": {str(i): features.get(i) for i in (5, 17, 29)}})
            at += 180
        long_probe = None
        if long_stay:
            temporal = TemporalHistory(maxlen=24)
            home = ContextEngine(OPTIONS); home.configure(states, registry)
            temporal.home_context = home
            probe = dict(states); observed = []; diagnostics = []
            for offset in range(-2, 601):
                stamp = at + offset
                values = [(TARGET, 'on' if offset >= 0 else 'off'), (ENERGY, 24),
                          (PRESENCE, 'on'), (LIGHT, 180+offset%30 if offset >= 1 else 20)]
                for eid, value in values:
                    st = dict(entity_id=eid, state=str(value), last_changed=iso_from_ts(at if eid==TARGET and offset>=0 else stamp),
                              last_updated=iso_from_ts(stamp), attributes={**attrs.get(eid, {}),
                              '__hm_received_time': stamp, '__hm_event_time': stamp, '__hm_quality': 1})
                    temporal.add(eid, stamp, st)
                    home.observe(eid, st, stamp, learn=False, event_ts=stamp, received_ts=stamp)
                    probe[eid] = st
                features, labels, meta = policy.features(probe, temporal, stamp+.01)
                chosen, _, arms, *_ = policy.predict(features)
                if offset in (30,120,180,200,240,300,600):
                    diagnostics.append(dict(offset=offset, desired=chosen['value'],
                        means=[arm['mean'] for arm in arms],
                        features={str(i)+':'+','.join(labels.get(i,[])):v for i,v in features.items()},
                        home=meta.get('home_forecast')))
                if offset >= 30:
                    observed.append(dict(offset=offset, desired=chosen['value'],
                        illumination_valid=meta['entity_observations'][LIGHT]['valid']))
            long_probe = dict(seconds=600, raw_history_limit=24, observations=len(observed),
                off_predictions=sum(r['desired'] < .5 for r in observed),
                invalid_illumination=sum(not r['illumination_valid'] for r in observed),
                diagnostics=diagnostics,
                pass_all=all(r['desired'] >= .5 and r['illumination_valid'] for r in observed))
        return {"scope": "isolated HistoryManager and persisted policy, synthetic data, no HA dispatch",
                "predictions": predictions, "training_quality": raw.get("_training_quality"),
                "additional_signal_objective": raw.get("_benchmark_counts",{}).get("additional_signal_objective"),
                "persisted_feature_contract": raw["schema"]["feature_contract_version"],
                "context_assessment": raw.get("selection_meta", {}).get("training_context_assessment"),
                "updates": policy.total_updates, "worker_seconds": result["wall_seconds"],
                "classifier": {k: v for k, v in raw["heads"]["1"].get("binary_state_classifier", {}).items() if k in ("ready", "fits", "seen")},
                "long_stay": long_probe,
                "pass": all(p["expected"] == p["predicted"] for p in predictions) and (long_probe is None or long_probe['pass_all'])}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--long-stay', action='store_true')
    report = run(long_stay=parser.parse_args().long_stay)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["pass"] else 1)
