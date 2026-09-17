"""Stage-09 Sensor Tournament: screen broadly, validate the exact deployable policy.

Historical relevance and semantic associations in this module are *screening* signals.
They are never reported as causal influence and they can never promote a sensor by
themselves. Promotion still requires paired future/prequential predictive gain.

The important F15 contract is that a challenger is not a tiny lookup table whose win is
later translated into a zero-weight column.  For every challenger we clone the current
``MultiHorizonPolicy``, migrate the clone to the exact target schema, predict with that
policy in Shadow, train that same policy only after the paired future outcome is scored,
and finally copy those trained heads into the live policy if all promotion gates pass.

No function in this module creates ActionIntent, calls Executor, or invokes Home
Assistant services.  Candidate policies are passive until the existing Tournament
promotion transaction commits them.  Existing schema/probation history remains the
rollback authority.
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


CONTRACT_VERSION = 1
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


def _json(raw, fallback):
    try:
        value = json.loads(raw or "")
    except Exception:
        return fallback
    return value


def _corr(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if _finite(x) is not None and _finite(y) is not None]
    if len(pairs) < 3:
        return 0.0
    ax = [x for x, _ in pairs]
    ay = [y for _, y in pairs]
    mx = sum(ax) / len(ax)
    my = sum(ay) / len(ay)
    vx = sum((x - mx) ** 2 for x in ax)
    vy = sum((y - my) ** 2 for y in ay)
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    return max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))


def semantic_predictive_score(samples, min_samples=MIN_SCREENING_SAMPLES):
    """Return a bounded screening score over interpretable semantic feature groups.

    The score is deliberately only a challenger-screening statistic. A high value says
    "worth testing on future episodes", not "causes the target".
    """
    rows = [dict(x) for x in (samples or []) if isinstance(x, dict)]
    if len(rows) < int(min_samples):
        return {
            "score": 0.0, "best_group": None, "samples": len(rows),
            "groups": {}, "status": "needs_more_data",
        }
    labels = [row.get("label") for row in rows]
    group_values = defaultdict(list)
    for row in rows:
        for key in ("value", "quality", "lag_1", "lag_3", "lag_10", "trend", "time_since_edge"):
            group_values[key].append(row.get(key))
        interactions = row.get("interactions") if isinstance(row.get("interactions"), dict) else {}
        for key in sorted(interactions):
            group_values[f"interaction:{key}"].append(interactions.get(key))

    scores = {}
    for key, values in group_values.items():
        paired = [(x, y) for x, y in zip(values, labels)
                  if _finite(x) is not None and _finite(y) is not None]
        if len(paired) < int(min_samples):
            continue
        score = abs(_corr([x for x, _ in paired], [y for _, y in paired]))
        coverage = min(1.0, len(paired) / max(float(min_samples * 2), 1.0))
        scores[key] = score * coverage
    if not scores:
        return {
            "score": 0.0, "best_group": None, "samples": len(rows),
            "groups": {}, "status": "neutral",
        }
    best_group = max(scores, key=lambda key: (scores[key], key))
    best = max(0.0, min(1.0, float(scores[best_group])))
    return {
        "score": best,
        "best_group": best_group,
        "samples": len(rows),
        "groups": {k: round(float(v), 6) for k, v in sorted(scores.items())},
        "status": "screened" if best > 0.05 else "neutral",
    }


def redundancy_score(samples_a, samples_b):
    """Correlation of contemporaneous values on independent target episodes."""
    by_episode = {}
    for row in samples_a or []:
        if not isinstance(row, dict) or row.get("episode") is None:
            continue
        value = _finite(row.get("value"))
        if value is not None:
            by_episode[str(row["episode"])] = value
    xs, ys = [], []
    for row in samples_b or []:
        if not isinstance(row, dict) or row.get("episode") is None:
            continue
        key = str(row["episode"])
        value = _finite(row.get("value"))
        if key in by_episode and value is not None:
            xs.append(by_episode[key]); ys.append(value)
    return {
        "score": abs(_corr(xs, ys)) if len(xs) >= MIN_REDUNDANCY_SAMPLES else None,
        "samples": len(xs),
    }


def multiple_testing_penalty(test_count, step=MULTIPLE_TESTING_GAIN_STEP):
    tests = max(1, int(test_count or 0))
    return min(0.02, max(0.0, float(step)) * math.log2(float(tests) + 1.0))


def feature_cost_penalty(active_schema, target_schema, event_frequency=0.0):
    """Small explicit cost for extra feature slots and very chatty sensors."""
    before = len(list(active_schema or []))
    after = len(list(target_schema or []))
    dims = max(16, int(OPTIONS.get("feature_dimensions", 128)))
    added_entities = max(0, after - before)
    # ExplicitFeatureSchema currently spends four slots per entity plus selected pairwise
    # interactions. Replacement still has a small complexity cost because interactions
    # change even when entity count is unchanged.
    slot_cost = (4.0 * max(1, added_entities)) / float(dims)
    frequency = max(0.0, float(event_frequency or 0.0))
    frequency_cost = min(0.004, 0.0008 * math.log1p(frequency))
    return min(0.012, FEATURE_COST_GAIN_SCALE * slot_cost + frequency_cost)


def sensor_health_from_row(row):
    row = dict(row or {})
    opportunities = int(row.get("opportunities") or 0)
    available = int(row.get("available_count") or 0)
    unknown = int(row.get("unknown_count") or 0)
    unavailable = int(row.get("unavailable_count") or 0)
    changes = int(row.get("change_count") or 0)
    leaks = int(row.get("own_action_leak_hits") or 0)
    availability = (available / opportunities) if opportunities else None
    failure_rate = ((unknown + unavailable) / opportunities) if opportunities else None
    health = None
    if availability is not None:
        health = max(0.0, min(1.0, availability * (1.0 - 0.35 * (failure_rate or 0.0))))
    leak_rate = (leaks / changes) if changes else None
    first = _finite(row.get("first_seen_ts"))
    last = _finite(row.get("last_seen_ts"))
    observed_days = 0.0 if first is None or last is None else max(0.0, (last - first) / 86400.0)
    return {
        "opportunities": opportunities,
        "availability": availability,
        "failure_rate": failure_rate,
        "health": health,
        "health_ready": opportunities >= MIN_HEALTH_OPPORTUNITIES,
        "change_count": changes,
        "own_action_leak_hits": leaks,
        "own_action_leak_rate": leak_rate,
        "observed_days": observed_days,
    }


def effective_required_gain(*, base_gain, primary=False, primary_broken=False,
                            test_count=1, active_schema=None, target_schema=None,
                            event_frequency=0.0, health=None):
    base = max(0.0, float(base_gain or 0.0))
    if primary and not primary_broken:
        base = max(base, float(primary_replacement_gain()))
    multiplicity = multiple_testing_penalty(test_count)
    cost = feature_cost_penalty(active_schema, target_schema, event_frequency)
    health_penalty = 0.0
    if health is not None:
        health_penalty = max(0.0, 0.9 - float(health)) * 0.01
    return {
        "required_gain": base + multiplicity + cost + health_penalty,
        "base_gain": base,
        "multiple_testing_penalty": multiplicity,
        "feature_cost_penalty": cost,
        "health_penalty": health_penalty,
    }


def plan_target_schema(agent, policy, challenger, tournament, health_lookup=None):
    """Freeze the schema that the deployable challenger will actually use."""
    active = [str(x) for x in (getattr(policy.schema, "entities", []) or [])]
    challenger = str(challenger)
    if challenger in active:
        return {"schema": list(active), "replaced": None, "replacement_is_primary": False,
                "primary_broken": False, "reason": "already_active"}
    limit = promotion._selection_limit(agent)
    if len(active) < limit:
        return {"schema": active + [challenger], "replaced": None,
                "replacement_is_primary": False, "primary_broken": False,
                "reason": "append"}

    explicit = {str(x) for x in (agent.get("input_entities") or [])}
    primary = primary_feature_ids(policy)
    scores = dict((tournament or {}).get("feature_scores") or {})
    removable = []
    for idx, entity_id in enumerate(active):
        if entity_id in explicit:
            continue
        health = dict((health_lookup(entity_id) if callable(health_lookup) else {}) or {})
        ready = bool(health.get("health_ready"))
        availability = health.get("availability")
        broken = bool(ready and availability is not None and float(availability) < 0.50)
        is_primary = entity_id in primary
        # Healthy primary inputs remain behind all ordinary removable inputs. A genuinely
        # broken primary may be replaced without pretending its historical role vanished.
        priority = 0 if broken else (2 if is_primary else 1)
        removable.append((priority, float(scores.get(entity_id, 0.0)), idx, entity_id,
                          is_primary, broken))
    if not removable:
        return {"schema": None, "replaced": None, "replacement_is_primary": False,
                "primary_broken": False, "reason": "no_replaceable_schema_slot"}
    removable.sort(key=lambda row: (row[0], row[1], -row[2], row[3]))
    _, _, idx, replaced, is_primary, broken = removable[0]
    result = list(active)
    result[idx] = challenger
    return {
        "schema": result, "replaced": replaced,
        "replacement_is_primary": bool(is_primary), "primary_broken": bool(broken),
        "reason": "replace_broken_primary" if (is_primary and broken) else "replace",
    }


def promotion_gate(*, model, gain, health, redundancy, duplicate_of, test_count,
                   active_schema, target_schema, replacement_is_primary=False,
                   primary_broken=False, event_frequency=0.0, base_gain=None):
    health = dict(health or {})
    gain_req = effective_required_gain(
        base_gain=(OPTIONS.get("context_tournament_min_gain", 0.03) if base_gain is None else base_gain),
        primary=replacement_is_primary,
        primary_broken=primary_broken,
        test_count=test_count,
        active_schema=active_schema,
        target_schema=target_schema,
        event_frequency=event_frequency,
        health=health.get("health"),
    )
    leak_rate = health.get("own_action_leak_rate")
    leak_blocked = bool(
        health.get("change_count", 0) >= 5 and leak_rate is not None
        and float(leak_rate) >= LEAK_BLOCK_RATE
    )
    redundancy_blocked = bool(
        redundancy is not None and float(redundancy) >= REDUNDANCY_THRESHOLD
        and duplicate_of
    )
    health_ok = bool(
        health.get("health_ready")
        and health.get("availability") is not None
        and float(health.get("availability")) >= 0.80
    )
    candidate_samples = int((model or {}).get("candidate_training_samples") or 0)
    exact_ready = bool((model or {}).get("candidate_policy") and (model or {}).get("candidate_target_schema"))
    gain_ok = gain is not None and float(gain) + FLOAT_EPSILON >= float(gain_req["required_gain"])
    ready = bool(health_ok and not leak_blocked and not redundancy_blocked and exact_ready
                 and candidate_samples > 0 and gain_ok)
    reason = None
    if not health_ok:
        reason = "sensor_health"
    elif leak_blocked:
        reason = "own_action_leakage"
    elif redundancy_blocked:
        reason = "redundant_sensor"
    elif not exact_ready:
        reason = "candidate_policy_missing"
    elif candidate_samples <= 0:
        reason = "candidate_policy_untrained"
    elif not gain_ok:
        reason = "predictive_gain"
    return {
        "ready": ready, "reason": reason, "predictive_gain": gain,
        "duplicate_of": duplicate_of, "redundancy": redundancy,
        "health": health, "candidate_training_samples": candidate_samples,
        **gain_req,
    }


def install_policy_candidates(service):
    """Install Stage-09 screening + exact deployable-policy comparison."""
    if getattr(service, "_policy_candidate_installed", False):
        return service

    with service.store.lock, service.store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_tournament_observed_pool (
                agent_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                pool_version INTEGER NOT NULL DEFAULT 1,
                opportunities INTEGER NOT NULL DEFAULT 0,
                available_count INTEGER NOT NULL DEFAULT 0,
                unknown_count INTEGER NOT NULL DEFAULT 0,
                unavailable_count INTEGER NOT NULL DEFAULT 0,
                change_count INTEGER NOT NULL DEFAULT 0,
                own_action_leak_hits INTEGER NOT NULL DEFAULT 0,
                first_seen_ts REAL,
                last_seen_ts REAL,
                last_change_ts REAL,
                last_value REAL,
                history_json TEXT NOT NULL DEFAULT '[]',
                samples_json TEXT NOT NULL DEFAULT '[]',
                screening_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL,
                PRIMARY KEY(agent_id, entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_context_observed_pool_agent_updated
                ON context_tournament_observed_pool(agent_id,updated_ts DESC);
            """
        )

    lock = threading.RLock()
    pool_cache = {}
    pool_members = {}
    current_context = {}
    candidate_cache = {}
    pending_training = {}
    last_target = {}
    last_own_action = {}
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
        return {
            "opportunities": 0, "available_count": 0, "unknown_count": 0,
            "unavailable_count": 0, "change_count": 0, "own_action_leak_hits": 0,
            "first_seen_ts": None, "last_seen_ts": None, "last_change_ts": None,
            "last_value": None, "history": [], "samples": [], "screening": {},
        }

    def load_pool(agent_id, entity_id):
        key = (str(agent_id), str(entity_id))
        with lock:
            if key in pool_cache:
                return pool_cache[key]
        with service.store.conn() as c:
            row = c.execute(
                "SELECT * FROM context_tournament_observed_pool WHERE agent_id=? AND entity_id=?",
                key,
            ).fetchone()
        value = blank_row()
        if row:
            for field in ("opportunities", "available_count", "unknown_count", "unavailable_count",
                          "change_count", "own_action_leak_hits"):
                value[field] = int(row[field] or 0)
            for field in ("first_seen_ts", "last_seen_ts", "last_change_ts", "last_value"):
                value[field] = row[field]
            value["history"] = _json(row["history_json"], []) if row["history_json"] else []
            value["samples"] = _json(row["samples_json"], []) if row["samples_json"] else []
            value["screening"] = _json(row["screening_json"], {}) if row["screening_json"] else {}
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
        with service.store.lock, service.store.conn() as c:
            c.execute(
                """INSERT INTO context_tournament_observed_pool
                   (agent_id,entity_id,pool_version,opportunities,available_count,unknown_count,
                    unavailable_count,change_count,own_action_leak_hits,first_seen_ts,last_seen_ts,
                    last_change_ts,last_value,history_json,samples_json,screening_json,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id,entity_id) DO UPDATE SET
                     pool_version=excluded.pool_version,opportunities=excluded.opportunities,
                     available_count=excluded.available_count,unknown_count=excluded.unknown_count,
                     unavailable_count=excluded.unavailable_count,change_count=excluded.change_count,
                     own_action_leak_hits=excluded.own_action_leak_hits,
                     first_seen_ts=excluded.first_seen_ts,last_seen_ts=excluded.last_seen_ts,
                     last_change_ts=excluded.last_change_ts,last_value=excluded.last_value,
                     history_json=excluded.history_json,samples_json=excluded.samples_json,
                     screening_json=excluded.screening_json,updated_ts=excluded.updated_ts""",
                (
                    key[0], key[1], POOL_VERSION, int(row.get("opportunities") or 0),
                    int(row.get("available_count") or 0), int(row.get("unknown_count") or 0),
                    int(row.get("unavailable_count") or 0), int(row.get("change_count") or 0),
                    int(row.get("own_action_leak_hits") or 0), row.get("first_seen_ts"),
                    row.get("last_seen_ts"), row.get("last_change_ts"), row.get("last_value"),
                    json.dumps(row.get("history") or [], separators=(",", ":"), sort_keys=True),
                    json.dumps(row.get("samples") or [], separators=(",", ":"), sort_keys=True),
                    json.dumps(row.get("screening") or {}, separators=(",", ":"), sort_keys=True),
                    float(now),
                ),
            )
        with lock:
            pool_cache[key] = row

    def pool_stats(agent_id, entity_id):
        row = load_pool(agent_id, entity_id)
        health = sensor_health_from_row(row)
        screening = dict(row.get("screening") or semantic_predictive_score(row.get("samples") or []))
        first = _finite(row.get("first_seen_ts")); last = _finite(row.get("last_seen_ts"))
        hours = 1.0 if first is None or last is None else max(1.0, (last - first) / 3600.0)
        frequency = float(row.get("change_count") or 0) / hours
        return {**health, "screening": screening, "event_frequency": frequency,
                "screening_samples": len(row.get("samples") or [])}

    def choose_members(agent, tournament, states):
        aid = str(agent["id"])
        active = [str(x) for x in tournament.get("active_features") or []]
        eligible = service._eligible_entities(agent, active)
        eligible = set(eligible or [])
        scores = dict(tournament.get("feature_scores") or {})
        must = list(active) + [str(x) for x in tournament.get("challenger_features") or []]
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

    def all_pool_rows(agent_id):
        aid = str(agent_id)
        with service.store.conn() as c:
            rows = c.execute(
                """SELECT entity_id,screening_json,opportunities,available_count,unknown_count,
                   unavailable_count,change_count,own_action_leak_hits,first_seen_ts,last_seen_ts,
                   last_change_ts,last_value,samples_json,updated_ts
                   FROM context_tournament_observed_pool WHERE agent_id=?
                   ORDER BY updated_ts DESC LIMIT ?""",
                (aid, max(32, int(OPTIONS.get("context_observed_pool_limit", DEFAULT_POOL_LIMIT)) * 2)),
            ).fetchall()
        out = []
        for row in rows:
            raw = {
                "opportunities": row["opportunities"], "available_count": row["available_count"],
                "unknown_count": row["unknown_count"], "unavailable_count": row["unavailable_count"],
                "change_count": row["change_count"], "own_action_leak_hits": row["own_action_leak_hits"],
                "first_seen_ts": row["first_seen_ts"], "last_seen_ts": row["last_seen_ts"],
            }
            health = sensor_health_from_row(raw)
            screening = _json(row["screening_json"], {})
            samples = _json(row["samples_json"], [])
            first = _finite(row["first_seen_ts"]); last = _finite(row["last_seen_ts"])
            hours = 1.0 if first is None or last is None else max(1.0, (last - first) / 3600.0)
            out.append({"entity_id": str(row["entity_id"]), **health,
                        "screening": screening if isinstance(screening, dict) else {},
                        "screening_samples": len(samples) if isinstance(samples, list) else 0,
                        "event_frequency": float(row["change_count"] or 0) / hours,
                        "last_change_ts": row["last_change_ts"], "last_value": row["last_value"]})
        return out

    def combined_screening_scores(agent, base_scores=None):
        aid = str(agent["id"])
        combined = {}
        for eid, value in (base_scores or {}).items():
            v = _finite(value)
            if v is not None:
                combined[str(eid)] = max(0.0, min(1.0, v))
        for row in all_pool_rows(aid):
            screening = dict(row.get("screening") or {})
            score = _finite(screening.get("score")) or 0.0
            if score > combined.get(row["entity_id"], 0.0):
                combined[row["entity_id"]] = min(1.0, score)
        return combined

    def sync_with_pool(agent, **kwargs):
        explicit_scores = kwargs.get("feature_scores")
        base = explicit_scores
        if base is None:
            base = getattr(service.engine, "context_relevance", {}).get(agent["id"]) or {}
        kwargs["feature_scores"] = combined_screening_scores(agent, base)
        state = original_sync(agent, **kwargs)
        with getattr(service.engine, "lock", threading.RLock()):
            states = dict(getattr(service.engine, "state_map", {}) or {})
        choose_members(agent, state, states)
        return state

    service.sync_agent = sync_with_pool

    def _history_value(history, cutoff):
        eligible = [(float(row[0]), float(row[1])) for row in history or []
                    if len(row) >= 2 and float(row[0]) <= float(cutoff)]
        return eligible[-1][1] if eligible else None

    def update_pool(agent, states, changed_entities, now):
        aid = str(agent["id"])
        tournament = original_state(aid)
        members = list(pool_members.get(aid) or ())
        if not members:
            members = choose_members(agent, tournament, states)
        changed = {str(x) for x in (changed_entities or set())}
        rt = (getattr(service.engine, "runtime", {}) or {}).get(aid) or {}
        current_target = target_value(states.get(agent["target_entity"]), agent["target_property"])
        previous_target = last_target.get(aid)
        origin = str(rt.get("last_change_origin") or "")
        target_changed = (
            current_target is not None and previous_target is not None
            and abs(float(current_target) - float(previous_target)) > max(0.01, float(agent.get("deadband") or 0.01) * 0.05)
        )
        if target_changed and origin == "own_command":
            last_own_action[aid] = float(now)
        independent_label = bool(target_changed and origin != "own_command")
        actions = [float(x) for x in action_values(agent)]
        label = None
        if independent_label and actions:
            actual_idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(current_target)))
            label = (actual_idx / max(1.0, float(len(actions) - 1)))
        episode = f"{aid}:{int(getattr(service.engine, 'state_revision', 0))}:{int(now * 1000)}"

        # Selected interactions are intentionally few and interpretable.
        policy = (getattr(service.engine, "models", {}) or {}).get(aid)
        active = list(getattr(getattr(policy, "schema", None), "entities", []) or []) if policy else []
        primary_order = []
        if policy is not None:
            meta = dict(getattr(policy, "selection_meta", {}) or {})
            for key in ("primary_occupancy_sensor", "primary_local_sensor"):
                if meta.get(key): primary_order.append(str(meta[key]))
        for eid in active:
            if eid not in primary_order:
                primary_order.append(eid)
        interaction_ids = primary_order[:3]
        interaction_values = {}
        for eid in interaction_ids:
            value = context_scalar(eid, states.get(eid), agent)
            if _finite(value) is not None:
                interaction_values[eid] = float(value)

        own_ts = last_own_action.get(aid)
        for entity_id in members:
            row = load_pool(aid, entity_id)
            row["opportunities"] = int(row.get("opportunities") or 0) + 1
            if row.get("first_seen_ts") is None:
                row["first_seen_ts"] = float(now)
            state = states.get(entity_id)
            text = str((state or {}).get("state") or "").strip().lower()
            scalar = context_scalar(entity_id, state, agent) if state is not None else None
            available = bool(state) and text not in ("", "unknown", "unavailable", "none")
            value = _finite(scalar)
            if available and value is not None:
                row["available_count"] = int(row.get("available_count") or 0) + 1
                row["last_seen_ts"] = float(now)
                previous_value = _finite(row.get("last_value"))
                changed_value = previous_value is None or abs(value - previous_value) > 1e-9
                if changed_value:
                    row["change_count"] = int(row.get("change_count") or 0) + 1
                    row["last_change_ts"] = float(now)
                    row["last_value"] = float(value)
                    history = list(row.get("history") or [])
                    history.append([float(now), float(value)])
                    row["history"] = history[-MAX_HISTORY:]
                    if own_ts is not None and 0.0 <= float(now) - float(own_ts) <= LEAK_WINDOW_SECONDS:
                        row["own_action_leak_hits"] = int(row.get("own_action_leak_hits") or 0) + 1
            elif text in ("unknown", "none", ""):
                row["unknown_count"] = int(row.get("unknown_count") or 0) + 1
            else:
                row["unavailable_count"] = int(row.get("unavailable_count") or 0) + 1

            if independent_label and label is not None and value is not None:
                history = list(row.get("history") or [])
                last_edge = _finite(row.get("last_change_ts"))
                health = sensor_health_from_row(row)
                sample = {
                    "episode": episode, "ts": float(now), "label": float(label),
                    "value": float(value), "quality": health.get("health"),
                    "lag_1": _history_value(history, now - 1.0),
                    "lag_3": _history_value(history, now - 3.0),
                    "lag_10": _history_value(history, now - 10.0),
                    "time_since_edge": None if last_edge is None else math.exp(-max(0.0, now - last_edge) / 30.0),
                    "interactions": {
                        other: float(value) * other_value for other, other_value in interaction_values.items()
                        if other != entity_id
                    },
                }
                base_lag = sample.get("lag_10")
                sample["trend"] = None if base_lag is None else float(value) - float(base_lag)
                samples = list(row.get("samples") or [])
                samples.append(sample)
                row["samples"] = samples[-MAX_SCREENING_SAMPLES:]
            save_pool(aid, entity_id, row, now)

        if current_target is not None:
            last_target[aid] = float(current_target)
        return independent_label

    def candidate_plan(agent, challenger, policy, tournament):
        return plan_target_schema(
            agent, policy, challenger, tournament,
            health_lookup=lambda eid: pool_stats(agent["id"], eid),
        )

    def candidate_from_model(agent, challenger, model, policy=None, tournament=None, requested_schema=None,
                             requested_replaced=None):
        aid = str(agent["id"])
        policy = policy or (getattr(service.engine, "models", {}) or {}).get(aid)
        if policy is None:
            return None
        tournament = dict(tournament or original_state(aid))
        target_plan = candidate_plan(agent, challenger, policy, tournament)
        forced = list(requested_schema or model.get("candidate_requested_schema") or [])
        if forced:
            target_plan = {
                "schema": forced,
                "replaced": requested_replaced if requested_replaced is not None else model.get("candidate_requested_replaced"),
                "replacement_is_primary": bool(model.get("candidate_requested_primary")),
                "primary_broken": bool(model.get("candidate_requested_primary_broken")),
                "reason": "promotion_plan_requalification",
            }
        target_schema = list(target_plan.get("schema") or [])
        if not target_schema or str(challenger) not in set(target_schema):
            model["candidate_blocked_reason"] = target_plan.get("reason") or "no_target_schema"
            return None

        source_revision = str(getattr(policy, "model_revision", "") or "")
        expected_revision = str(model.get("evaluation_champion_revision") or source_revision)
        raw = model.get("candidate_policy") if isinstance(model.get("candidate_policy"), dict) else None
        valid = bool(
            raw and list((raw.get("schema") or {}).get("entities") or []) == target_schema
            and str(model.get("candidate_source_model_revision") or "") == expected_revision
            and int(model.get("candidate_contract_version") or 0) == CONTRACT_VERSION
        )
        key = (aid, str(challenger))
        if valid:
            cached = candidate_cache.get(key)
            if cached is not None and str(getattr(cached, "model_revision", "")) == str(raw.get("model_revision") or ""):
                return cached
            with getattr(service.engine, "lock", threading.RLock()):
                states = dict(getattr(service.engine, "state_map", {}) or {})
                registry = dict(getattr(service.engine, "entity_registry", {}) or {})
            candidate = MultiHorizonPolicy(
                agent, states, registry, set(), model=raw, relevance_scores=None,
                context_engine=getattr(service.engine, "context", None),
            )
            candidate_cache[key] = candidate
            return candidate

        with getattr(service.engine, "lock", threading.RLock()):
            states = dict(getattr(service.engine, "state_map", {}) or {})
            registry = dict(getattr(service.engine, "entity_registry", {}) or {})
        champion_raw = policy.serialize()
        candidate = MultiHorizonPolicy(
            agent, states, registry, set(), model=champion_raw, relevance_scores=None,
            context_engine=getattr(service.engine, "context", None),
        )
        meta = promotion._selection_meta_for_promotion(
            candidate, str(challenger), target_plan.get("replaced")
        )
        migration = _migrate_schema(candidate, target_schema, meta)
        model["candidate_contract_version"] = CONTRACT_VERSION
        model["candidate_source_model_revision"] = expected_revision
        model["candidate_target_schema"] = list(candidate.schema.entities)
        model["candidate_replaced_entity"] = target_plan.get("replaced")
        model["candidate_replacement_is_primary"] = bool(target_plan.get("replacement_is_primary"))
        model["candidate_primary_broken"] = bool(target_plan.get("primary_broken"))
        model["candidate_policy"] = candidate.serialize()
        model["candidate_model_revision"] = candidate.model_revision
        model["candidate_training_samples"] = 0
        model["candidate_migration"] = migration
        model["candidate_data_version"] = {
            "contract": "paired_future_policy_v1",
            "schema_revision": int(tournament.get("schema_revision") or 0),
            "champion_revision": expected_revision,
            "policy_version": int(getattr(candidate, "VERSION", 0)),
            "feature_schema_version": int(getattr(candidate.schema, "VERSION", 0)),
        }
        model.pop("candidate_requested_schema", None)
        model.pop("candidate_requested_replaced", None)
        model.pop("candidate_requested_primary", None)
        model.pop("candidate_requested_primary_broken", None)
        candidate_cache[key] = candidate
        service._save_shadow_model(aid, challenger, model)
        return candidate

    def load_with_key(agent_id, challenger, action_count):
        model = original_load(agent_id, challenger, action_count)
        tls.shadow_key = (str(agent_id), str(challenger))
        return model

    service._load_shadow_model = load_with_key

    def predict_with_exact_candidate(model, active_idx, bucket):
        key = getattr(tls, "shadow_key", None)
        if not key:
            return original_shadow_predict(model, active_idx, bucket)
        aid, challenger = key
        context = current_context.get(aid)
        if not context:
            return original_shadow_predict(model, active_idx, bucket)
        agent = context["agent"]
        tournament = original_state(aid)
        policy = (getattr(service.engine, "models", {}) or {}).get(aid)
        candidate = candidate_from_model(agent, challenger, model, policy=policy, tournament=tournament)
        if candidate is None:
            return int(active_idx)
        temporal = getattr(service.engine, "temporal_history", None)
        if temporal is None:
            return int(active_idx)
        try:
            features, _, _ = candidate.features(context["states"], temporal, at_ts=context["now"])
            chosen, confidence, _, horizon, support, novelty = candidate.predict(features)
            actions = [float(x) for x in action_values(agent)]
            idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(chosen["value"])))
            pending_training[key] = {
                "features": dict(features), "horizon": int(horizon), "ts": float(context["now"]),
                "state_revision": int(getattr(service.engine, "state_revision", 0)),
                "candidate_model_revision": str(candidate.model_revision),
                "candidate_schema": list(candidate.schema.entities),
                "confidence": float(confidence), "support": float(support), "novelty": float(novelty),
            }
            return int(idx)
        except Exception as exc:
            model["candidate_blocked_reason"] = f"candidate_prediction:{type(exc).__name__}"
            return int(active_idx)

    service._shadow_predict_index = predict_with_exact_candidate

    def score_and_train_exact_candidate(agent_id, challenger, pending, actual_idx, action_count, now):
        # Inner metrics score baseline and challenger first; only then may the deployable
        # candidate learn from this outcome.
        result = original_score(agent_id, challenger, pending, actual_idx, action_count, now)
        key = (str(agent_id), str(challenger))
        train = pending_training.get(key)
        context = current_context.get(str(agent_id))
        if not train or not context:
            return result
        agent = context["agent"]
        model = original_load(agent_id, challenger, action_count)
        tournament = original_state(agent_id)
        candidate = candidate_from_model(agent, challenger, model, tournament=tournament)
        if candidate is None or list(candidate.schema.entities) != list(train.get("candidate_schema") or []):
            return result
        horizon = int(train.get("horizon") or min(candidate.horizons))
        features = dict(train.get("features") or {})
        if horizon not in candidate.heads or not features:
            return result
        try:
            candidate.heads[horizon].validate(int(actual_idx), features, 1.0, sample_ts=train.get("ts"))
            candidate.update(horizon, int(actual_idx), features, 1.0, sample_ts=train.get("ts"))
            model["candidate_policy"] = candidate.serialize()
            model["candidate_model_revision"] = candidate.model_revision
            model["candidate_training_samples"] = int(model.get("candidate_training_samples") or 0) + 1
            model["candidate_last_training_ts"] = float(now)
            model["candidate_last_episode_state_revision"] = int(train.get("state_revision") or 0)
            service._save_shadow_model(agent_id, challenger, model)
            candidate_cache[key] = candidate
        finally:
            pending_training.pop(key, None)
        return result

    service._score_shadow_sample = score_and_train_exact_candidate

    def duplicate_status(agent, challenger, active):
        challenger_row = load_pool(agent["id"], challenger)
        best = {"score": None, "samples": 0, "entity_id": None}
        for entity_id in active:
            if entity_id == challenger:
                continue
            score = redundancy_score(challenger_row.get("samples") or [],
                                     load_pool(agent["id"], entity_id).get("samples") or [])
            if score.get("score") is None:
                continue
            if best["score"] is None or float(score["score"]) > float(best["score"]):
                best = {"score": float(score["score"]), "samples": int(score["samples"]),
                        "entity_id": str(entity_id)}
        return best

    def tested_count(agent_id):
        return sum(1 for row in all_pool_rows(agent_id)
                   if int(row.get("screening_samples") or 0) >= MIN_SCREENING_SAMPLES)

    def stage09_gate(agent, challenger, model, tournament, policy):
        target_schema = list(model.get("candidate_target_schema") or [])
        active = list(getattr(policy.schema, "entities", []) or [])
        health = pool_stats(agent["id"], challenger)
        duplicate = duplicate_status(agent, challenger, active)
        metrics = metric_row(model, [float(x) for x in action_values(agent)])
        gate = promotion_gate(
            model=model, gain=metrics.get("gain"), health=health,
            redundancy=duplicate.get("score"), duplicate_of=duplicate.get("entity_id"),
            test_count=tested_count(agent["id"]), active_schema=active, target_schema=target_schema,
            replacement_is_primary=bool(model.get("candidate_replacement_is_primary")),
            primary_broken=bool(model.get("candidate_primary_broken")),
            event_frequency=health.get("event_frequency") or 0.0,
        )
        model["stage09_gate"] = gate
        return gate

    def reset_for_exact_schema(agent, challenger, proposed, replaced, policy, tournament, old_model):
        fresh = service._blank_shadow_model(len(action_values(agent)))
        fresh.update({
            "prequential_epoch_version": PREQUENTIAL_EPOCH_VERSION,
            "evaluation_schema_revision": int(tournament.get("schema_revision") or 0),
            "evaluation_champion_revision": str(getattr(policy, "model_revision", "") or ""),
            "evaluation_started_ts": time.time(),
            "evaluation_reason": "exact_candidate_schema_changed",
            "candidate_requested_schema": list(proposed or []),
            "candidate_requested_replaced": replaced,
            "candidate_requested_primary": bool(replaced and replaced in primary_feature_ids(policy)),
            "candidate_requested_primary_broken": bool(
                replaced and (pool_stats(agent["id"], replaced).get("health_ready"))
                and (pool_stats(agent["id"], replaced).get("availability") or 1.0) < 0.50
            ),
            "candidate_blocked_reason": "target_schema_changed_retrain_required",
        })
        service._save_shadow_model(agent["id"], challenger, fresh)
        candidate_cache.pop((str(agent["id"]), str(challenger)), None)
        pending_training.pop((str(agent["id"]), str(challenger)), None)
        return fresh

    def choose_exact_candidate(agent, policy, challenger, tournament):
        proposed, replaced = base_chooser(agent, policy, challenger, tournament)
        if proposed is None:
            return None, None
        actions = [float(x) for x in action_values(agent)]
        model = original_load(agent["id"], challenger, len(actions)) if actions else {}
        candidate = candidate_from_model(agent, challenger, model, policy=policy, tournament=tournament)
        if candidate is None:
            model["promotion_blocked_reason"] = "candidate_policy_missing"
            service._save_shadow_model(agent["id"], challenger, model)
            return None, None
        candidate_schema = list(candidate.schema.entities)
        if list(proposed) != candidate_schema or replaced != model.get("candidate_replaced_entity"):
            reset_for_exact_schema(agent, challenger, proposed, replaced, policy, tournament, model)
            return None, None
        gate = stage09_gate(agent, challenger, model, tournament, policy)
        if not gate["ready"]:
            model["promotion_blocked_reason"] = gate.get("reason")
            service._save_shadow_model(agent["id"], challenger, model)
            return None, None
        model["promotion_blocked_reason"] = None
        service._save_shadow_model(agent["id"], challenger, model)
        return list(proposed), replaced

    promotion._choose_schema_after_promotion = choose_exact_candidate

    def migrate_exact_candidate(policy, new_entities, new_meta):
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
        candidate = MultiHorizonPolicy(
            policy.agent, states, registry, set(), model=raw, relevance_scores=None,
            context_engine=getattr(service.engine, "context", None),
        )
        old_entities = list(policy.schema.entities)
        policy.schema = candidate.schema
        policy.heads = candidate.heads
        policy.model_revision = candidate.model_revision
        policy.selection_meta = dict(new_meta or candidate.selection_meta or {})
        audit = {
            "contract": CONTRACT_VERSION,
            "promoted": challenger,
            "old_schema": old_entities,
            "new_schema": list(policy.schema.entities),
            "candidate_model_revision": candidate.model_revision,
            "candidate_training_samples": int(model.get("candidate_training_samples") or 0),
            "candidate_data_version": model.get("candidate_data_version"),
            "stage09_gate": model.get("stage09_gate") or {},
        }
        with lock:
            setattr(service, "_last_exact_policy_promotion", audit)
        return {
            "changed": old_entities != list(policy.schema.entities),
            "added": [x for x in policy.schema.entities if x not in old_entities],
            "removed": [x for x in old_entities if x not in set(policy.schema.entities)],
            "exact_candidate_policy": True,
        }

    promotion._migrate_schema = migrate_exact_candidate

    def observe_with_policy_candidates(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        states = dict(state_map or getattr(service.engine, "state_map", {}) or {})
        changed = set(changed_entities or ())
        now = time.time()
        independent_label = update_pool(agent, states, changed, now)
        if independent_label:
            policy = (getattr(service.engine, "models", {}) or {}).get(aid)
            if policy is not None:
                # Screening may use the just-observed label only to choose what to test.
                # A newly selected challenger predicts *after* this point, so this label
                # can never enter its paired future promotion evidence.
                base_scores = getattr(service.engine, "context_relevance", {}).get(aid) or {}
                original_sync(agent, policy=policy,
                              feature_scores=combined_screening_scores(agent, base_scores),
                              evaluated_at=now)
                choose_members(agent, original_state(aid), states)

        before_promotion = service.promotion_status(agent) if hasattr(service, "promotion_status") else {}
        before_ts = before_promotion.get("last_promotion_ts")
        with lock:
            current_context[aid] = {"agent": agent, "states": states, "now": now}
        try:
            result = original_observe(agent, states, changed_entities)
        finally:
            with lock:
                current_context.pop(aid, None)

        after_promotion = service.promotion_status(agent) if hasattr(service, "promotion_status") else {}
        after_ts = after_promotion.get("last_promotion_ts")
        if after_ts is not None and after_ts != before_ts:
            audit = dict(getattr(service, "_last_exact_policy_promotion", {}) or {})
            if audit and audit.get("promoted") == after_promotion.get("promoted_entity"):
                details = dict(after_promotion.get("details") or {})
                details.update({
                    "exact_candidate_policy": True,
                    "candidate_model_revision": audit.get("candidate_model_revision"),
                    "candidate_training_samples": audit.get("candidate_training_samples"),
                    "candidate_data_version": audit.get("candidate_data_version"),
                    "stage09_gate": audit.get("stage09_gate"),
                })
                with service.store.lock, service.store.conn() as c:
                    c.execute(
                        "UPDATE context_tournament_promotions SET details_json=?,updated_ts=? WHERE agent_id=?",
                        (json.dumps(details, separators=(",", ":"), sort_keys=True, default=str),
                         time.time(), aid),
                    )
                service.store.event(
                    aid, "info", "context_feature_policy_promoted",
                    f"Promoted exact trained policy candidate using {audit.get('promoted')}",
                    {"predictive_gain": (audit.get("stage09_gate") or {}).get("predictive_gain"),
                     "candidate_training_samples": audit.get("candidate_training_samples"),
                     "exact_candidate_policy": True},
                )
        return result

    service.observe_shadow = observe_with_policy_candidates

    def status_with_policy_candidates(agent):
        payload = original_status(agent)
        aid = str(agent["id"])
        tournament = original_state(aid)
        policy = (getattr(service.engine, "models", {}) or {}).get(aid)
        active = list(getattr(getattr(policy, "schema", None), "entities", []) or []) if policy else list(tournament.get("active_features") or [])
        actions = [float(x) for x in action_values(agent)]
        for row in payload.get("challengers") or []:
            challenger = str(row.get("entity_id") or "")
            model = original_load(aid, challenger, len(actions)) if actions else {}
            health = pool_stats(aid, challenger)
            screen_row = load_pool(aid, challenger)
            screening = dict(screen_row.get("screening") or semantic_predictive_score(screen_row.get("samples") or []))
            duplicate = duplicate_status(agent, challenger, active)
            row["screening"] = screening
            row["screening_role"] = "priority_only_not_causal_evidence"
            row["sensor_health"] = health
            row["redundancy"] = duplicate
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
            elif screening.get("status") == "needs_more_data":
                row["evidence_state"] = "needs_more_data"
            else:
                row["evidence_state"] = "neutral_or_screening_only"

        observed = all_pool_rows(aid)
        observed.sort(key=lambda item: (
            -float((item.get("screening") or {}).get("score") or 0.0), item["entity_id"]
        ))
        payload["observed_pool_count"] = len(observed)
        payload["observed_pool"] = observed[:12]
        payload["active_feature_count"] = len(active)
        payload["selection_contract"] = (
            "broad_observed_pool -> historical+semantic screening -> small active/challenger schemas -> "
            "paired future predictive gain"
        )
        payload["semantic_groups"] = [
            "value", "quality", "lags", "trend", "time_since_edge", "selected_interactions"
        ]
        payload["result_name"] = "predictive_gain"
        payload["causal_claim"] = False
        payload["exact_candidate_policy_contract"] = CONTRACT_VERSION
        payload["candidate_training_order"] = "predict -> paired score -> candidate learn"
        return payload

    service.shadow_status = status_with_policy_candidates

    def state_with_pool(agent_id):
        state = original_state(agent_id)
        rows = all_pool_rows(agent_id)
        state["observed_pool_count"] = len(rows)
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
