"""Post-promotion Shadow probation with automatic schema/model rollback.

A Sensor Tournament promotion changes the policy representation.  The promoted policy
therefore remains in Shadow and is compared prequentially against the frozen champion
that existed immediately before promotion.  Each target transition is scored using
predictions made before that transition; the old champion is never learned on probation
data.

Default contract:
- keep the exact previous serialized policy + schema,
- observe up to ``context_schema_probation_samples`` future outcomes (default 50),
- start rollback checks after 30 scored outcomes,
- rollback when ``new_score < old_score - 0.03``,
- otherwise mark the promoted schema accepted after the configured probation sample
  count once the paired score is defined.

Rollback restores the exact previous serialized policy, leaves the affected agent in
Shadow, marks the schema-history row ``rolled_back`` and emits
``context_schema_rolled_back``.  This module never sends an ActionIntent or HA service.
"""
import json
import math
import time

from context import action_values, target_value
from context_tournament_metrics import metric_row
from policy import MultiHorizonPolicy
from settings import OPTIONS
import context_tournament_promotion as promotion


MIN_ROLLBACK_SAMPLES = 30
ROLLBACK_MARGIN = 0.03
FLOAT_EPSILON = 1e-12
PROMOTION_REASON = "sensor_tournament_promotion"


def probation_target_samples(options=None):
    opts = options or OPTIONS
    return max(MIN_ROLLBACK_SAMPLES, int(opts.get("context_schema_probation_samples", 50)))


def probation_decision(stats, actions, target_samples=50):
    """Return wait / rollback / accept from paired future-only probation evidence."""
    metrics = metric_row(stats or {}, actions or [])
    samples = int(metrics.get("samples") or 0)
    old_score = metrics.get("baseline_score")
    new_score = metrics.get("challenger_score")
    comparable = old_score is not None and new_score is not None
    degraded = bool(
        comparable
        and samples >= MIN_ROLLBACK_SAMPLES
        and float(new_score) < float(old_score) - ROLLBACK_MARGIN - FLOAT_EPSILON
    )
    if degraded:
        decision = "rollback"
    elif comparable and samples >= max(MIN_ROLLBACK_SAMPLES, int(target_samples)):
        decision = "accept"
    else:
        decision = "wait"
    return {
        "decision": decision,
        "samples": samples,
        "old_score": old_score,
        "new_score": new_score,
        "delta": None if not comparable else float(new_score) - float(old_score),
        "margin": ROLLBACK_MARGIN,
        "min_rollback_samples": MIN_ROLLBACK_SAMPLES,
        "target_samples": max(MIN_ROLLBACK_SAMPLES, int(target_samples)),
        "metric": metrics.get("metric"),
    }


def _schema_from_model(raw):
    schema = dict((raw or {}).get("schema") or {})
    return [str(x) for x in (schema.get("entities") or [])]


def _blank_stats(action_count):
    n = int(action_count)
    return {
        "samples": 0,
        "class_totals": [0] * n,
        "active_correct_by_class": [0] * n,
        "shadow_correct_by_class": [0] * n,
        "active_abs_error_sum": 0.0,
        "shadow_abs_error_sum": 0.0,
    }


def _score_stats(stats, actions, actual_value, old_prediction, new_prediction):
    """Score one paired out-of-sample outcome: old champion vs promoted policy."""
    actions = [float(x) for x in actions]
    if not actions:
        return stats
    row = dict(stats or _blank_stats(len(actions)))
    n = len(actions)
    for key in ("class_totals", "active_correct_by_class", "shadow_correct_by_class"):
        values = list(row.get(key) or [])
        if len(values) != n:
            values = [0] * n
        row[key] = values
    actual_idx = min(range(n), key=lambda i: abs(actions[i] - float(actual_value)))
    old_idx = min(range(n), key=lambda i: abs(actions[i] - float(old_prediction)))
    new_idx = min(range(n), key=lambda i: abs(actions[i] - float(new_prediction)))
    row["samples"] = int(row.get("samples") or 0) + 1
    row["class_totals"][actual_idx] += 1
    row["active_correct_by_class"][actual_idx] += 1 if old_idx == actual_idx else 0
    row["shadow_correct_by_class"][actual_idx] += 1 if new_idx == actual_idx else 0
    actual_action = actions[actual_idx]
    row["active_abs_error_sum"] = float(row.get("active_abs_error_sum") or 0.0) + abs(
        actions[old_idx] - actual_action
    )
    row["shadow_abs_error_sum"] = float(row.get("shadow_abs_error_sum") or 0.0) + abs(
        actions[new_idx] - actual_action
    )
    return row


def _json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _decode(raw, fallback):
    try:
        value = json.loads(raw or "")
        return value
    except Exception:
        return fallback


def install_schema_probation(service):
    """Attach persistent post-promotion probation outside history/requalification hooks."""
    if getattr(service, "_schema_probation_installed", False):
        return service
    if not hasattr(service, "schema_history") or not hasattr(service, "set_schema_history_status"):
        raise RuntimeError("Schema history must be installed before schema probation")

    with service.store.lock, service.store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_schema_probation (
                history_id INTEGER PRIMARY KEY,
                agent_id TEXT NOT NULL,
                created_ts REAL NOT NULL,
                previous_model_json TEXT NOT NULL,
                previous_schema_json TEXT NOT NULL,
                promoted_schema_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','accepted','rolled_back')),
                samples INTEGER NOT NULL DEFAULT 0,
                stats_json TEXT NOT NULL DEFAULT '{}',
                last_target REAL,
                pending_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_context_schema_probation_agent_status
                ON context_schema_probation(agent_id,status,created_ts DESC);
            """
        )

    def row_dict(row):
        if not row:
            return None
        return {
            "history_id": int(row["history_id"]),
            "agent_id": str(row["agent_id"]),
            "created_ts": float(row["created_ts"]),
            "previous_model": _decode(row["previous_model_json"], {}),
            "previous_schema": _decode(row["previous_schema_json"], []),
            "promoted_schema": _decode(row["promoted_schema_json"], []),
            "status": str(row["status"]),
            "samples": int(row["samples"] or 0),
            "stats": _decode(row["stats_json"], {}),
            "last_target": None if row["last_target"] is None else float(row["last_target"]),
            "pending": _decode(row["pending_json"], {}),
            "updated_ts": float(row["updated_ts"]),
        }

    def probation_for_agent(agent_id, active_only=False):
        where = "AND status='active'" if active_only else ""
        with service.store.conn() as c:
            row = c.execute(
                f"""SELECT * FROM context_schema_probation WHERE agent_id=? {where}
                    ORDER BY created_ts DESC,history_id DESC LIMIT 1""",
                (str(agent_id),),
            ).fetchone()
        return row_dict(row)

    def probation_active(agent_id):
        return probation_for_agent(agent_id, active_only=True) is not None

    def save_probation(row):
        now = time.time()
        stats = dict(row.get("stats") or {})
        samples = int(stats.get("samples") or row.get("samples") or 0)
        with service.store.lock, service.store.conn() as c:
            c.execute(
                """UPDATE context_schema_probation SET status=?,samples=?,stats_json=?,
                   last_target=?,pending_json=?,updated_ts=? WHERE history_id=?""",
                (
                    str(row["status"]), samples, _json(stats), row.get("last_target"),
                    _json(row.get("pending") or {}), now, int(row["history_id"]),
                ),
            )
        row["samples"] = samples
        row["updated_ts"] = now
        return row

    def start_probation(history_row, previous_model):
        history_id = int(history_row["id"])
        old_schema = [str(x) for x in (history_row.get("old_schema") or [])]
        new_schema = [str(x) for x in (history_row.get("new_schema") or [])]
        if not previous_model or _schema_from_model(previous_model) != old_schema:
            raise RuntimeError("Previous policy snapshot does not match pre-promotion schema")
        actions = [float(x) for x in action_values(service.store.get_agent_config(history_row["agent_id"]))]
        now = time.time()
        with service.store.lock, service.store.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO context_schema_probation
                   (history_id,agent_id,created_ts,previous_model_json,previous_schema_json,
                    promoted_schema_json,status,samples,stats_json,last_target,pending_json,updated_ts)
                   VALUES(?,?,?,?,?,?, 'active',0,?,NULL,'{}',?)""",
                (
                    history_id, str(history_row["agent_id"]), now, _json(previous_model),
                    _json(old_schema), _json(new_schema), _json(_blank_stats(len(actions))), now,
                ),
            )
        return probation_for_agent(history_row["agent_id"], active_only=True)

    def predict_model(raw_model, agent, states, at_ts):
        if not raw_model:
            return None
        engine = service.engine
        with getattr(engine, "lock", _NullContext()):
            registry = dict(getattr(engine, "entity_registry", {}) or {})
        policy = MultiHorizonPolicy(
            agent, states, registry, [], model=raw_model, relevance_scores=None,
            context_engine=getattr(engine, "context", None),
        )
        temporal = getattr(engine, "temporal_history", None)
        if temporal is None:
            return None
        features, _, _ = policy.features(states, temporal, at_ts=at_ts)
        chosen, _, _, _, _, _ = policy.predict(features)
        return float(chosen["value"])

    def current_model_snapshot(agent_id):
        model = (getattr(service.engine, "models", {}) or {}).get(str(agent_id))
        if model is not None and hasattr(model, "serialize"):
            return model.serialize()
        return service.store.get_model(str(agent_id))

    def set_pending(row, agent, states, now):
        current_model = current_model_snapshot(agent["id"])
        if not current_model or _schema_from_model(current_model) != list(row["promoted_schema"]):
            row["pending"] = {}
            return row
        old_prediction = predict_model(row["previous_model"], agent, states, now)
        new_prediction = predict_model(current_model, agent, states, now)
        if old_prediction is None or new_prediction is None:
            row["pending"] = {}
            return row
        row["pending"] = {
            "ts": float(now),
            "old_prediction": float(old_prediction),
            "new_prediction": float(new_prediction),
        }
        return row

    def restore_previous(row, agent, decision):
        aid = str(agent["id"])
        previous_model = dict(row.get("previous_model") or {})
        if _schema_from_model(previous_model) != list(row.get("previous_schema") or []):
            raise RuntimeError("Stored rollback policy/schema mismatch")
        service.store.save_model(aid, previous_model)
        models = getattr(service.engine, "models", None)
        if isinstance(models, dict):
            models.pop(aid, None)
        restored = service.engine.policy(agent)
        restored_schema = list(getattr(getattr(restored, "schema", None), "entities", []) or [])
        if restored_schema != list(row.get("previous_schema") or []):
            raise RuntimeError("Rollback reload did not restore the previous schema")
        service.set_schema_history_status(row["history_id"], "rolled_back")
        row["status"] = "rolled_back"
        row["pending"] = {}
        save_probation(row)

        runtime = getattr(service.engine, "runtime", None)
        if isinstance(runtime, dict):
            rt = runtime.setdefault(aid, {})
            rt["decision_state"] = "shadow"
            rt["decision_reason"] = "Promoted schema underperformed; previous policy restored"
            rt["schema_requalification"] = {
                "required": True,
                "rolled_back": True,
                "history_id": int(row["history_id"]),
                "reason": "schema_probation_rollback",
            }
        data = {
            "history_id": int(row["history_id"]),
            "samples": int(decision.get("samples") or 0),
            "old_score": decision.get("old_score"),
            "new_score": decision.get("new_score"),
            "delta": decision.get("delta"),
            "rollback_margin": ROLLBACK_MARGIN,
            "restored_schema": list(row.get("previous_schema") or []),
        }
        service.store.event(
            aid, "warning", "context_schema_rolled_back",
            "Promoted schema underperformed during Shadow probation; previous policy restored",
            data,
        )
        try:
            service.engine.wake_event.set()
        except Exception:
            pass
        return True

    def accept_probation(row, agent, decision):
        aid = str(agent["id"])
        service.set_schema_history_status(row["history_id"], "accepted")
        row["status"] = "accepted"
        row["pending"] = {}
        save_probation(row)
        runtime = getattr(service.engine, "runtime", None)
        if isinstance(runtime, dict):
            rt = runtime.setdefault(aid, {})
            rq = dict(rt.get("schema_requalification") or {})
            rq.update({
                "required": False,
                "probation_complete": True,
                "history_id": int(row["history_id"]),
                "probation_samples": int(decision.get("samples") or 0),
            })
            rt["schema_requalification"] = rq
        service.store.event(
            aid, "info", "context_schema_probation_accepted",
            "Promoted schema passed Shadow probation",
            {
                "history_id": int(row["history_id"]),
                "samples": int(decision.get("samples") or 0),
                "old_score": decision.get("old_score"),
                "new_score": decision.get("new_score"),
                "delta": decision.get("delta"),
            },
        )
        return True

    def score_existing_probation(agent, states, now):
        aid = str(agent["id"])
        row = probation_for_agent(aid, active_only=True)
        if not row:
            return None
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        if current is None or not math.isfinite(float(current)):
            return row
        last_target = row.get("last_target")
        pending = dict(row.get("pending") or {})
        deadband = max(0.01, float(agent.get("deadband") or 0.01) * 0.05)
        changed = last_target is not None and abs(float(current) - float(last_target)) > deadband
        if changed and pending:
            rt = (getattr(service.engine, "runtime", {}) or {}).get(aid) or {}
            origin = str(rt.get("last_change_origin") or "")
            if origin != "own_command":
                actions = [float(x) for x in action_values(agent)]
                row["stats"] = _score_stats(
                    row.get("stats"), actions, float(current),
                    float(pending["old_prediction"]), float(pending["new_prediction"]),
                )
        row["last_target"] = float(current)
        save_probation(row)

        decision = probation_decision(
            row.get("stats"), [float(x) for x in action_values(agent)], probation_target_samples()
        )
        if decision["decision"] == "rollback":
            try:
                restore_previous(row, agent, decision)
            except Exception as exc:
                service.store.event(
                    aid, "error", "context_schema_rollback_failed",
                    f"Automatic schema rollback failed: {type(exc).__name__}: {exc}",
                    {"history_id": int(row["history_id"]), "samples": decision.get("samples")},
                )
            return probation_for_agent(aid, active_only=True)
        if decision["decision"] == "accept":
            accept_probation(row, agent, decision)
            return None
        return row

    # While a promoted schema is on probation, do not allow another challenger to mutate
    # the same agent's schema.  The final quality-aware chooser is preserved underneath.
    base_chooser = promotion._choose_schema_after_promotion

    def choose_schema_with_probation(agent, policy, challenger, tournament):
        if probation_active(agent["id"]):
            return None, None
        return base_chooser(agent, policy, challenger, tournament)

    promotion._choose_schema_after_promotion = choose_schema_with_probation

    original_observe = service.observe_shadow
    original_status = service.shadow_status

    def observe_with_probation(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        states = dict(state_map or getattr(service.engine, "state_map", {}) or {})
        now = time.time()
        old_policy_snapshot = current_model_snapshot(aid)
        before_history = service.schema_history(aid, limit=1)
        before_history_id = int(before_history[0]["id"]) if before_history else 0

        # Score the prediction made on the previous event before inner Tournament code can
        # learn from or react to this outcome.
        active = score_existing_probation(agent, states, now)

        result = original_observe(agent, states, changed_entities)

        latest_rows = service.schema_history(aid, limit=1)
        latest = dict(latest_rows[0]) if latest_rows else None
        if (
            latest and int(latest.get("id") or 0) > before_history_id
            and latest.get("reason") == PROMOTION_REASON
            and latest.get("status") == "promoted"
            and list(latest.get("old_schema") or []) != list(latest.get("new_schema") or [])
        ):
            try:
                active = start_probation(latest, old_policy_snapshot)
                service.store.event(
                    aid, "info", "context_schema_probation_started",
                    "Promoted schema entered Shadow probation against the previous champion",
                    {
                        "history_id": int(latest["id"]),
                        "probation_samples": probation_target_samples(),
                        "min_rollback_samples": MIN_ROLLBACK_SAMPLES,
                        "rollback_margin": ROLLBACK_MARGIN,
                    },
                )
            except Exception as exc:
                service.store.event(
                    aid, "error", "context_schema_probation_start_failed",
                    f"Could not preserve previous policy for probation: {type(exc).__name__}: {exc}",
                    {"history_id": int(latest["id"])},
                )
                active = None
        else:
            active = probation_for_agent(aid, active_only=True)

        if active:
            current = target_value(states.get(agent["target_entity"]), agent["target_property"])
            if current is not None and math.isfinite(float(current)):
                active["last_target"] = float(current)
                try:
                    set_pending(active, agent, states, now)
                except Exception:
                    active["pending"] = {}
                save_probation(active)
        return result

    def status_with_probation(agent):
        payload = original_status(agent)
        row = probation_for_agent(agent["id"], active_only=False)
        if not row:
            payload["schema_probation"] = None
            return payload
        actions = [float(x) for x in action_values(agent)]
        decision = probation_decision(row.get("stats"), actions, probation_target_samples())
        payload["schema_probation"] = {
            "history_id": int(row["history_id"]),
            "status": row["status"],
            "samples": int(decision.get("samples") or 0),
            "target_samples": probation_target_samples(),
            "min_rollback_samples": MIN_ROLLBACK_SAMPLES,
            "rollback_margin": ROLLBACK_MARGIN,
            "old_score": decision.get("old_score"),
            "new_score": decision.get("new_score"),
            "delta": decision.get("delta"),
            "metric": decision.get("metric"),
            "previous_schema": list(row.get("previous_schema") or []),
            "promoted_schema": list(row.get("promoted_schema") or []),
        }
        return payload

    service.observe_shadow = observe_with_probation
    service.shadow_status = status_with_probation
    service.schema_probation = lambda agent_id: probation_for_agent(agent_id, active_only=False)
    service.schema_probation_active = probation_active
    service._schema_probation_installed = True
    return service


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
