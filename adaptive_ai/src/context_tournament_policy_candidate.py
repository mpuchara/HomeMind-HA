"""Stage-09 Sensor Tournament: broad screening, exact deployable policy validation.

Historical relevance and semantic associations are screening signals only. They decide
what is worth testing, never what is causal and never what may be promoted. Promotion
still requires paired future/prequential predictive gain.

For F15 every challenger is a real ``MultiHorizonPolicy`` cloned from the champion and
migrated to the exact target schema. The clone predicts in Shadow, is scored on the same
future outcome as the champion, learns only after that paired score, and the trained clone
is the object copied into Live if all gates pass. No ActionIntent/Executor/HA call exists
in this module.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict

import context_tournament_promotion as promotion
from context import action_values, context_scalar, target_value
from context_tournament_metrics import PREQUENTIAL_EPOCH_VERSION, metric_row
from context_tournament_primary_protection import primary_feature_ids, primary_replacement_gain
from manual_context_learning import _migrate_schema
from policy import MultiHorizonPolicy
from settings import OPTIONS


CONTRACT_VERSION = 2
POOL_VERSION = 1
MAX_HISTORY = 32
MAX_SCREENING_SAMPLES = 96
DEFAULT_POOL_LIMIT = 96
MIN_SCREENING_SAMPLES = 8
MIN_HEALTH_OPPORTUNITIES = 20
LEAK_WINDOW_SECONDS = 5.0
LEAK_BLOCK_RATE = 0.35
REDUNDANCY_THRESHOLD = 0.97
MIN_REDUNDANCY_SAMPLES = 12
MULTIPLE_TESTING_GAIN_STEP = 0.0015
FEATURE_COST_GAIN_SCALE = 0.020
FLOAT_EPSILON = 1e-12


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _decode(raw, fallback):
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _corr(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if _finite(x) is not None and _finite(y) is not None]
    if len(pairs) < 3:
        return 0.0
    ax = [x for x, _ in pairs]
    ay = [y for _, y in pairs]
    mx, my = sum(ax) / len(ax), sum(ay) / len(ay)
    vx = sum((x - mx) ** 2 for x in ax)
    vy = sum((y - my) ** 2 for y in ay)
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))


def semantic_predictive_score(samples, min_samples=MIN_SCREENING_SAMPLES):
    """Screen value/quality/lags/trend/edge-age and selected pair interactions."""
    rows = [dict(x) for x in (samples or []) if isinstance(x, dict)]
    if len(rows) < int(min_samples):
        return {"score": 0.0, "best_group": None, "samples": len(rows),
                "groups": {}, "status": "needs_more_data"}
    labels = [row.get("label") for row in rows]
    groups = defaultdict(list)
    for row in rows:
        for key in ("value", "quality", "lag_1", "lag_3", "lag_10", "trend", "time_since_edge"):
            groups[key].append(row.get(key))
        for key, value in sorted((row.get("interactions") or {}).items()):
            groups[f"interaction:{key}"].append(value)
    scores = {}
    for key, values in groups.items():
        paired = [(x, y) for x, y in zip(values, labels)
                  if _finite(x) is not None and _finite(y) is not None]
        if len(paired) < int(min_samples):
            continue
        raw = abs(_corr([x for x, _ in paired], [y for _, y in paired]))
        coverage = min(1.0, len(paired) / max(1.0, float(min_samples * 2)))
        scores[key] = raw * coverage
    if not scores:
        return {"score": 0.0, "best_group": None, "samples": len(rows),
                "groups": {}, "status": "neutral"}
    best_group = max(scores, key=lambda key: (scores[key], key))
    best = max(0.0, min(1.0, float(scores[best_group])))
    return {"score": best, "best_group": best_group, "samples": len(rows),
            "groups": {k: round(float(v), 6) for k, v in sorted(scores.items())},
            "status": "screened" if best > 0.05 else "neutral"}


def redundancy_score(samples_a, samples_b):
    by_episode = {}
    for row in samples_a or []:
        if isinstance(row, dict) and row.get("episode") is not None:
            value = _finite(row.get("value"))
            if value is not None:
                by_episode[str(row["episode"])] = value
    xs, ys = [], []
    for row in samples_b or []:
        if not isinstance(row, dict) or row.get("episode") is None:
            continue
        value = _finite(row.get("value"))
        key = str(row["episode"])
        if value is not None and key in by_episode:
            xs.append(by_episode[key]); ys.append(value)
    return {"score": abs(_corr(xs, ys)) if len(xs) >= MIN_REDUNDANCY_SAMPLES else None,
            "samples": len(xs)}


def multiple_testing_penalty(test_count, step=MULTIPLE_TESTING_GAIN_STEP):
    return min(0.02, max(0.0, float(step)) * math.log2(max(1, int(test_count or 0)) + 1.0))


def feature_cost_penalty(active_schema, target_schema, event_frequency=0.0):
    before, after = len(list(active_schema or [])), len(list(target_schema or []))
    dims = max(16, int(OPTIONS.get("feature_dimensions", 128)))
    slot_cost = 4.0 * max(1, after - before) / float(dims)
    frequency_cost = min(0.004, 0.0008 * math.log1p(max(0.0, float(event_frequency or 0.0))))
    return min(0.012, FEATURE_COST_GAIN_SCALE * slot_cost + frequency_cost)


def sensor_health_from_row(row):
    row = dict(row or {})
    opportunities = int(row.get("opportunities") or 0)
    available = int(row.get("available_count") or 0)
    unknown = int(row.get("unknown_count") or 0)
    unavailable = int(row.get("unavailable_count") or 0)
    changes = int(row.get("change_count") or 0)
    leaks = int(row.get("own_action_leak_hits") or 0)
    availability = available / opportunities if opportunities else None
    failure_rate = (unknown + unavailable) / opportunities if opportunities else None
    health = None if availability is None else max(
        0.0, min(1.0, availability * (1.0 - 0.35 * (failure_rate or 0.0)))
    )
    first, last = _finite(row.get("first_seen_ts")), _finite(row.get("last_seen_ts"))
    return {
        "opportunities": opportunities,
        "availability": availability,
        "failure_rate": failure_rate,
        "health": health,
        "health_ready": opportunities >= MIN_HEALTH_OPPORTUNITIES,
        "change_count": changes,
        "own_action_leak_hits": leaks,
        "own_action_leak_rate": (leaks / changes) if changes else None,
        "observed_days": 0.0 if first is None or last is None else max(0.0, (last - first) / 86400.0),
    }


def effective_required_gain(*, base_gain, primary=False, primary_broken=False,
                            test_count=1, active_schema=None, target_schema=None,
                            event_frequency=0.0, health=None):
    base = max(0.0, float(base_gain or 0.0))
    if primary and not primary_broken:
        base = max(base, float(primary_replacement_gain()))
    multiplicity = multiple_testing_penalty(test_count)
    cost = feature_cost_penalty(active_schema, target_schema, event_frequency)
    health_penalty = max(0.0, 0.9 - float(health)) * 0.01 if health is not None else 0.0
    return {"required_gain": base + multiplicity + cost + health_penalty,
            "base_gain": base, "multiple_testing_penalty": multiplicity,
            "feature_cost_penalty": cost, "health_penalty": health_penalty}


def plan_target_schema(agent, policy, challenger, tournament, health_lookup=None):
    """Freeze the exact schema to be trained and, eventually, deployed."""
    active = [str(x) for x in (getattr(policy.schema, "entities", []) or [])]
    challenger = str(challenger)
    if challenger in active:
        return {"schema": active, "replaced": None, "replacement_is_primary": False,
                "primary_broken": False, "reason": "already_active"}
    limit = promotion._selection_limit(agent)
    if len(active) < limit:
        return {"schema": active + [challenger], "replaced": None,
                "replacement_is_primary": False, "primary_broken": False, "reason": "append"}
    explicit = {str(x) for x in (agent.get("input_entities") or [])}
    primary = primary_feature_ids(policy)
    scores = dict((tournament or {}).get("feature_scores") or {})
    removable = []
    for idx, entity_id in enumerate(active):
        if entity_id in explicit:
            continue
        health = dict((health_lookup(entity_id) if callable(health_lookup) else {}) or {})
        broken = bool(health.get("health_ready") and health.get("availability") is not None
                      and float(health["availability"]) < 0.50)
        is_primary = entity_id in primary
        priority = 0 if broken else (2 if is_primary else 1)
        removable.append((priority, float(scores.get(entity_id, 0.0)), -idx,
                          entity_id, is_primary, broken, idx))
    if not removable:
        return {"schema": None, "replaced": None, "replacement_is_primary": False,
                "primary_broken": False, "reason": "no_replaceable_schema_slot"}
    _, _, _, replaced, is_primary, broken, idx = min(removable)
    result = list(active); result[idx] = challenger
    return {"schema": result, "replaced": replaced, "replacement_is_primary": bool(is_primary),
            "primary_broken": bool(broken),
            "reason": "replace_broken_primary" if is_primary and broken else "replace"}


def promotion_gate(*, model, gain, health, redundancy, duplicate_of, test_count,
                   active_schema, target_schema, replacement_is_primary=False,
                   primary_broken=False, event_frequency=0.0, base_gain=None):
    health = dict(health or {})
    req = effective_required_gain(
        base_gain=OPTIONS.get("context_tournament_min_gain", 0.03) if base_gain is None else base_gain,
        primary=replacement_is_primary, primary_broken=primary_broken,
        test_count=test_count, active_schema=active_schema, target_schema=target_schema,
        event_frequency=event_frequency, health=health.get("health"),
    )
    leak_rate = health.get("own_action_leak_rate")
    leak_blocked = bool(health.get("change_count", 0) >= 5 and leak_rate is not None
                        and float(leak_rate) >= LEAK_BLOCK_RATE)
    redundancy_blocked = bool(redundancy is not None and float(redundancy) >= REDUNDANCY_THRESHOLD
                              and duplicate_of)
    health_ok = bool(health.get("health_ready") and health.get("availability") is not None
                     and float(health["availability"]) >= 0.80)
    samples = int((model or {}).get("candidate_training_samples") or 0)
    exact_ready = bool((model or {}).get("candidate_policy") and (model or {}).get("candidate_target_schema"))
    gain_ok = gain is not None and float(gain) + FLOAT_EPSILON >= float(req["required_gain"])
    ready = health_ok and not leak_blocked and not redundancy_blocked and exact_ready and samples > 0 and gain_ok
    if not health_ok:
        reason = "sensor_health"
    elif leak_blocked:
        reason = "own_action_leakage"
    elif redundancy_blocked:
        reason = "redundant_sensor"
    elif not exact_ready:
        reason = "candidate_policy_missing"
    elif samples <= 0:
        reason = "candidate_policy_untrained"
    elif not gain_ok:
        reason = "predictive_gain"
    else:
        reason = None
    return {"ready": bool(ready), "reason": reason, "predictive_gain": gain,
            "duplicate_of": duplicate_of, "redundancy": redundancy,
            "health": health, "candidate_training_samples": samples, **req}


def install_policy_candidates(service):
    if getattr(service, "_policy_candidate_installed", False):
        return service

    with service.store.lock, service.store.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS context_tournament_observed_pool (
            agent_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            pool_version INTEGER NOT NULL DEFAULT 1,
            opportunities INTEGER NOT NULL DEFAULT 0,
            available_count INTEGER NOT NULL DEFAULT 0,
            unknown_count INTEGER NOT NULL DEFAULT 0,
            unavailable_count INTEGER NOT NULL DEFAULT 0,
            change_count INTEGER NOT NULL DEFAULT 0,
            own_action_leak_hits INTEGER NOT NULL DEFAULT 0,
            first_seen_ts REAL, last_seen_ts REAL, last_change_ts REAL, last_value REAL,
            history_json TEXT NOT NULL DEFAULT '[]', samples_json TEXT NOT NULL DEFAULT '[]',
            screening_json TEXT NOT NULL DEFAULT '{}', updated_ts REAL NOT NULL,
            PRIMARY KEY(agent_id, entity_id));
        CREATE INDEX IF NOT EXISTS idx_context_observed_pool_agent_updated
            ON context_tournament_observed_pool(agent_id,updated_ts DESC);
        """)

    lock = threading.RLock()
    pool_cache, pool_members = {}, {}
    current_context, candidate_cache, pending_training = {}, {}, {}
    last_target, last_own_action = {}, {}
    tls = threading.local()

    original_state = service.state
    original_sync = service.sync_agent
    original_observe = service.observe_shadow
    original_status = service.shadow_status
    original_load = service._load_shadow_model
    original_score = service._score_shadow_sample
    original_shadow_predict = service._shadow_predict_index
    base_chooser = promotion._choose_schema_after_promotion
    base_migrate = promotion._migrate_schema

    def blank_row():
        return {"opportunities": 0, "available_count": 0, "unknown_count": 0,
                "unavailable_count": 0, "change_count": 0, "own_action_leak_hits": 0,
                "first_seen_ts": None, "last_seen_ts": None, "last_change_ts": None,
                "last_value": None, "history": [], "samples": [], "screening": {}}

    def load_pool(agent_id, entity_id):
        key = (str(agent_id), str(entity_id))
        with lock:
            if key in pool_cache:
                return pool_cache[key]
        with service.store.conn() as c:
            row = c.execute("SELECT * FROM context_tournament_observed_pool WHERE agent_id=? AND entity_id=?", key).fetchone()
        value = blank_row()
        if row:
            for field in ("opportunities", "available_count", "unknown_count", "unavailable_count",
                          "change_count", "own_action_leak_hits"):
                value[field] = int(row[field] or 0)
            for field in ("first_seen_ts", "last_seen_ts", "last_change_ts", "last_value"):
                value[field] = row[field]
            value["history"] = _decode(row["history_json"], [])
            value["samples"] = _decode(row["samples_json"], [])
            value["screening"] = _decode(row["screening_json"], {})
            if not isinstance(value["history"], list): value["history"] = []
            if not isinstance(value["samples"], list): value["samples"] = []
            if not isinstance(value["screening"], dict): value["screening"] = {}
        with lock:
            pool_cache[key] = value
        return value

    def save_pool(agent_id, entity_id, row, now):
        key = (str(agent_id), str(entity_id))
        row["history"] = list(row.get("history") or [])[-MAX_HISTORY:]
        row["samples"] = list(row.get("samples") or [])[-MAX_SCREENING_SAMPLES:]
        row["screening"] = semantic_predictive_score(row["samples"])
        args = (
            key[0], key[1], POOL_VERSION, int(row.get("opportunities") or 0),
            int(row.get("available_count") or 0), int(row.get("unknown_count") or 0),
            int(row.get("unavailable_count") or 0), int(row.get("change_count") or 0),
            int(row.get("own_action_leak_hits") or 0), row.get("first_seen_ts"),
            row.get("last_seen_ts"), row.get("last_change_ts"), row.get("last_value"),
            json.dumps(row.get("history") or [], separators=(",", ":"), sort_keys=True),
            json.dumps(row.get("samples") or [], separators=(",", ":"), sort_keys=True),
            json.dumps(row.get("screening") or {}, separators=(",", ":"), sort_keys=True), float(now),
        )
        with service.store.lock, service.store.conn() as c:
            c.execute("""INSERT INTO context_tournament_observed_pool
                (agent_id,entity_id,pool_version,opportunities,available_count,unknown_count,
                 unavailable_count,change_count,own_action_leak_hits,first_seen_ts,last_seen_ts,
                 last_change_ts,last_value,history_json,samples_json,screening_json,updated_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id,entity_id) DO UPDATE SET
                 pool_version=excluded.pool_version,opportunities=excluded.opportunities,
                 available_count=excluded.available_count,unknown_count=excluded.unknown_count,
                 unavailable_count=excluded.unavailable_count,change_count=excluded.change_count,
                 own_action_leak_hits=excluded.own_action_leak_hits,first_seen_ts=excluded.first_seen_ts,
                 last_seen_ts=excluded.last_seen_ts,last_change_ts=excluded.last_change_ts,
                 last_value=excluded.last_value,history_json=excluded.history_json,
                 samples_json=excluded.samples_json,screening_json=excluded.screening_json,
                 updated_ts=excluded.updated_ts""", args)
        with lock:
            pool_cache[key] = row

    def pool_stats(agent_id, entity_id):
        row = load_pool(agent_id, entity_id)
        health = sensor_health_from_row(row)
        first, last = _finite(row.get("first_seen_ts")), _finite(row.get("last_seen_ts"))
        hours = 1.0 if first is None or last is None else max(1.0, (last - first) / 3600.0)
        return {**health, "screening": dict(row.get("screening") or semantic_predictive_score(row.get("samples") or [])),
                "event_frequency": float(row.get("change_count") or 0) / hours,
                "screening_samples": len(row.get("samples") or [])}

    def all_pool_rows(agent_id):
        aid = str(agent_id)
        limit = max(32, int(OPTIONS.get("context_observed_pool_limit", DEFAULT_POOL_LIMIT)) * 2)
        with service.store.conn() as c:
            ids = [str(row[0]) for row in c.execute(
                "SELECT entity_id FROM context_tournament_observed_pool WHERE agent_id=? ORDER BY updated_ts DESC LIMIT ?",
                (aid, limit)).fetchall()]
        out = []
        for entity_id in ids:
            stats = pool_stats(aid, entity_id)
            raw = load_pool(aid, entity_id)
            out.append({"entity_id": entity_id, **stats,
                        "last_change_ts": raw.get("last_change_ts"), "last_value": raw.get("last_value")})
        return out

    def combined_scores(agent, base_scores=None):
        out = {}
        for eid, value in (base_scores or {}).items():
            value = _finite(value)
            if value is not None:
                out[str(eid)] = max(0.0, min(1.0, value))
        for row in all_pool_rows(agent["id"]):
            score = _finite((row.get("screening") or {}).get("score")) or 0.0
            if score > out.get(row["entity_id"], 0.0):
                out[row["entity_id"]] = min(1.0, score)
        return out

    def choose_members(agent, tournament, states):
        aid = str(agent["id"])
        active = [str(x) for x in tournament.get("active_features") or []]
        eligible = set(service._eligible_entities(agent, active) or [])
        scores = dict(tournament.get("feature_scores") or {})
        must = active + [str(x) for x in tournament.get("challenger_features") or []]
        ranked = sorted(eligible, key=lambda eid: (-float(scores.get(eid, 0.0)), eid))
        limit = max(16, int(OPTIONS.get("context_observed_pool_limit", DEFAULT_POOL_LIMIT)))
        members = []
        for eid in must + ranked:
            if eid in states and eid not in members:
                members.append(eid)
            if len(members) >= limit:
                break
        with lock:
            pool_members[aid] = tuple(members)
        return members

    def sync_with_pool(agent, **kwargs):
        base = kwargs.get("feature_scores")
        if base is None:
            base = getattr(service.engine, "context_relevance", {}).get(agent["id"]) or {}
        kwargs["feature_scores"] = combined_scores(agent, base)
        state = original_sync(agent, **kwargs)
        with getattr(service.engine, "lock", threading.RLock()):
            states = dict(getattr(service.engine, "state_map", {}) or {})
        choose_members(agent, state, states)
        return state

    service.sync_agent = sync_with_pool

    def history_value(history, cutoff):
        values = [(float(ts), float(value)) for ts, value in (history or []) if float(ts) <= float(cutoff)]
        return values[-1][1] if values else None

    def update_pool(agent, states, changed_entities, now):
        aid = str(agent["id"])
        tournament = original_state(aid)
        members = list(pool_members.get(aid) or ()) or choose_members(agent, tournament, states)
        rt = (getattr(service.engine, "runtime", {}) or {}).get(aid) or {}
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        previous = last_target.get(aid)
        origin = str(rt.get("last_change_origin") or "")
        target_changed = bool(current is not None and previous is not None and
                              abs(float(current) - float(previous)) > max(0.01, float(agent.get("deadband") or 0.01) * 0.05))
        if target_changed and origin == "own_command":
            last_own_action[aid] = float(now)
        independent = bool(target_changed and origin != "own_command")
        actions = [float(x) for x in action_values(agent)]
        label = None
        if independent and actions:
            actual_idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(current)))
            label = actual_idx / max(1.0, float(len(actions) - 1))
        episode = f"{aid}:{int(getattr(service.engine, 'state_revision', 0))}:{int(now * 1000)}"

        policy = (getattr(service.engine, "models", {}) or {}).get(aid)
        active = list(getattr(getattr(policy, "schema", None), "entities", []) or []) if policy else []
        ordered = []
        if policy is not None:
            meta = dict(getattr(policy, "selection_meta", {}) or {})
            for key in ("primary_occupancy_sensor", "primary_local_sensor"):
                if meta.get(key): ordered.append(str(meta[key]))
        ordered.extend(eid for eid in active if eid not in ordered)
        interaction_values = {}
        for eid in ordered[:3]:
            value = _finite(context_scalar(eid, states.get(eid), agent))
            if value is not None:
                interaction_values[eid] = value

        own_ts = last_own_action.get(aid)
        for entity_id in members:
            row = load_pool(aid, entity_id)
            row["opportunities"] = int(row.get("opportunities") or 0) + 1
            if row.get("first_seen_ts") is None:
                row["first_seen_ts"] = float(now)
            state = states.get(entity_id)
            text = str((state or {}).get("state") or "").strip().lower()
            value = _finite(context_scalar(entity_id, state, agent)) if state is not None else None
            available = bool(state) and text not in ("", "unknown", "unavailable", "none") and value is not None
            if available:
                row["available_count"] = int(row.get("available_count") or 0) + 1
                row["last_seen_ts"] = float(now)
                previous_value = _finite(row.get("last_value"))
                if previous_value is None:
                    row["last_value"] = value
                    row["last_change_ts"] = float(now)
                    row["history"] = (list(row.get("history") or []) + [[float(now), value]])[-MAX_HISTORY:]
                elif abs(value - previous_value) > 1e-9:
                    row["change_count"] = int(row.get("change_count") or 0) + 1
                    row["last_change_ts"] = float(now); row["last_value"] = value
                    row["history"] = (list(row.get("history") or []) + [[float(now), value]])[-MAX_HISTORY:]
                    if own_ts is not None and 0.0 <= now - float(own_ts) <= LEAK_WINDOW_SECONDS:
                        row["own_action_leak_hits"] = int(row.get("own_action_leak_hits") or 0) + 1
            elif text in ("unknown", "none", ""):
                row["unknown_count"] = int(row.get("unknown_count") or 0) + 1
            else:
                row["unavailable_count"] = int(row.get("unavailable_count") or 0) + 1

            if independent and label is not None and value is not None:
                history = list(row.get("history") or [])
                last_edge = _finite(row.get("last_change_ts"))
                sample = {
                    "episode": episode, "ts": float(now), "label": float(label), "value": value,
                    "quality": sensor_health_from_row(row).get("health"),
                    "lag_1": history_value(history, now - 1.0),
                    "lag_3": history_value(history, now - 3.0),
                    "lag_10": history_value(history, now - 10.0),
                    "time_since_edge": None if last_edge is None else math.exp(-max(0.0, now - last_edge) / 30.0),
                    "interactions": {eid: value * other for eid, other in interaction_values.items() if eid != entity_id},
                }
                sample["trend"] = None if sample["lag_10"] is None else value - float(sample["lag_10"])
                row["samples"] = (list(row.get("samples") or []) + [sample])[-MAX_SCREENING_SAMPLES:]
            save_pool(aid, entity_id, row, now)
        if current is not None:
            last_target[aid] = float(current)
        return independent

    def candidate_plan(agent, challenger, policy, tournament):
        return plan_target_schema(agent, policy, challenger, tournament,
                                  health_lookup=lambda eid: pool_stats(agent["id"], eid))

    def candidate_from_model(agent, challenger, model, policy=None, tournament=None):
        aid = str(agent["id"])
        policy = policy or (getattr(service.engine, "models", {}) or {}).get(aid)
        if policy is None:
            return None
        tournament = dict(tournament or original_state(aid))
        requested = list(model.get("candidate_requested_schema") or [])
        if requested:
            plan = {"schema": requested, "replaced": model.get("candidate_requested_replaced"),
                    "replacement_is_primary": bool(model.get("candidate_requested_primary")),
                    "primary_broken": bool(model.get("candidate_requested_primary_broken"))}
        else:
            plan = candidate_plan(agent, challenger, policy, tournament)
        target_schema = list(plan.get("schema") or [])
        if not target_schema or str(challenger) not in set(target_schema):
            model["candidate_blocked_reason"] = "no_target_schema"
            return None
        current_revision = str(getattr(policy, "model_revision", "") or "")
        expected_revision = str(model.get("evaluation_champion_revision") or current_revision)
        current_policy_version = int(getattr(policy, "VERSION", 0) or 0)
        current_schema_version = int(getattr(policy.schema, "VERSION", 0) or 0)
        data_version = dict(model.get("candidate_data_version") or {})
        raw = model.get("candidate_policy") if isinstance(model.get("candidate_policy"), dict) else None
        valid = bool(
            raw
            and current_revision == expected_revision
            and list((raw.get("schema") or {}).get("entities") or []) == target_schema
            and int(raw.get("version") or 0) == current_policy_version
            and int((raw.get("schema") or {}).get("version") or 0) == current_schema_version
            and str(model.get("candidate_source_model_revision") or "") == expected_revision
            and int(model.get("candidate_contract_version") or 0) == CONTRACT_VERSION
            and str(data_version.get("contract") or "") == "paired_future_policy_v2"
            and int(data_version.get("schema_revision") or -1) == int(tournament.get("schema_revision") or 0)
            and str(data_version.get("champion_revision") or "") == expected_revision
            and int(data_version.get("policy_version") or 0) == current_policy_version
            and int(data_version.get("feature_schema_version") or 0) == current_schema_version
        )
        key = (aid, str(challenger))
        with getattr(service.engine, "lock", threading.RLock()):
            states = dict(getattr(service.engine, "state_map", {}) or {})
            registry = dict(getattr(service.engine, "entity_registry", {}) or {})
        if valid:
            cached = candidate_cache.get(key)
            if cached is not None and str(cached.model_revision) == str(raw.get("model_revision") or ""):
                return cached
            candidate = MultiHorizonPolicy(agent, states, registry, set(), model=raw,
                                           relevance_scores=None, context_engine=getattr(service.engine, "context", None))
            candidate_cache[key] = candidate
            return candidate
        candidate = MultiHorizonPolicy(agent, states, registry, set(), model=policy.serialize(),
                                       relevance_scores=None, context_engine=getattr(service.engine, "context", None))
        meta = promotion._selection_meta_for_promotion(candidate, str(challenger), plan.get("replaced"))
        migration = _migrate_schema(candidate, target_schema, meta)
        model.update({
            "candidate_contract_version": CONTRACT_VERSION,
            "candidate_source_model_revision": expected_revision,
            "candidate_target_schema": list(candidate.schema.entities),
            "candidate_replaced_entity": plan.get("replaced"),
            "candidate_replacement_is_primary": bool(plan.get("replacement_is_primary")),
            "candidate_primary_broken": bool(plan.get("primary_broken")),
            "candidate_policy": candidate.serialize(),
            "candidate_model_revision": candidate.model_revision,
            "candidate_training_samples": 0,
            "candidate_migration": migration,
            "candidate_data_version": {
                "contract": "paired_future_policy_v2",
                "schema_revision": int(tournament.get("schema_revision") or 0),
                "champion_revision": expected_revision,
                "policy_version": int(getattr(candidate, "VERSION", 0)),
                "feature_schema_version": int(getattr(candidate.schema, "VERSION", 0)),
            },
        })
        for key_name in ("candidate_requested_schema", "candidate_requested_replaced",
                         "candidate_requested_primary", "candidate_requested_primary_broken"):
            model.pop(key_name, None)
        candidate_cache[key] = candidate
        service._save_shadow_model(aid, challenger, model)
        return candidate

    def load_with_key(agent_id, challenger, action_count):
        model = original_load(agent_id, challenger, action_count)
        tls.shadow_key = (str(agent_id), str(challenger))
        return model

    service._load_shadow_model = load_with_key

    def predict_exact(model, active_idx, bucket):
        key = getattr(tls, "shadow_key", None)
        if not key:
            return original_shadow_predict(model, active_idx, bucket)
        aid, challenger = key
        context = current_context.get(aid)
        if not context:
            return original_shadow_predict(model, active_idx, bucket)
        pending_training.pop(key, None)
        agent = context["agent"]
        candidate = candidate_from_model(agent, challenger, model,
                                         policy=(getattr(service.engine, "models", {}) or {}).get(aid),
                                         tournament=original_state(aid))
        temporal = getattr(service.engine, "temporal_history", None)
        if candidate is None or temporal is None:
            return int(active_idx)
        try:
            features, _, _ = candidate.features(context["states"], temporal, at_ts=context["now"])
            chosen, confidence, _, horizon, support, novelty = candidate.predict(features)
            actions = [float(x) for x in action_values(agent)]
            idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(chosen["value"])))
            pending_training[key] = {
                "features": dict(features), "horizon": int(horizon), "ts": float(context["now"]),
                "state_revision": int(getattr(service.engine, "state_revision", 0)),
                "candidate_schema": list(candidate.schema.entities), "confidence": float(confidence),
                "support": float(support), "novelty": float(novelty),
                "candidate_model_revision": str(candidate.model_revision),
                "champion_revision": str((model.get("candidate_data_version") or {}).get("champion_revision") or ""),
                "candidate_data_version": json.loads(json.dumps(model.get("candidate_data_version") or {})),
            }
            return int(idx)
        except Exception as exc:
            model["candidate_blocked_reason"] = f"candidate_prediction:{type(exc).__name__}"
            return int(active_idx)

    service._shadow_predict_index = predict_exact

    def score_and_train(agent_id, challenger, pending, actual_idx, action_count, now):
        result = original_score(agent_id, challenger, pending, actual_idx, action_count, now)
        key = (str(agent_id), str(challenger))
        train = pending_training.get(key); context = current_context.get(str(agent_id))
        if not train or not context:
            return result
        agent = context["agent"]
        model = original_load(agent_id, challenger, action_count)
        candidate = candidate_from_model(agent, challenger, model, tournament=original_state(agent_id))
        try:
            live_policy = (getattr(service.engine, "models", {}) or {}).get(str(agent_id))
            live_revision = str(getattr(live_policy, "model_revision", "") or "")
            model_version = dict(model.get("candidate_data_version") or {})
            train_version = dict(train.get("candidate_data_version") or {})
            version_match = bool(
                model_version
                and model_version == train_version
                and str(model_version.get("champion_revision") or "") == str(train.get("champion_revision") or "")
                and str(model.get("evaluation_champion_revision") or "") == str(train.get("champion_revision") or "")
                and live_revision == str(train.get("champion_revision") or "")
                and int(model_version.get("schema_revision") or -1)
                    == int((original_state(agent_id) or {}).get("schema_revision") or 0)
                and int(model_version.get("policy_version") or 0) == int(getattr(candidate, "VERSION", 0) or 0)
                and int(model_version.get("feature_schema_version") or 0)
                    == int(getattr(getattr(candidate, "schema", None), "VERSION", 0) or 0)
            )
            if (candidate is None
                    or list(candidate.schema.entities) != list(train.get("candidate_schema") or [])
                    or not version_match):
                model["candidate_blocked_reason"] = "paired_data_version_changed"
                service._save_shadow_model(agent_id, challenger, model)
                return result
            horizon, features = int(train.get("horizon") or min(candidate.horizons)), dict(train.get("features") or {})
            if horizon not in candidate.heads or not features:
                return result
            candidate.heads[horizon].validate(int(actual_idx), features, 1.0, sample_ts=train.get("ts"))
            candidate.update(horizon, int(actual_idx), features, 1.0, sample_ts=train.get("ts"))
            model["candidate_policy"] = candidate.serialize()
            model["candidate_model_revision"] = candidate.model_revision
            model["candidate_training_samples"] = int(model.get("candidate_training_samples") or 0) + 1
            model["candidate_last_training_ts"] = float(now)
            model["candidate_last_episode_state_revision"] = int(train.get("state_revision") or 0)
            service._save_shadow_model(agent_id, challenger, model)
            candidate_cache[key] = candidate
            return result
        finally:
            pending_training.pop(key, None)

    service._score_shadow_sample = score_and_train

    def duplicate_status(agent, challenger, active):
        source = load_pool(agent["id"], challenger).get("samples") or []
        best = {"score": None, "samples": 0, "entity_id": None}
        for entity_id in active:
            result = redundancy_score(source, load_pool(agent["id"], entity_id).get("samples") or [])
            if result["score"] is not None and (best["score"] is None or result["score"] > best["score"]):
                best = {"score": result["score"], "samples": result["samples"], "entity_id": entity_id}
        return best

    def tested_count(agent_id):
        return sum(1 for row in all_pool_rows(agent_id)
                   if int(row.get("screening_samples") or 0) >= MIN_SCREENING_SAMPLES)

    def stage09_gate(agent, challenger, model, tournament, policy):
        active = list(policy.schema.entities)
        health = pool_stats(agent["id"], challenger)
        duplicate = duplicate_status(agent, challenger, active)
        if (model.get("candidate_primary_broken") and
                duplicate.get("entity_id") == model.get("candidate_replaced_entity")):
            duplicate = {"score": None, "samples": duplicate.get("samples", 0), "entity_id": None}
        metrics = metric_row(model, [float(x) for x in action_values(agent)])
        gate = promotion_gate(
            model=model, gain=metrics.get("gain"), health=health,
            redundancy=duplicate.get("score"), duplicate_of=duplicate.get("entity_id"),
            test_count=tested_count(agent["id"]), active_schema=active,
            target_schema=list(model.get("candidate_target_schema") or []),
            replacement_is_primary=bool(model.get("candidate_replacement_is_primary")),
            primary_broken=bool(model.get("candidate_primary_broken")),
            event_frequency=health.get("event_frequency") or 0.0,
        )
        model["stage09_gate"] = gate
        return gate

    def reset_for_schema(agent, challenger, proposed, replaced, policy, tournament):
        fresh = service._blank_shadow_model(len(action_values(agent)))
        broken = bool(replaced and pool_stats(agent["id"], replaced).get("health_ready") and
                      (pool_stats(agent["id"], replaced).get("availability") or 1.0) < 0.50)
        fresh.update({
            "prequential_epoch_version": PREQUENTIAL_EPOCH_VERSION,
            "evaluation_schema_revision": int(tournament.get("schema_revision") or 0),
            "evaluation_champion_revision": str(getattr(policy, "model_revision", "") or ""),
            "evaluation_started_ts": time.time(), "evaluation_reason": "exact_candidate_schema_changed",
            "candidate_requested_schema": list(proposed or []), "candidate_requested_replaced": replaced,
            "candidate_requested_primary": bool(replaced and replaced in primary_feature_ids(policy)),
            "candidate_requested_primary_broken": broken,
            "candidate_blocked_reason": "target_schema_changed_retrain_required",
        })
        service._save_shadow_model(agent["id"], challenger, fresh)
        candidate_cache.pop((str(agent["id"]), str(challenger)), None)
        pending_training.pop((str(agent["id"]), str(challenger)), None)

    def choose_exact(agent, policy, challenger, tournament):
        actions = [float(x) for x in action_values(agent)]
        model = original_load(agent["id"], challenger, len(actions)) if actions else {}
        candidate = candidate_from_model(agent, challenger, model, policy=policy, tournament=tournament)
        if candidate is None:
            model["promotion_blocked_reason"] = "candidate_policy_missing"
            service._save_shadow_model(agent["id"], challenger, model)
            return None, None

        # A proven-broken primary is allowed to use the ordinary Stage-09 gain/health gate.
        # Later schema probation still protects against a bad replacement. Healthy primary
        # sources continue through the existing stricter primary+quality chooser.
        if model.get("candidate_primary_broken"):
            proposed = list(model.get("candidate_target_schema") or [])
            replaced = model.get("candidate_replaced_entity")
        else:
            proposed, replaced = base_chooser(agent, policy, challenger, tournament)
            if proposed is None:
                return None, None
        if list(proposed) != list(candidate.schema.entities) or replaced != model.get("candidate_replaced_entity"):
            reset_for_schema(agent, challenger, proposed, replaced, policy, tournament)
            return None, None
        gate = stage09_gate(agent, challenger, model, tournament, policy)
        if not gate["ready"]:
            model["promotion_blocked_reason"] = gate.get("reason")
            service._save_shadow_model(agent["id"], challenger, model)
            return None, None
        model["promotion_blocked_reason"] = None
        service._save_shadow_model(agent["id"], challenger, model)
        return list(proposed), replaced

    promotion._choose_schema_after_promotion = choose_exact

    def migrate_exact(policy, new_entities, new_meta):
        challenger = str((new_meta or {}).get("sensor_tournament_promoted") or "")
        aid = str(getattr(policy, "agent", {}).get("id") or "")
        if not aid or not challenger:
            return base_migrate(policy, new_entities, new_meta)
        actions = [float(x) for x in action_values(policy.agent)]
        model = original_load(aid, challenger, len(actions)) if actions else {}
        raw = model.get("candidate_policy") if isinstance(model.get("candidate_policy"), dict) else None
        if not raw or list((raw.get("schema") or {}).get("entities") or []) != list(new_entities):
            return base_migrate(policy, new_entities, new_meta)
        with getattr(service.engine, "lock", threading.RLock()):
            states = dict(getattr(service.engine, "state_map", {}) or {})
            registry = dict(getattr(service.engine, "entity_registry", {}) or {})
        candidate = MultiHorizonPolicy(policy.agent, states, registry, set(), model=raw,
                                       relevance_scores=None, context_engine=getattr(service.engine, "context", None))
        old_entities = list(policy.schema.entities)
        policy.schema, policy.heads = candidate.schema, candidate.heads
        policy.model_revision = candidate.model_revision
        policy.selection_meta = dict(new_meta or candidate.selection_meta or {})
        service._last_exact_policy_promotion = {
            "contract": CONTRACT_VERSION, "promoted": challenger,
            "candidate_model_revision": candidate.model_revision,
            "candidate_training_samples": int(model.get("candidate_training_samples") or 0),
            "candidate_data_version": model.get("candidate_data_version"),
            "stage09_gate": model.get("stage09_gate") or {},
        }
        return {"changed": old_entities != list(policy.schema.entities),
                "added": [x for x in policy.schema.entities if x not in old_entities],
                "removed": [x for x in old_entities if x not in set(policy.schema.entities)],
                "exact_candidate_policy": True}

    promotion._migrate_schema = migrate_exact

    def observe_with_candidates(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        states = dict(state_map or getattr(service.engine, "state_map", {}) or {})
        now = time.time()
        if update_pool(agent, states, set(changed_entities or ()), now):
            policy = (getattr(service.engine, "models", {}) or {}).get(aid)
            if policy is not None:
                base = getattr(service.engine, "context_relevance", {}).get(aid) or {}
                original_sync(agent, policy=policy, feature_scores=combined_scores(agent, base), evaluated_at=now)
                choose_members(agent, original_state(aid), states)
        before = service.promotion_status(agent) if hasattr(service, "promotion_status") else {}
        before_ts = before.get("last_promotion_ts")
        with lock:
            current_context[aid] = {"agent": agent, "states": states, "now": now}
        try:
            result = original_observe(agent, states, changed_entities)
        finally:
            with lock:
                current_context.pop(aid, None)
        after = service.promotion_status(agent) if hasattr(service, "promotion_status") else {}
        if after.get("last_promotion_ts") is not None and after.get("last_promotion_ts") != before_ts:
            audit = dict(getattr(service, "_last_exact_policy_promotion", {}) or {})
            if audit.get("promoted") == after.get("promoted_entity"):
                details = dict(after.get("details") or {})
                details.update({"exact_candidate_policy": True,
                                "candidate_model_revision": audit.get("candidate_model_revision"),
                                "candidate_training_samples": audit.get("candidate_training_samples"),
                                "candidate_data_version": audit.get("candidate_data_version"),
                                "stage09_gate": audit.get("stage09_gate")})
                with service.store.lock, service.store.conn() as c:
                    c.execute("UPDATE context_tournament_promotions SET details_json=?,updated_ts=? WHERE agent_id=?",
                              (json.dumps(details, separators=(",", ":"), sort_keys=True, default=str), time.time(), aid))
                service.store.event(aid, "info", "context_feature_policy_promoted",
                                    f"Promoted exact trained policy candidate using {audit.get('promoted')}",
                                    {"predictive_gain": (audit.get("stage09_gate") or {}).get("predictive_gain"),
                                     "candidate_training_samples": audit.get("candidate_training_samples"),
                                     "exact_candidate_policy": True})
        return result

    service.observe_shadow = observe_with_candidates

    def status_with_candidates(agent):
        payload = original_status(agent)
        aid = str(agent["id"]); tournament = original_state(aid)
        policy = (getattr(service.engine, "models", {}) or {}).get(aid)
        active = list(policy.schema.entities) if policy is not None else list(tournament.get("active_features") or [])
        actions = [float(x) for x in action_values(agent)]
        for row in payload.get("challengers") or []:
            challenger = str(row.get("entity_id") or "")
            model = original_load(aid, challenger, len(actions)) if actions else {}
            raw = load_pool(aid, challenger)
            screening = dict(raw.get("screening") or semantic_predictive_score(raw.get("samples") or []))
            row["screening"] = screening
            row["screening_role"] = "priority_only_not_causal_evidence"
            row["sensor_health"] = pool_stats(aid, challenger)
            row["redundancy"] = duplicate_status(agent, challenger, active)
            row["candidate_target_schema"] = list(model.get("candidate_target_schema") or [])
            row["candidate_replaced_entity"] = model.get("candidate_replaced_entity")
            row["candidate_training_samples"] = int(model.get("candidate_training_samples") or 0)
            row["candidate_model_revision"] = model.get("candidate_model_revision")
            row["candidate_data_version"] = model.get("candidate_data_version")
            if policy is not None and model.get("candidate_target_schema"):
                gate = stage09_gate(agent, challenger, model, tournament, policy)
                row["predictive_gain"] = row.get("gain")
                row["predictive_gain_required"] = gate.get("required_gain")
                row["multiple_testing_penalty"] = gate.get("multiple_testing_penalty")
                row["feature_cost_penalty"] = gate.get("feature_cost_penalty")
                row["stage09_gate"] = gate
                row["promotion_ready"] = bool(row.get("promotion_ready") and gate.get("ready"))
                if not gate.get("ready") and not row.get("promotion_blocked_reason"):
                    row["promotion_blocked_reason"] = gate.get("reason")
            else:
                row["evidence_state"] = "needs_more_data" if screening.get("status") == "needs_more_data" else "neutral_or_screening_only"
        observed = all_pool_rows(aid)
        observed.sort(key=lambda item: (-float((item.get("screening") or {}).get("score") or 0.0), item["entity_id"]))
        payload.update({
            "observed_pool_count": len(observed), "observed_pool": observed[:12],
            "active_feature_count": len(active),
            "selection_contract": "broad observed pool -> screening -> compact schema -> paired future predictive gain",
            "semantic_groups": ["value", "quality", "lags", "trend", "time_since_edge", "selected_interactions"],
            "result_name": "predictive_gain", "causal_claim": False,
            "exact_candidate_policy_contract": CONTRACT_VERSION,
            "candidate_training_order": "predict -> paired score -> candidate learn",
        })
        return payload

    service.shadow_status = status_with_candidates

    def state_with_pool(agent_id):
        state = original_state(agent_id)
        state["observed_pool_count"] = len(all_pool_rows(agent_id))
        state["active_feature_count"] = len(state.get("active_features") or [])
        return state

    service.state = state_with_pool
    service.observed_pool = all_pool_rows
    service.sensor_pool_stats = pool_stats
    service.semantic_predictive_score = semantic_predictive_score
    service.predictive_redundancy = redundancy_score
    service.policy_candidate_contract = CONTRACT_VERSION
    service._policy_candidate_installed = True
    return service
