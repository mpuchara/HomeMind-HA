"""Sensor quality diagnostics and quality-aware ranking for Sensor Tournament.

Step 12 keeps the first quality definition deliberately simple:

    sensor_quality = availability

The additional counters are persisted now so later ranking logic can become richer
without losing history: unknown/unavailable rates, event frequency, stale time and recent
failures.  The current predictive ranking is intentionally minimal as well:

    ranking_score = predictive_gain * sensor_quality

Quality is diagnostics-only for append operations.  When a full schema would evict an
active feature, quality also participates in the replacement decision: the challenger
must clear the normal future-only gain gates, its quality-adjusted gain must clear the
same replacement margin, and its quality-adjusted discovery rank must beat the proposed
incumbent.  This prevents a flaky sensor from displacing an equally relevant stable one.

The extension never creates ActionIntent, never calls Executor and never invokes Home
Assistant services.
"""
import json
import math
import threading
import time

import context_tournament_promotion as promotion
from context import action_values
from context_tournament_metrics import metric_row
from context_tournament_primary_protection import primary_feature_ids, primary_replacement_gain
from settings import OPTIONS


RECENT_FAILURE_SECONDS = 24.0 * 3600.0
FAILURE_HISTORY_LIMIT = 64
FLOAT_EPSILON = 1e-12


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def quality_from_availability(availability):
    """Initial 0..1 sensor-quality contract: Q = availability."""
    value = _finite(availability)
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def quality_adjusted_ranking(predictive_gain, sensor_quality):
    """Current simple tournament ranking: predictive gain multiplied by quality."""
    gain = _finite(predictive_gain)
    quality = quality_from_availability(sensor_quality)
    if gain is None or quality is None:
        return None
    return gain * quality


def _safe_rate(numerator, denominator):
    denominator = int(denominator or 0)
    return (float(numerator or 0) / denominator) if denominator > 0 else None


def quality_metrics(row, now=None):
    """Derive stable diagnostics from a persisted quality accumulator."""
    now = float(time.time() if now is None else now)
    opportunities = int((row or {}).get("opportunities") or 0)
    available = int((row or {}).get("available_count") or 0)
    unknown = int((row or {}).get("unknown_count") or 0)
    unavailable = int((row or {}).get("unavailable_count") or 0)
    events = int((row or {}).get("event_count") or 0)
    first_ts = _finite((row or {}).get("first_observed_ts"))
    last_event_ts = _finite((row or {}).get("last_event_ts"))
    failures = []
    for value in (row or {}).get("failure_timestamps") or []:
        ts = _finite(value)
        if ts is not None and ts >= now - RECENT_FAILURE_SECONDS:
            failures.append(ts)

    availability = _safe_rate(available, opportunities)
    unknown_rate = _safe_rate(unknown, opportunities)
    unavailable_rate = _safe_rate(unavailable, opportunities)
    if first_ts is None:
        event_frequency = 0.0
        stale_time = None
    else:
        observed_hours = max(1.0, max(0.0, now - first_ts) / 3600.0)
        event_frequency = events / observed_hours
        stale_time = max(0.0, now - (last_event_ts if last_event_ts is not None else first_ts))

    return {
        "availability": availability,
        "unknown_rate": unknown_rate,
        "unavailable_rate": unavailable_rate,
        "event_frequency": event_frequency,
        "stale_time": stale_time,
        "recent_failures": len(failures),
        "sensor_quality": quality_from_availability(availability),
        "opportunities": opportunities,
        "available_observations": available,
        "event_count": events,
    }


def _state_class(state):
    if not state:
        return "unavailable"
    text = str(state.get("state") or "").strip().lower()
    if text in ("unknown", "none", ""):
        return "unknown"
    if text == "unavailable":
        return "unavailable"
    return "available"


def _required_replacement_gain(policy, replaced):
    base = max(0.0, min(1.0, float(OPTIONS.get("context_tournament_min_gain", 0.03))))
    if replaced and str(replaced) in primary_feature_ids(policy):
        return primary_replacement_gain()
    return base


def quality_replacement_gate(*, predictive_gain, challenger_quality, incumbent_quality,
                             challenger_feature_score, incumbent_feature_score,
                             required_gain):
    """Return the conservative quality gate for a schema replacement.

    Two comparisons are used and both are simple:
    1. future-only incremental gain is discounted by challenger availability;
    2. for an actual eviction, quality-adjusted discovery rank of the challenger must beat
       the incumbent's quality-adjusted discovery rank.

    Missing challenger quality is treated as 0; missing incumbent quality as 1.  This is
    intentionally conservative during upgrades until enough live observations exist.
    """
    cq = quality_from_availability(challenger_quality)
    iq = quality_from_availability(incumbent_quality)
    cq = 0.0 if cq is None else cq
    iq = 1.0 if iq is None else iq
    gain_rank = quality_adjusted_ranking(predictive_gain, cq)
    required = max(0.0, float(required_gain or 0.0))

    challenger_discovery = _finite(challenger_feature_score)
    incumbent_discovery = _finite(incumbent_feature_score)
    challenger_discovery = 0.0 if challenger_discovery is None else max(0.0, challenger_discovery)
    incumbent_discovery = 0.0 if incumbent_discovery is None else max(0.0, incumbent_discovery)
    challenger_rank = challenger_discovery * cq
    incumbent_rank = incumbent_discovery * iq

    gain_passes = gain_rank is not None and gain_rank > required + FLOAT_EPSILON
    incumbent_passes = challenger_rank > incumbent_rank + FLOAT_EPSILON
    return {
        "passes": bool(gain_passes and incumbent_passes),
        "quality_adjusted_gain": gain_rank,
        "required_gain": required,
        "challenger_quality": cq,
        "incumbent_quality": iq,
        "challenger_discovery_score": challenger_discovery,
        "incumbent_discovery_score": incumbent_discovery,
        "challenger_ranking_score": challenger_rank,
        "incumbent_ranking_score": incumbent_rank,
        "gain_passes": bool(gain_passes),
        "incumbent_rank_passes": bool(incumbent_passes),
    }


def install_sensor_quality(service):
    """Persist quality statistics and add them to Tournament diagnostics/replacement."""
    if getattr(service, "_sensor_quality_installed", False):
        return service

    with service.store.lock, service.store.conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS context_tournament_sensor_quality (
                   agent_id TEXT NOT NULL,
                   entity_id TEXT NOT NULL,
                   opportunities INTEGER NOT NULL DEFAULT 0,
                   available_count INTEGER NOT NULL DEFAULT 0,
                   unknown_count INTEGER NOT NULL DEFAULT 0,
                   unavailable_count INTEGER NOT NULL DEFAULT 0,
                   event_count INTEGER NOT NULL DEFAULT 0,
                   first_observed_ts REAL,
                   last_observed_ts REAL,
                   last_event_ts REAL,
                   failure_timestamps_json TEXT NOT NULL DEFAULT '[]',
                   updated_ts REAL NOT NULL,
                   PRIMARY KEY(agent_id, entity_id)
               )"""
        )

    lock = threading.RLock()
    cache = {}
    original_observe = service.observe_shadow
    original_status = service.shadow_status
    base_chooser = promotion._choose_schema_after_promotion

    def load_row(agent_id, entity_id):
        key = (str(agent_id), str(entity_id))
        with lock:
            if key in cache:
                return dict(cache[key])
        with service.store.conn() as c:
            row = c.execute(
                "SELECT * FROM context_tournament_sensor_quality WHERE agent_id=? AND entity_id=?",
                key,
            ).fetchone()
        if row:
            try:
                failures = json.loads(row["failure_timestamps_json"] or "[]")
            except Exception:
                failures = []
            value = {
                "opportunities": int(row["opportunities"] or 0),
                "available_count": int(row["available_count"] or 0),
                "unknown_count": int(row["unknown_count"] or 0),
                "unavailable_count": int(row["unavailable_count"] or 0),
                "event_count": int(row["event_count"] or 0),
                "first_observed_ts": row["first_observed_ts"],
                "last_observed_ts": row["last_observed_ts"],
                "last_event_ts": row["last_event_ts"],
                "failure_timestamps": failures if isinstance(failures, list) else [],
            }
        else:
            value = {
                "opportunities": 0, "available_count": 0, "unknown_count": 0,
                "unavailable_count": 0, "event_count": 0, "first_observed_ts": None,
                "last_observed_ts": None, "last_event_ts": None, "failure_timestamps": [],
            }
        with lock:
            cache[key] = dict(value)
        return value

    def save_row(agent_id, entity_id, value, now):
        key = (str(agent_id), str(entity_id))
        failures = [
            float(ts) for ts in (value.get("failure_timestamps") or [])
            if _finite(ts) is not None and float(ts) >= now - RECENT_FAILURE_SECONDS
        ][-FAILURE_HISTORY_LIMIT:]
        value["failure_timestamps"] = failures
        with service.store.lock, service.store.conn() as c:
            c.execute(
                """INSERT INTO context_tournament_sensor_quality
                   (agent_id,entity_id,opportunities,available_count,unknown_count,
                    unavailable_count,event_count,first_observed_ts,last_observed_ts,
                    last_event_ts,failure_timestamps_json,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id,entity_id) DO UPDATE SET
                     opportunities=excluded.opportunities,
                     available_count=excluded.available_count,
                     unknown_count=excluded.unknown_count,
                     unavailable_count=excluded.unavailable_count,
                     event_count=excluded.event_count,
                     first_observed_ts=excluded.first_observed_ts,
                     last_observed_ts=excluded.last_observed_ts,
                     last_event_ts=excluded.last_event_ts,
                     failure_timestamps_json=excluded.failure_timestamps_json,
                     updated_ts=excluded.updated_ts""",
                (
                    key[0], key[1], int(value.get("opportunities") or 0),
                    int(value.get("available_count") or 0), int(value.get("unknown_count") or 0),
                    int(value.get("unavailable_count") or 0), int(value.get("event_count") or 0),
                    value.get("first_observed_ts"), value.get("last_observed_ts"),
                    value.get("last_event_ts"), json.dumps(failures, separators=(",", ":")),
                    float(now),
                ),
            )
        with lock:
            cache[key] = dict(value)

    def observe_quality(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        states = dict(state_map or getattr(service.engine, "state_map", {}) or {})
        tournament = service.state(aid)
        entities = []
        seen = set()
        for eid in list(tournament.get("active_features") or []) + list(tournament.get("challenger_features") or []):
            eid = str(eid)
            if eid and eid not in seen:
                entities.append(eid)
                seen.add(eid)
        changed = {str(x) for x in (changed_entities or set())}
        now = time.time()
        for entity_id in entities:
            value = load_row(aid, entity_id)
            value["opportunities"] = int(value.get("opportunities") or 0) + 1
            if value.get("first_observed_ts") is None:
                value["first_observed_ts"] = now
            state_class = _state_class(states.get(entity_id))
            if state_class == "available":
                value["available_count"] = int(value.get("available_count") or 0) + 1
                value["last_observed_ts"] = now
            elif state_class == "unknown":
                value["unknown_count"] = int(value.get("unknown_count") or 0) + 1
                value.setdefault("failure_timestamps", []).append(now)
            else:
                value["unavailable_count"] = int(value.get("unavailable_count") or 0) + 1
                value.setdefault("failure_timestamps", []).append(now)
            if entity_id in changed:
                value["event_count"] = int(value.get("event_count") or 0) + 1
                value["last_event_ts"] = now
            save_row(aid, entity_id, value, now)
        # Quality is recorded before the wrapped promotion observer, so an automatic
        # replacement sees the current event's reliability state.
        return original_observe(agent, states, changed_entities)

    def sensor_stats(agent_id, entity_id, now=None):
        return quality_metrics(load_row(agent_id, entity_id), now)

    def choose_with_quality(agent, policy, challenger, tournament):
        new_entities, replaced = base_chooser(agent, policy, challenger, tournament)
        if new_entities is None or not replaced:
            return new_entities, replaced

        aid = str(agent["id"])
        actions = [float(x) for x in action_values(agent)]
        model = service._load_shadow_model(aid, challenger, len(actions)) if actions else {}
        metrics = metric_row(model, actions) if actions else {"gain": None}
        challenger_stats = sensor_stats(aid, challenger)
        incumbent_stats = sensor_stats(aid, replaced)
        feature_scores = dict((tournament or {}).get("feature_scores") or {})
        gate = quality_replacement_gate(
            predictive_gain=metrics.get("gain"),
            challenger_quality=challenger_stats.get("sensor_quality"),
            incumbent_quality=incumbent_stats.get("sensor_quality"),
            challenger_feature_score=feature_scores.get(challenger),
            incumbent_feature_score=feature_scores.get(replaced),
            required_gain=_required_replacement_gain(policy, replaced),
        )
        previews = getattr(service, "_sensor_quality_replacement_previews", None)
        if previews is None:
            previews = {}
            service._sensor_quality_replacement_previews = previews
        previews[(aid, str(challenger))] = {"replaced": replaced, **gate}
        if not gate["passes"]:
            model["promotion_blocked_reason"] = "sensor_quality"
            try:
                service._save_shadow_model(aid, challenger, model)
            except Exception:
                pass
            return None, None
        return new_entities, replaced

    def status_with_quality(agent):
        payload = original_status(agent)
        aid = str(agent["id"])
        now = time.time()
        previews = getattr(service, "_sensor_quality_replacement_previews", {})
        for row in payload.get("challengers") or []:
            entity_id = str(row.get("entity_id") or "")
            stats = sensor_stats(aid, entity_id, now)
            row.update(stats)
            row["predictive_gain"] = row.get("gain")
            row["ranking_score"] = quality_adjusted_ranking(
                row.get("gain"), stats.get("sensor_quality")
            )
            preview = previews.get((aid, entity_id)) or {}
            if preview:
                row["quality_adjusted_gain"] = preview.get("quality_adjusted_gain")
                row["incumbent_quality"] = preview.get("incumbent_quality")
                row["challenger_ranking_score"] = preview.get("challenger_ranking_score")
                row["incumbent_ranking_score"] = preview.get("incumbent_ranking_score")
                row["quality_replacement_passes"] = bool(preview.get("passes"))

        active_rows = []
        for entity_id in service.state(aid).get("active_features") or []:
            stats = sensor_stats(aid, entity_id, now)
            active_rows.append({"entity_id": entity_id, **stats})
        payload["active_sensor_quality"] = active_rows
        payload["sensor_quality_definition"] = "sensor_quality = availability"
        payload["ranking_definition"] = "ranking_score = predictive_gain * sensor_quality"
        payload["quality_recent_failure_hours"] = RECENT_FAILURE_SECONDS / 3600.0
        return payload

    service.observe_shadow = observe_quality
    service.shadow_status = status_with_quality
    service.sensor_quality = sensor_stats
    promotion._choose_schema_after_promotion = choose_with_quality
    service._sensor_quality_installed = True
    return service
