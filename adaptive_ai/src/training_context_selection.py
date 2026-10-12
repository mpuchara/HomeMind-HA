"""Bounded pre-training assessment of automatic fast-light context.

Compare ON outcomes at presence arrivals using ambient readings from *before*
the arrival, while the target is OFF. Lamp emission is never a solar precursor.
This selects observations, not a control rule or a replacement training label.
"""
import math
from collections import deque
from statistics import median

from context import (archived_state, is_discrete_occupancy_entity, is_fast_reactive_agent, occupancy_state_bool,
                     target_value)
from fast_automation_replay import paired_numeric_baseline
from lighting_conditions import illumination_sensor, _automation_lighting_context
from radar_context import radar_context_entities, radar_role
from training_budget import TRAINING_BUDGET

ASSESSMENT_VERSION = 1
MAX_ROWS = 65536
MAX_OPPORTUNITIES = 256
OUTCOME_SECONDS = 30.0


def candidates(agent, meta, states, registry, existing=()):
    baseline = list(meta.get("automation_baseline_entities") or existing)
    siblings = radar_context_entities(agent, baseline, states, registry, limit=16)
    scores = {**(meta.get("causal_presence_scores") or {}),
              **(meta.get("causal_behaviour_scores") or {})}
    proven = [eid for eid in sorted(scores, key=lambda eid: (-scores[eid], eid))
              if scores[eid] >= .5 and eid in states][:2]
    ids = list(dict.fromkeys(baseline + proven + siblings + list(existing)))
    # Solar state is assessed alongside local photometry, never automatically used.
    if "sun.sun" in states:
        ids.append("sun.sun")
    return ids


def _number(eid, state):
    if eid == "sun.sun":
        return {"above_horizon": 1.0, "below_horizon": 0.0}.get(state.get("state"))
    try:
        value = float(state.get("state"))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _association(samples):
    result = {"samples": len(samples), "status": "insufficient_history"}
    if len(samples) < 24:
        return result
    values = sorted(row[1] for row in samples)
    cuts = {median(values), (values[0] + values[-1]) / 2}
    cuts.update(values[int((len(values) - 1) * q / 8)] for q in range(1, 8))
    rate = lambda rows: sum(row[2] for row in rows) / len(rows)
    splits = [(cut, [r for r in samples if r[1] <= cut], [r for r in samples if r[1] > cut]) for cut in sorted(cuts)]
    splits = [row for row in splits if min(len(row[1]), len(row[2])) >= 8]
    if not splits:
        return dict(result, status="uninformative_range")
    cut, low, high = max(splits, key=lambda row: rate(row[1]) - rate(row[2]))
    spread = max(row[1] for row in samples) - min(row[1] for row in samples)
    if min(len(low), len(high)) < 8 or spread < max(.5, abs(cut) * .2):
        return dict(result, status="uninformative_range")
    dark, bright = rate(low), rate(high)
    result.update(low_on_rate=round(dark, 4), high_on_rate=round(bright, 4),
                  split_value=cut, status="unproven")
    # Chronological replication reduces a one-off schedule/brightness coincidence.
    halves = (samples[:len(samples)//2], samples[len(samples)//2:])
    replicated = all(
        len(a := [r for r in half if r[1] <= cut]) >= 4
        and len(b := [r for r in half if r[1] > cut]) >= 4
        and rate(a) - rate(b) >= .4 for half in halves
    )
    if dark >= .75 and bright <= .5 and dark - bright >= .5 and replicated:
        result["status"] = "dependent"
    elif min(dark, bright) >= .9:
        result["status"] = "presence_only_in_observed_range"
    return result


def assess(rows, agent, meta, states, *, end_ts, provenance=None):
    """One streaming pass, constant state plus at most 256 arrival records.

    Delayed observations cannot become precursors; incomplete/late outcomes are
    discarded. Unknown provenance remains explicit, never guessed to be manual.
    """
    pair = paired_numeric_baseline(meta)
    driver = pair["sensor"] if pair else meta.get("primary_occupancy_sensor")
    if not pair and not is_discrete_occupancy_entity(driver, states.get(driver)):
        driver = None
    lights = [eid for eid in states if illumination_sensor(eid, states[eid]) or eid == "sun.sun"]
    samples = {eid: [] for eid in lights}
    current, received_at, readings = {}, {}, {eid: deque(maxlen=2) for eid in lights}
    previous, pending, hold_start = None, None, None
    observed, scanned, limited = 0, 0, False
    target = agent["target_entity"]

    def finish(on):
        nonlocal pending, observed
        if pending is not None:
            observed += 1
            for eid, value in pending[1].items():
                samples[eid].append((pending[0], value, int(on)))
            pending = None

    for row in rows:
        scanned += 1
        if scanned % 128 == 0:
            TRAINING_BUDGET.checkpoint("training_context_assessment")
        if scanned > MAX_ROWS or observed >= MAX_OPPORTUNITIES:
            limited = True
            break
        ts = float(row["ts"])
        if ts >= end_ts:
            break
        eid = row["entity_id"]
        received = float(row.get("received_ts") or ts)
        if received >= end_ts:
            continue
        if received > ts + .1 + 1e-6:
            # Do not certify an OFF opportunity across an unavailable target event.
            if eid == target:
                pending = None
                current.pop(target, None)
            if eid == driver:
                previous, hold_start = None, None
            continue
        if pending and ts > pending[0] + OUTCOME_SECONDS:
            value = (target_value(current.get(target), agent["target_property"])
                     if received_at.get(target, math.inf) <= ts else None)
            if value is not None and value < (agent["min_value"] + agent["max_value"]) / 2:
                finish(False)
            else:
                pending = None
        state = archived_state(row)
        if eid == target:
            value = target_value(state, agent["target_property"])
            origin = ((provenance(row) if provenance else row.get("_provenance") or {}).get("origin")
                      if pending is not None else None)
            if value is None or origin in {"own_command", "user", "user_intent", "manual", "user_manual", "user_action"}:
                pending = None
            elif pending and value >= (agent["min_value"] + agent["max_value"]) / 2:
                finish(True)
        if eid in readings:
            power = (target_value(current.get(target), agent["target_property"])
                     if received_at.get(target, math.inf) <= ts else None)
            ambient = _number(eid, state) if eid == "sun.sun" or (power is not None and power < .5) else None
            if ambient is not None and ambient < 0:
                ambient = None
            readings[eid].append((ts, ambient, received))
        if eid == driver:
            if pair:
                number = _number(eid, state)
                active = None if number is None else number > pair["on_threshold"]
            else:
                active = occupancy_state_bool(state)
            if active is None or active is False:
                hold_start = None
            if previous is False and active is True:
                hold_start = ts
            ready = hold_start is not None and ts >= hold_start + (pair["on_hold"] if pair else 0)
            if ready:
                value = (target_value(current.get(target), agent["target_property"])
                         if received_at.get(target, math.inf) <= ts else None)
                if value is not None and value < (agent["min_value"] + agent["max_value"]) / 2:
                    before = {}
                    for light, values in readings.items():
                        hit = next(((t, v) for t, v, known in reversed(values) if t < hold_start and known <= hold_start), None)
                        if hit and hit[1] is not None and hold_start - hit[0] <= 1800:
                            before[light] = hit[1]
                    if pending is None:
                        pending = (ts, before)
                hold_start = None
            previous = active
        current[eid] = state
        received_at[eid] = received
    # Last unfinished interval is deliberately not labelled OFF without follow-up.
    return {"version": ASSESSMENT_VERSION, "driver": driver,
            "opportunities": observed, "rows": min(scanned, MAX_ROWS),
            "bounded": limited, "end_ts": float(end_ts),
            "sensors": {eid: _association(values) for eid, values in samples.items()},
            "basis": "pre_arrival_target_off; chronological_training_slice"}


def select_training_context(policy, agent, states, registry, store, start_ts, end_ts):
    """Resolve an empty automatic schema once; trained feature slots stay fixed."""
    if (not is_fast_reactive_agent(agent) or "*" not in set(agent.get("input_entities") or ["*"])
            or policy.total_updates or policy.selection_meta.get("training_context_assessment")):
        return False
    from additional_signal import entities as additional_entities
    from observation_contract import FeatureSchemaV12, FEATURE_CONTRACT_VERSION
    from long_memory import selected_history_provenance
    meta = policy.selection_meta
    ids = [eid for eid in candidates(agent, meta, states, registry, policy.schema.entities)
           if eid not in policy.excluded_context_entities and eid != agent["target_entity"]]
    snapshot = {eid: states[eid] for eid in ids if eid in states}
    rows = store.archive_iter(start_ts=start_ts, end_ts=end_ts,
                              entity_ids=ids + [agent["target_entity"]], chunk_size=256)
    try:
        result = assess(rows, agent, meta, snapshot, end_ts=end_ts,
                        provenance=lambda row: selected_history_provenance(store, [row["id"]]).get(row["id"], {}))
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()
    baseline = list(meta.get("automation_baseline_entities") or [])
    protected = list(dict.fromkeys(baseline + additional_entities(agent)))
    configured = _automation_lighting_context(meta, states)
    protected = list(dict.fromkeys(protected + configured["sensors"]))
    dependent = [eid for eid, info in result["sensors"].items() if info["status"] == "dependent"]
    # Occupancy/energy/distance precede unproven photometry; use the available ninth
    # slot at dims=128 for useful radar context, without enlarging the linear model.
    behavioural = [eid for eid in ids if eid != "sun.sun" and not illumination_sensor(eid, states.get(eid))]
    selected = list(dict.fromkeys(protected + dependent + behavioural))
    schema = FeatureSchemaV12(policy.dims, selected, FEATURE_CONTRACT_VERSION)
    if not set(protected) <= set(schema.entities):
        raise ValueError("Configured training inputs exceed feature schema capacity")
    result.update(start_ts=float(start_ts), selected=list(schema.entities),
                  excluded=[eid for eid in ids if eid not in schema.entities],
                  configured_lighting=bool(configured["configured"]),
                  behavioural_scores=dict(meta.get("causal_behaviour_scores") or {}),
                  target_entity=agent["target_entity"], target_property=agent["target_property"])
    policy.schema = schema
    meta["training_context_assessment"] = result
    meta["radar_context_entities"] = [eid for eid in schema.entities if radar_role(eid, states.get(eid))]
    meta["selected_entities"] = len(schema.entities)
    return True
