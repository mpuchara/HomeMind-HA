"""Structured numeric event contract for Context Tournament decisions.

This module is observability-only.  It wraps Tournament bookkeeping and the Store event
sink after all control/safety extensions have been installed.  It never changes feature
selection, policy inference, ActionIntent, Executor, qualification, or HA services.

All Context Tournament decision events use deterministic event names/messages and
structured numeric evidence.  There is deliberately no generated explanatory text.
"""
import math
import time

from context import action_values
from context_tournament_metrics import availability_stats, metric_row
from context_tournament_promotion import tournament_config
from context_schema_probation import MIN_ROLLBACK_SAMPLES, ROLLBACK_MARGIN, probation_target_samples


EVENT_MESSAGES = {
    "context_challenger_started": "Context challenger started",
    "context_challenger_evaluated": "Context challenger evaluated",
    "context_feature_promoted": "Context feature promoted",
    "context_feature_rejected": "Context feature rejected",
    "context_schema_probation_started": "Context schema probation started",
    "context_schema_accepted": "Context schema accepted",
    "context_schema_rolled_back": "Context schema rolled back",
    "context_sensor_unreliable": "Context sensor unreliable",
}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric(value, default=0.0):
    number = _finite(value)
    return float(default if number is None else number)


def _metrics(service, agent, entity_id):
    actions = [float(x) for x in action_values(agent)]
    if not actions:
        return {
            "metric": None, "old_score": None, "new_score": None, "gain": None,
            "samples": 0, "availability": None, "days": 0.0,
        }
    model = service._load_shadow_model(agent["id"], entity_id, len(actions))
    row = metric_row(model, actions)
    availability, days = availability_stats(model)
    return {
        "metric": row.get("metric"),
        "old_score": row.get("baseline_score"),
        "new_score": row.get("challenger_score"),
        "gain": row.get("gain"),
        "samples": int(row.get("samples") or 0),
        "availability": availability,
        "days": float(days or 0.0),
        "model": model,
    }


def _quality_payload(service, agent_id, entity_id):
    getter = getattr(service, "sensor_quality", None)
    if not callable(getter):
        return {}
    try:
        row = dict(getter(agent_id, entity_id) or {})
    except Exception:
        return {}
    out = {}
    for key in (
        "availability", "unknown_rate", "unavailable_rate", "event_frequency",
        "stale_time", "recent_failures", "opportunities", "available_observations",
        "event_count", "sensor_quality",
    ):
        value = row.get(key)
        if isinstance(value, bool):
            continue
        number = _finite(value)
        if number is not None:
            out[key] = int(number) if key in {
                "recent_failures", "opportunities", "available_observations", "event_count"
            } else number
    return out


def _event_state_defaults():
    return {
        "evaluation_started_ts": None,
        "evaluated_windows": 0,
        "rejected_started_ts": None,
        "unreliable_started_ts": None,
    }


def install_context_events(service):
    """Install persisted, deduplicated numeric Context Tournament event reporting."""
    if getattr(service, "_context_events_installed", False):
        return service

    with service.store.lock, service.store.conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS context_tournament_event_state (
                   agent_id TEXT NOT NULL,
                   entity_id TEXT NOT NULL,
                   evaluation_started_ts REAL,
                   evaluated_windows INTEGER NOT NULL DEFAULT 0,
                   rejected_started_ts REAL,
                   unreliable_started_ts REAL,
                   updated_ts REAL NOT NULL,
                   PRIMARY KEY(agent_id, entity_id)
               )"""
        )

    raw_event = service.store.event
    original_sync = service.sync_agent
    original_observe = service.observe_shadow

    def read_event_state(agent_id, entity_id):
        with service.store.conn() as c:
            row = c.execute(
                "SELECT * FROM context_tournament_event_state WHERE agent_id=? AND entity_id=?",
                (str(agent_id), str(entity_id)),
            ).fetchone()
        if not row:
            return _event_state_defaults()
        return {
            "evaluation_started_ts": (
                None if row["evaluation_started_ts"] is None else float(row["evaluation_started_ts"])
            ),
            "evaluated_windows": int(row["evaluated_windows"] or 0),
            "rejected_started_ts": (
                None if row["rejected_started_ts"] is None else float(row["rejected_started_ts"])
            ),
            "unreliable_started_ts": (
                None if row["unreliable_started_ts"] is None else float(row["unreliable_started_ts"])
            ),
        }

    def save_event_state(agent_id, entity_id, row):
        now = time.time()
        with service.store.lock, service.store.conn() as c:
            c.execute(
                """INSERT INTO context_tournament_event_state
                   (agent_id,entity_id,evaluation_started_ts,evaluated_windows,
                    rejected_started_ts,unreliable_started_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id,entity_id) DO UPDATE SET
                     evaluation_started_ts=excluded.evaluation_started_ts,
                     evaluated_windows=excluded.evaluated_windows,
                     rejected_started_ts=excluded.rejected_started_ts,
                     unreliable_started_ts=excluded.unreliable_started_ts,
                     updated_ts=excluded.updated_ts""",
                (
                    str(agent_id), str(entity_id), row.get("evaluation_started_ts"),
                    int(row.get("evaluated_windows") or 0), row.get("rejected_started_ts"),
                    row.get("unreliable_started_ts"), now,
                ),
            )

    def emit(agent_id, level, kind, data):
        raw_event(
            agent_id, level, kind, EVENT_MESSAGES[kind], data,
        )

    def normalize_existing_event(agent_id, level, kind, message, data):
        """Upgrade existing promotion/probation events to the numeric event contract."""
        payload = dict(data or {})
        aid = None if agent_id is None else str(agent_id)
        if kind == "context_feature_promoted" and aid:
            agent = service.store.get_agent_config(aid)
            entity = payload.get("promoted") or payload.get("added")
            removed = payload.get("replaced") or payload.get("removed")
            if agent and entity:
                evidence = _metrics(service, agent, entity)
                preview = dict(
                    getattr(service, "_sensor_quality_replacement_previews", {}).get(
                        (aid, str(entity)), {}
                    ) or {}
                )
                cfg = tournament_config()
                state = service.state(aid)
                payload = {
                    "added": str(entity),
                    "removed": None if removed is None else str(removed),
                    "old_score": evidence.get("old_score"),
                    "new_score": evidence.get("new_score"),
                    "gain": evidence.get("gain"),
                    "samples": int(evidence.get("samples") or 0),
                    "days": float(evidence.get("days") or 0.0),
                    "availability": evidence.get("availability"),
                    "required_gain": _numeric(preview.get("required_gain"), cfg["min_gain"]),
                    "consecutive_wins": int(payload.get("consecutive_wins") or 0),
                    "required_wins": int(cfg["consecutive_wins"]),
                    "completed_windows": int(payload.get("completed_windows") or 0),
                    "schema_revision": int(state.get("schema_revision") or 0),
                }
            return agent_id, level, kind, EVENT_MESSAGES[kind], payload

        if kind == "context_schema_probation_started" and aid:
            history_id = int(payload.get("history_id") or 0)
            history = service.schema_history_by_id(history_id) if history_id and hasattr(service, "schema_history_by_id") else None
            history = dict(history or {})
            old_score = history.get("baseline_score")
            new_score = history.get("challenger_score")
            gain = None if old_score is None or new_score is None else float(new_score) - float(old_score)
            payload = {
                "history_id": history_id,
                "old_score": old_score,
                "new_score": new_score,
                "gain": gain,
                "samples": int(history.get("evaluation_samples") or 0),
                "probation_samples": int(probation_target_samples()),
                "min_rollback_samples": int(MIN_ROLLBACK_SAMPLES),
                "rollback_margin": float(ROLLBACK_MARGIN),
                "old_schema_size": len(history.get("old_schema") or []),
                "new_schema_size": len(history.get("new_schema") or []),
            }
            return agent_id, level, kind, EVENT_MESSAGES[kind], payload

        if kind in ("context_schema_probation_accepted", "context_schema_accepted"):
            kind = "context_schema_accepted"
            old_score = payload.get("old_score")
            new_score = payload.get("new_score")
            gain = payload.get("delta")
            if gain is None and old_score is not None and new_score is not None:
                gain = float(new_score) - float(old_score)
            payload = {
                "history_id": int(payload.get("history_id") or 0),
                "old_score": old_score,
                "new_score": new_score,
                "gain": gain,
                "samples": int(payload.get("samples") or 0),
                "required_samples": int(probation_target_samples()),
                "rollback_margin": float(ROLLBACK_MARGIN),
            }
            return agent_id, level, kind, EVENT_MESSAGES[kind], payload

        if kind == "context_schema_rolled_back":
            old_score = payload.get("old_score")
            new_score = payload.get("new_score")
            gain = payload.get("delta")
            if gain is None and old_score is not None and new_score is not None:
                gain = float(new_score) - float(old_score)
            threshold = None if old_score is None else float(old_score) - float(ROLLBACK_MARGIN)
            payload = {
                "history_id": int(payload.get("history_id") or 0),
                "old_score": old_score,
                "new_score": new_score,
                "gain": gain,
                "samples": int(payload.get("samples") or 0),
                "min_rollback_samples": int(MIN_ROLLBACK_SAMPLES),
                "rollback_margin": float(ROLLBACK_MARGIN),
                "rollback_threshold": threshold,
                "restored_schema_size": len(payload.get("restored_schema") or []),
            }
            return agent_id, level, kind, EVENT_MESSAGES[kind], payload

        return agent_id, level, kind, message, data

    def event_with_numeric_contract(agent_id, level, kind, message, data=None):
        args = normalize_existing_event(agent_id, level, kind, message, data)
        return raw_event(*args)

    service.store.event = event_with_numeric_contract

    def ensure_started_event(agent, tournament, entity_id):
        aid = str(agent["id"])
        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return
        model = service._load_shadow_model(aid, entity_id, len(actions))
        started = _finite(model.get("evaluation_started_ts"))
        if started is None:
            return
        row = read_event_state(aid, entity_id)
        if row.get("evaluation_started_ts") == started:
            return
        cfg = tournament_config()
        challengers = list(tournament.get("challenger_features") or [])
        scores = dict(tournament.get("feature_scores") or {})
        ordered = sorted(challengers, key=lambda eid: (-float(scores.get(eid, 0.0)), str(eid)))
        rank = ordered.index(entity_id) + 1 if entity_id in ordered else 0
        row.update({
            "evaluation_started_ts": started,
            # Do not backfill historical window events on upgrade. A new challenger starts at 0.
            "evaluated_windows": int(model.get("promotion_completed_windows") or 0),
            "rejected_started_ts": None,
            "unreliable_started_ts": None,
        })
        save_event_state(aid, entity_id, row)
        emit(aid, "info", "context_challenger_started", {
            "entity_id": str(entity_id),
            "evaluation_started_ts": started,
            "schema_revision": int(tournament.get("schema_revision") or 0),
            "feature_score": _numeric(scores.get(entity_id)),
            "selection_rank": int(rank),
            "challenger_count": len(challengers),
            "active_feature_count": len(tournament.get("active_features") or []),
            "min_samples": int(cfg["min_samples"]),
            "min_days": float(cfg["min_days"]),
            "min_gain": float(cfg["min_gain"]),
            "required_wins": int(cfg["consecutive_wins"]),
            "evaluation_hours": float(cfg["evaluation_hours"]),
        })

    def emit_rejected(agent, before, after, entity_id):
        aid = str(agent["id"])
        if entity_id in set(after.get("active_features") or []):
            return
        actions = [float(x) for x in action_values(agent)]
        model = service._load_shadow_model(aid, entity_id, len(actions)) if actions else {}
        started = _finite(model.get("evaluation_started_ts")) or 0.0
        row = read_event_state(aid, entity_id)
        if row.get("rejected_started_ts") == started:
            return
        evidence = _metrics(service, agent, entity_id)
        before_scores = dict(before.get("feature_scores") or {})
        after_scores = dict(after.get("feature_scores") or {})
        selected_scores = [
            _numeric(after_scores.get(eid)) for eid in (after.get("challenger_features") or [])
        ]
        cutoff = min(selected_scores) if selected_scores else 0.0
        row["rejected_started_ts"] = started
        save_event_state(aid, entity_id, row)
        payload = {
            "entity_id": str(entity_id),
            "reason_code": "challenger_pool_changed",
            "feature_score": _numeric(before_scores.get(entity_id)),
            "selected_cutoff_score": float(cutoff),
            "old_score": evidence.get("old_score"),
            "new_score": evidence.get("new_score"),
            "gain": evidence.get("gain"),
            "samples": int(evidence.get("samples") or 0),
            "days": float(evidence.get("days") or 0.0),
            "schema_revision": int(after.get("schema_revision") or 0),
            "challenger_count": len(after.get("challenger_features") or []),
        }
        emit(aid, "info", "context_feature_rejected", payload)

    def sync_with_events(agent, **kwargs):
        aid = str(agent["id"])
        before = service.state(aid)
        after = original_sync(agent, **kwargs)
        for entity_id in after.get("challenger_features") or []:
            ensure_started_event(agent, after, entity_id)
        removed = set(before.get("challenger_features") or []) - set(after.get("challenger_features") or [])
        for entity_id in sorted(removed):
            emit_rejected(agent, before, after, entity_id)
        return after

    def emit_completed_windows(agent, tournament, entity_id):
        aid = str(agent["id"])
        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return
        model = service._load_shadow_model(aid, entity_id, len(actions))
        started = _finite(model.get("evaluation_started_ts"))
        if started is None:
            return
        row = read_event_state(aid, entity_id)
        if row.get("evaluation_started_ts") != started:
            ensure_started_event(agent, tournament, entity_id)
            row = read_event_state(aid, entity_id)
        completed = int(model.get("promotion_completed_windows") or 0)
        already = int(row.get("evaluated_windows") or 0)
        if completed <= already:
            return
        history = list(model.get("promotion_window_history") or [])
        cfg = tournament_config()
        # History is bounded. Emit only retained windows that have not been reported yet.
        first_index = max(already + 1, completed - len(history) + 1)
        for window_index in range(first_index, completed + 1):
            history_pos = window_index - (completed - len(history) + 1)
            if history_pos < 0 or history_pos >= len(history):
                continue
            item = dict(history[history_pos] or {})
            old_score = item.get("baseline_score")
            new_score = item.get("challenger_score")
            gain = item.get("gain")
            emit(aid, "info", "context_challenger_evaluated", {
                "entity_id": str(entity_id),
                "window_index": int(window_index),
                "old_score": old_score,
                "new_score": new_score,
                "gain": gain,
                "samples": int(item.get("samples") or 0),
                "win": bool(item.get("win")),
                "min_gain": float(cfg["min_gain"]),
                "consecutive_wins": int(model.get("promotion_consecutive_wins") or 0),
                "required_wins": int(cfg["consecutive_wins"]),
                "days": float(availability_stats(model)[1] or 0.0),
                "availability": availability_stats(model)[0],
                "schema_revision": int(tournament.get("schema_revision") or 0),
            })
        row["evaluated_windows"] = completed
        save_event_state(aid, entity_id, row)

    def emit_unreliable_if_blocked(agent, tournament, entity_id):
        aid = str(agent["id"])
        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return
        model = service._load_shadow_model(aid, entity_id, len(actions))
        if str(model.get("promotion_blocked_reason") or "") != "sensor_quality":
            return
        started = _finite(model.get("evaluation_started_ts")) or 0.0
        row = read_event_state(aid, entity_id)
        if row.get("unreliable_started_ts") == started:
            return
        preview = dict(
            getattr(service, "_sensor_quality_replacement_previews", {}).get(
                (aid, str(entity_id)), {}
            ) or {}
        )
        evidence = _metrics(service, agent, entity_id)
        quality = _quality_payload(service, aid, entity_id)
        payload = {
            "entity_id": str(entity_id),
            "replaced_entity": preview.get("replaced"),
            "old_score": evidence.get("old_score"),
            "new_score": evidence.get("new_score"),
            "gain": evidence.get("gain"),
            "samples": int(evidence.get("samples") or 0),
            "days": float(evidence.get("days") or 0.0),
            "required_gain": _numeric(preview.get("required_gain"), tournament_config()["min_gain"]),
            "quality_adjusted_gain": _numeric(preview.get("quality_adjusted_gain")),
            "challenger_ranking_score": _numeric(preview.get("challenger_ranking_score")),
            "incumbent_ranking_score": _numeric(preview.get("incumbent_ranking_score")),
            "schema_revision": int(tournament.get("schema_revision") or 0),
            **quality,
        }
        row["unreliable_started_ts"] = started
        save_event_state(aid, entity_id, row)
        emit(aid, "warning", "context_sensor_unreliable", payload)

    def observe_with_events(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        before = service.state(aid)
        for entity_id in before.get("challenger_features") or []:
            ensure_started_event(agent, before, entity_id)

        result = original_observe(agent, state_map, changed_entities)

        after = service.state(aid)
        entities = list(dict.fromkeys(
            list(before.get("challenger_features") or [])
            + list(after.get("challenger_features") or [])
        ))
        for entity_id in entities:
            emit_completed_windows(agent, after, entity_id)
            if entity_id in set(after.get("challenger_features") or []):
                emit_unreliable_if_blocked(agent, after, entity_id)
        return result

    service.sync_agent = sync_with_events
    service.observe_shadow = observe_with_events
    service.context_event_contract = {
        "events": list(EVENT_MESSAGES),
        "generated_text": False,
        "decision_evidence": "structured_numeric",
    }
    service._context_events_installed = True
    return service
