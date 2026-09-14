"""Automatic promotion for Sensor Tournament challengers.

A challenger may enter the live feature schema only after proving incremental value on
future/prequential data.  Promotion is deliberately conservative:

- at least ``context_tournament_min_samples`` out-of-sample scored predictions,
- at least ``context_tournament_min_days`` of observed availability,
- cumulative gain >= ``context_tournament_min_gain``,
- the same minimum gain in ``context_tournament_consecutive_wins`` consecutive,
  non-overlapping evaluation windows,
- and the per-agent promotion cooldown has expired.

The tournament never creates an ActionIntent and never calls Executor or Home Assistant
services.  A successful promotion only migrates the in-memory policy feature schema,
preserves matching learned weights, persists the model, and lets the normal runtime use
that schema on subsequent inference passes.
"""
import json
import math
import threading
import time

from context import action_values, is_fast_reactive_agent
from context_tournament_metrics import availability_stats, metric_row
from manual_context_learning import _migrate_schema
from settings import OPTIONS


PROMOTION_EPOCH_VERSION = 1
WINDOW_HISTORY_LIMIT = 12


def tournament_config(options=None):
    opts = options or OPTIONS
    return {
        "enabled": bool(opts.get("context_tournament_enabled", True)),
        "min_samples": max(1, int(opts.get("context_tournament_min_samples", 40))),
        "min_days": max(0.0, float(opts.get("context_tournament_min_days", 3))),
        "min_gain": max(0.0, min(1.0, float(opts.get("context_tournament_min_gain", 0.03)))),
        "consecutive_wins": max(1, int(opts.get("context_tournament_consecutive_wins", 3))),
        "evaluation_hours": max(1.0, float(opts.get("context_tournament_evaluation_hours", 24))),
        "cooldown_hours": max(0.0, float(opts.get("context_tournament_cooldown_hours", 24))),
    }


def _counter_list(model, name, n):
    values = list(model.get(name) or [])
    if len(values) != int(n):
        values = [0] * int(n)
    return values


def _reset_window(model, action_count, start_ts, hours):
    model["promotion_window_hours"] = float(hours)
    model["promotion_window_start_ts"] = float(start_ts)
    model["promotion_window_end_ts"] = float(start_ts) + float(hours) * 3600.0
    model["promotion_window_samples"] = 0
    model["promotion_window_class_totals"] = [0] * int(action_count)
    model["promotion_window_active_correct_by_class"] = [0] * int(action_count)
    model["promotion_window_shadow_correct_by_class"] = [0] * int(action_count)
    model["promotion_window_active_abs_error_sum"] = 0.0
    model["promotion_window_shadow_abs_error_sum"] = 0.0


def _snapshot_seen(model, action_count):
    model["promotion_seen_samples"] = int(model.get("samples") or 0)
    model["promotion_seen_class_totals"] = _counter_list(model, "class_totals", action_count)
    model["promotion_seen_active_correct_by_class"] = _counter_list(
        model, "active_correct_by_class", action_count
    )
    model["promotion_seen_shadow_correct_by_class"] = _counter_list(
        model, "shadow_correct_by_class", action_count
    )
    model["promotion_seen_active_abs_error_sum"] = float(
        model.get("active_abs_error_sum") or 0.0
    )
    model["promotion_seen_shadow_abs_error_sum"] = float(
        model.get("shadow_abs_error_sum") or 0.0
    )


def ensure_promotion_epoch(model, action_count, now, config=None):
    """Initialize window bookkeeping without reusing unsegmented historical proof."""
    cfg = config or tournament_config()
    hours = float(cfg["evaluation_hours"])
    version_ok = int(model.get("promotion_epoch_version") or 0) == PROMOTION_EPOCH_VERSION
    same_hours = abs(float(model.get("promotion_window_hours") or 0.0) - hours) < 1e-9
    if version_ok and same_hours and model.get("promotion_window_start_ts") is not None:
        return False

    # A challenger selected after this code is installed has zero scored samples, so its
    # first window begins exactly at the future-only evaluation epoch.  Existing deployed
    # challengers can already contain cumulative proof but no per-window segmentation;
    # start their win streak now instead of retroactively manufacturing window wins.
    samples = int(model.get("samples") or 0)
    evaluation_start = model.get("evaluation_started_ts")
    start = float(evaluation_start) if samples <= 0 and evaluation_start is not None else float(now)
    model["promotion_epoch_version"] = PROMOTION_EPOCH_VERSION
    model["promotion_consecutive_wins"] = 0
    model["promotion_completed_windows"] = 0
    model["promotion_last_window_gain"] = None
    model["promotion_last_window_score"] = None
    model["promotion_window_history"] = []
    model["promotion_blocked_reason"] = None
    _reset_window(model, action_count, start, hours)
    _snapshot_seen(model, action_count)
    return True


def _window_model(model, action_count):
    return {
        "samples": int(model.get("promotion_window_samples") or 0),
        "class_totals": _counter_list(model, "promotion_window_class_totals", action_count),
        "active_correct_by_class": _counter_list(
            model, "promotion_window_active_correct_by_class", action_count
        ),
        "shadow_correct_by_class": _counter_list(
            model, "promotion_window_shadow_correct_by_class", action_count
        ),
        "active_abs_error_sum": float(model.get("promotion_window_active_abs_error_sum") or 0.0),
        "shadow_abs_error_sum": float(model.get("promotion_window_shadow_abs_error_sum") or 0.0),
    }


def _finalize_one_window(model, actions, config):
    row = metric_row(_window_model(model, len(actions)), actions)
    gain = row.get("gain")
    won = gain is not None and float(gain) >= float(config["min_gain"])
    model["promotion_consecutive_wins"] = (
        int(model.get("promotion_consecutive_wins") or 0) + 1 if won else 0
    )
    model["promotion_completed_windows"] = int(model.get("promotion_completed_windows") or 0) + 1
    model["promotion_last_window_gain"] = gain
    model["promotion_last_window_score"] = row.get("challenger_score")
    history = list(model.get("promotion_window_history") or [])
    history.append({
        "start_ts": float(model.get("promotion_window_start_ts") or 0.0),
        "end_ts": float(model.get("promotion_window_end_ts") or 0.0),
        "samples": int(row.get("samples") or 0),
        "baseline_score": row.get("baseline_score"),
        "challenger_score": row.get("challenger_score"),
        "gain": gain,
        "win": bool(won),
    })
    model["promotion_window_history"] = history[-WINDOW_HISTORY_LIMIT:]
    return bool(won)


def advance_windows(model, actions, now, config=None):
    """Close elapsed non-overlapping windows. Empty/invalid windows break the streak."""
    cfg = config or tournament_config()
    ensure_promotion_epoch(model, len(actions), now, cfg)
    changed = False
    while float(now) >= float(model.get("promotion_window_end_ts") or math.inf):
        end_ts = float(model["promotion_window_end_ts"])
        _finalize_one_window(model, actions, cfg)
        _reset_window(model, len(actions), end_ts, cfg["evaluation_hours"])
        changed = True
    return changed


def absorb_new_scored_evidence(model, actions, now, config=None):
    """Add only samples scored since the previous observation into the current window."""
    cfg = config or tournament_config()
    ensure_promotion_epoch(model, len(actions), now, cfg)

    current_samples = int(model.get("samples") or 0)
    seen_samples = int(model.get("promotion_seen_samples") or 0)
    delta_samples = max(0, current_samples - seen_samples)
    scored_ts = float(model.get("last_scored_ts") or now)
    if delta_samples <= 0:
        advance_windows(model, actions, now, cfg)
        return 0

    # The new scored event belongs to the window containing its scoring timestamp.  Any
    # elapsed windows are finalized first, so a future event can never improve an older
    # window after seeing its outcome.
    advance_windows(model, actions, scored_ts, cfg)
    n = len(actions)
    if n == 2:
        current_totals = _counter_list(model, "class_totals", n)
        current_active = _counter_list(model, "active_correct_by_class", n)
        current_shadow = _counter_list(model, "shadow_correct_by_class", n)
        seen_totals = _counter_list(model, "promotion_seen_class_totals", n)
        seen_active = _counter_list(model, "promotion_seen_active_correct_by_class", n)
        seen_shadow = _counter_list(model, "promotion_seen_shadow_correct_by_class", n)
        window_totals = _counter_list(model, "promotion_window_class_totals", n)
        window_active = _counter_list(model, "promotion_window_active_correct_by_class", n)
        window_shadow = _counter_list(model, "promotion_window_shadow_correct_by_class", n)
        for idx in range(n):
            window_totals[idx] += max(0, int(current_totals[idx]) - int(seen_totals[idx]))
            window_active[idx] += max(0, int(current_active[idx]) - int(seen_active[idx]))
            window_shadow[idx] += max(0, int(current_shadow[idx]) - int(seen_shadow[idx]))
        model["promotion_window_class_totals"] = window_totals
        model["promotion_window_active_correct_by_class"] = window_active
        model["promotion_window_shadow_correct_by_class"] = window_shadow
    else:
        active_sum = float(model.get("active_abs_error_sum") or 0.0)
        shadow_sum = float(model.get("shadow_abs_error_sum") or 0.0)
        seen_active_sum = float(model.get("promotion_seen_active_abs_error_sum") or 0.0)
        seen_shadow_sum = float(model.get("promotion_seen_shadow_abs_error_sum") or 0.0)
        model["promotion_window_active_abs_error_sum"] = float(
            model.get("promotion_window_active_abs_error_sum") or 0.0
        ) + max(0.0, active_sum - seen_active_sum)
        model["promotion_window_shadow_abs_error_sum"] = float(
            model.get("promotion_window_shadow_abs_error_sum") or 0.0
        ) + max(0.0, shadow_sum - seen_shadow_sum)

    model["promotion_window_samples"] = int(model.get("promotion_window_samples") or 0) + delta_samples
    _snapshot_seen(model, n)
    advance_windows(model, actions, now, cfg)
    return delta_samples


def promotion_eligibility(model, actions, now, last_promotion_ts=None, options=None):
    cfg = tournament_config(options)
    metrics = metric_row(model, actions)
    _, days_observed = availability_stats(model)
    cooldown_seconds = float(cfg["cooldown_hours"]) * 3600.0
    cooldown_remaining = 0.0
    if last_promotion_ts is not None and cooldown_seconds > 0:
        cooldown_remaining = max(0.0, float(last_promotion_ts) + cooldown_seconds - float(now))
    gain = metrics.get("gain")
    checks = {
        "enabled": bool(cfg["enabled"]),
        "samples": int(metrics.get("samples") or 0) >= int(cfg["min_samples"]),
        "days": float(days_observed or 0.0) >= float(cfg["min_days"]),
        "gain": gain is not None and float(gain) >= float(cfg["min_gain"]),
        "consecutive_wins": int(model.get("promotion_consecutive_wins") or 0) >= int(cfg["consecutive_wins"]),
        "cooldown": cooldown_remaining <= 0.0,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "config": cfg,
        "samples": int(metrics.get("samples") or 0),
        "days_observed": float(days_observed or 0.0),
        "gain": gain,
        "consecutive_wins": int(model.get("promotion_consecutive_wins") or 0),
        "completed_windows": int(model.get("promotion_completed_windows") or 0),
        "cooldown_remaining_seconds": cooldown_remaining,
    }


def _selection_limit(agent):
    dims = int(OPTIONS.get("feature_dimensions", 128))
    limit = min(int(OPTIONS.get("max_context_entities", 28)), max(4, (dims - 9) // 4))
    if is_fast_reactive_agent(agent):
        limit = min(limit, max(2, int(OPTIONS.get("fast_max_context_entities", 8))))
    return limit


def _ensure_promotion_table(service):
    with service.store.lock, service.store.conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS context_tournament_promotions (
                   agent_id TEXT PRIMARY KEY,
                   last_promotion_ts REAL,
                   promoted_entity TEXT,
                   replaced_entity TEXT,
                   schema_revision INTEGER,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   updated_ts REAL NOT NULL
               )"""
        )


def _promotion_state(service, agent_id):
    with service.store.conn() as c:
        row = c.execute(
            "SELECT * FROM context_tournament_promotions WHERE agent_id=?", (str(agent_id),)
        ).fetchone()
    if not row:
        return {
            "last_promotion_ts": None,
            "promoted_entity": None,
            "replaced_entity": None,
            "schema_revision": None,
            "details": {},
        }
    try:
        details = json.loads(row["details_json"] or "{}")
    except Exception:
        details = {}
    return {
        "last_promotion_ts": None if row["last_promotion_ts"] is None else float(row["last_promotion_ts"]),
        "promoted_entity": row["promoted_entity"],
        "replaced_entity": row["replaced_entity"],
        "schema_revision": row["schema_revision"],
        "details": details if isinstance(details, dict) else {},
    }


def _save_promotion_state(service, agent_id, promoted, replaced, schema_revision, details, now):
    raw = json.dumps(details or {}, separators=(",", ":"), sort_keys=True)
    with service.store.lock, service.store.conn() as c:
        c.execute(
            """INSERT INTO context_tournament_promotions
               (agent_id,last_promotion_ts,promoted_entity,replaced_entity,schema_revision,details_json,updated_ts)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 last_promotion_ts=excluded.last_promotion_ts,
                 promoted_entity=excluded.promoted_entity,
                 replaced_entity=excluded.replaced_entity,
                 schema_revision=excluded.schema_revision,
                 details_json=excluded.details_json,
                 updated_ts=excluded.updated_ts""",
            (str(agent_id), float(now), str(promoted), replaced, int(schema_revision), raw, float(now)),
        )


def _choose_schema_after_promotion(agent, policy, challenger, tournament):
    active = list(policy.schema.entities)
    if challenger in active:
        return active, None
    limit = _selection_limit(agent)
    if len(active) < limit:
        return active + [challenger], None

    # Explicitly configured inputs are user intent and are never silently displaced.
    protected = {str(x) for x in (agent.get("input_entities") or [])}
    removable = [(idx, eid) for idx, eid in enumerate(active) if eid not in protected]
    if not removable:
        return None, None
    scores = dict(tournament.get("feature_scores") or {})
    # Replace the weakest discovery-ranked active feature.  Gain itself still decides
    # whether the challenger wins; discovery score is used only to choose which slot to
    # free when the compact schema is already at its hard limit.
    idx, replaced = min(
        removable,
        key=lambda item: (float(scores.get(item[1], 0.0)), -int(item[0]), item[1]),
    )
    result = list(active)
    result[idx] = challenger
    return result, replaced


def _selection_meta_for_promotion(policy, challenger, replaced):
    meta = dict(getattr(policy, "selection_meta", {}) or {})
    reasons = {k: list(v) for k, v in dict(meta.get("selection_reasons") or {}).items()}
    if replaced:
        reasons.pop(replaced, None)
    reasons.setdefault(challenger, [])
    if "sensor-tournament" not in reasons[challenger]:
        reasons[challenger].append("sensor-tournament")
    meta["selection_reasons"] = reasons
    meta["sensor_tournament_promoted"] = challenger
    meta["sensor_tournament_replaced"] = replaced
    if replaced:
        for key in ("primary_local_sensors", "primary_behavioural_drivers", "upstream_sensors"):
            meta[key] = [x for x in (meta.get(key) or []) if x != replaced]
        if meta.get("primary_local_sensor") == replaced:
            meta["primary_local_sensor"] = None
        if meta.get("primary_occupancy_sensor") == replaced:
            meta["primary_occupancy_sensor"] = None
        for key in ("causal_presence_scores", "causal_behaviour_scores"):
            values = dict(meta.get(key) or {})
            values.pop(replaced, None)
            meta[key] = values
    return meta


def install_promotion(service):
    """Attach automatic windowed promotion after future-only metrics are installed."""
    if getattr(service, "_auto_promotion_installed", False):
        return service
    _ensure_promotion_table(service)
    lock = threading.RLock()
    original_observe = service.observe_shadow
    original_status = service.shadow_status
    original_sync = service.sync_agent

    def initialize_models(agent, tournament, now):
        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return
        cfg = tournament_config()
        for challenger in tournament.get("challenger_features") or []:
            model = service._load_shadow_model(agent["id"], challenger, len(actions))
            if ensure_promotion_epoch(model, len(actions), now, cfg):
                service._save_shadow_model(agent["id"], challenger, model)

    def sync_with_promotion_windows(agent, **kwargs):
        tournament = original_sync(agent, **kwargs)
        initialize_models(agent, tournament, time.time())
        return tournament

    def attempt_promotion(agent, challenger, model, actions, now):
        aid = str(agent["id"])
        with lock:
            tournament = service.state(aid)
            if challenger not in set(tournament.get("challenger_features") or []):
                return False
            promotion_state = _promotion_state(service, aid)
            eligibility = promotion_eligibility(
                model, actions, now, promotion_state.get("last_promotion_ts")
            )
            if not eligibility["ready"]:
                return False
            policy = (getattr(service.engine, "models", {}) or {}).get(aid)
            if policy is None:
                model["promotion_blocked_reason"] = "active_policy_not_cached"
                return False
            new_entities, replaced = _choose_schema_after_promotion(
                agent, policy, challenger, tournament
            )
            if new_entities is None:
                model["promotion_blocked_reason"] = "no_replaceable_schema_slot"
                return False
            new_meta = _selection_meta_for_promotion(policy, challenger, replaced)
            migration = _migrate_schema(policy, new_entities, new_meta)
            if not migration.get("changed"):
                return False

            service.store.save_model(aid, policy.serialize())
            refreshed = service.sync_agent(
                agent,
                policy=policy,
                feature_scores=tournament.get("feature_scores") or {},
                evaluated_at=now,
            )
            details = {
                "gain": eligibility.get("gain"),
                "samples": eligibility.get("samples"),
                "days_observed": eligibility.get("days_observed"),
                "consecutive_wins": eligibility.get("consecutive_wins"),
                "completed_windows": eligibility.get("completed_windows"),
                "added": migration.get("added") or [],
                "removed": migration.get("removed") or [],
            }
            _save_promotion_state(
                service, aid, challenger, replaced,
                int(refreshed.get("schema_revision") or 0), details, now,
            )
            service.store.event(
                aid, "info", "context_feature_promoted",
                f"Sensor Tournament promoted {challenger} into the active feature schema",
                {"promoted": challenger, "replaced": replaced, **details},
            )
            try:
                service.engine.wake_event.set()
            except Exception:
                pass
            return True

    def observe_with_auto_promotion(agent, state_map=None, changed_entities=None):
        result = original_observe(agent, state_map, changed_entities)
        aid = str(agent["id"])
        tournament = service.state(aid)
        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return result
        cfg = tournament_config()
        now = time.time()
        for challenger in list(tournament.get("challenger_features") or []):
            model = service._load_shadow_model(aid, challenger, len(actions))
            ensure_promotion_epoch(model, len(actions), now, cfg)
            absorb_new_scored_evidence(model, actions, now, cfg)
            service._save_shadow_model(aid, challenger, model)
            if cfg["enabled"]:
                attempt_promotion(agent, challenger, model, actions, now)
        return result

    def status_with_auto_promotion(agent):
        payload = original_status(agent)
        actions = [float(x) for x in action_values(agent)]
        state = _promotion_state(service, agent["id"])
        cfg = tournament_config()
        now = time.time()
        for row in payload.get("challengers") or []:
            challenger = row.get("entity_id")
            model = service._load_shadow_model(agent["id"], challenger, len(actions)) if actions else {}
            if actions:
                ensure_promotion_epoch(model, len(actions), now, cfg)
                eligibility = promotion_eligibility(
                    model, actions, now, state.get("last_promotion_ts")
                )
            else:
                eligibility = {"ready": False, "checks": {}, "consecutive_wins": 0,
                               "completed_windows": 0, "cooldown_remaining_seconds": 0.0}
            row["promotion_ready"] = bool(eligibility.get("ready"))
            row["promotion_checks"] = eligibility.get("checks") or {}
            row["consecutive_wins"] = int(model.get("promotion_consecutive_wins") or 0)
            row["completed_windows"] = int(model.get("promotion_completed_windows") or 0)
            row["last_window_gain"] = model.get("promotion_last_window_gain")
            row["evaluation_window_start_ts"] = model.get("promotion_window_start_ts")
            row["evaluation_window_end_ts"] = model.get("promotion_window_end_ts")
            row["promotion_blocked_reason"] = model.get("promotion_blocked_reason")
            row["cooldown_remaining_seconds"] = float(
                eligibility.get("cooldown_remaining_seconds") or 0.0
            )
        payload["auto_promotion"] = {
            **cfg,
            "last_promotion_ts": state.get("last_promotion_ts"),
            "last_promoted_entity": state.get("promoted_entity"),
            "last_replaced_entity": state.get("replaced_entity"),
        }
        return payload

    service.sync_agent = sync_with_promotion_windows
    service.observe_shadow = observe_with_auto_promotion
    service.shadow_status = status_with_auto_promotion
    service.promotion_status = lambda agent: _promotion_state(service, agent["id"])
    service._auto_promotion_installed = True
    return service
