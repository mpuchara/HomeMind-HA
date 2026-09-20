"""Incremental value metrics for Sensor Tournament challengers.

Step 7 evaluates whether a challenger adds predictive value beyond the active policy.
Step 8 makes that proof strictly future/prequential: candidate discovery may use older
history, but a challenger evaluation epoch starts only after the sensor is selected.
Every evaluation event is scored before either the challenger residual model or the
active replay policy can learn from that event.

Metrics are paired on the same prequential samples:
- binary targets: balanced accuracy (primary), exposed as a higher-is-better score;
- continuous targets: normalized MAE, exposed as score = 1 - nMAE.

Therefore ``gain = challenger_score - baseline_score`` is exactly
``Loss(active) - Loss(active + sensor)`` for both target families.
"""
import math
import threading
import time

from context import action_values, context_scalar, target_value


PERSIST_INTERVAL_SECONDS = 60.0
PREQUENTIAL_EPOCH_VERSION = 1


def balanced_accuracy(class_totals, correct_by_class):
    """Balanced accuracy for a binary target; both classes must be represented."""
    totals = [int(x or 0) for x in (class_totals or [])]
    correct = [int(x or 0) for x in (correct_by_class or [])]
    if len(totals) != 2 or len(correct) != 2 or any(n <= 0 for n in totals):
        return None
    return sum(correct[i] / totals[i] for i in range(2)) / 2.0


def metric_row(model, actions):
    """Return a stable higher-is-better score and incremental gain."""
    samples = int(model.get("samples") or 0)
    if len(actions) == 2:
        baseline = balanced_accuracy(
            model.get("class_totals"), model.get("active_correct_by_class")
        )
        challenger = balanced_accuracy(
            model.get("class_totals"), model.get("shadow_correct_by_class")
        )
        gain = (challenger - baseline) if baseline is not None and challenger is not None else None
        return {
            "metric": "balanced_accuracy",
            "baseline_score": baseline,
            "challenger_score": challenger,
            "gain": gain,
            "samples": samples,
            "baseline_loss": (1.0 - baseline) if baseline is not None else None,
            "challenger_loss": (1.0 - challenger) if challenger is not None else None,
        }

    value_range = max(1e-9, max(actions) - min(actions)) if actions else 1.0
    if samples <= 0:
        return {
            "metric": "normalized_mae",
            "baseline_score": None,
            "challenger_score": None,
            "gain": None,
            "samples": 0,
            "baseline_loss": None,
            "challenger_loss": None,
            "baseline_mae": None,
            "challenger_mae": None,
            "baseline_nmae": None,
            "challenger_nmae": None,
        }
    baseline_mae = float(model.get("active_abs_error_sum") or 0.0) / samples
    challenger_mae = float(model.get("shadow_abs_error_sum") or 0.0) / samples
    baseline_nmae = baseline_mae / value_range
    challenger_nmae = challenger_mae / value_range
    baseline_score = 1.0 - baseline_nmae
    challenger_score = 1.0 - challenger_nmae
    return {
        "metric": "normalized_mae",
        "baseline_score": baseline_score,
        "challenger_score": challenger_score,
        "gain": baseline_nmae - challenger_nmae,
        "samples": samples,
        "baseline_loss": baseline_nmae,
        "challenger_loss": challenger_nmae,
        "baseline_mae": baseline_mae,
        "challenger_mae": challenger_mae,
        "baseline_nmae": baseline_nmae,
        "challenger_nmae": challenger_nmae,
    }


def availability_stats(model):
    opportunities = int(model.get("observation_opportunities") or 0)
    available = int(model.get("available_observations") or 0)
    first_ts = model.get("first_observed_ts")
    last_ts = model.get("last_observed_ts")
    days = 0.0 if first_ts is None or last_ts is None else max(
        0.0, (float(last_ts) - float(first_ts)) / 86400.0
    )
    return (available / opportunities) if opportunities else None, days


def install_metrics(service):
    """Attach additive-value metrics and future-only evaluation epochs.

    The extension wraps only shadow bookkeeping. It never creates an ActionIntent, never
    invokes Executor/HA services and never changes feature schema. Challenger proof is
    reset when a sensor enters the tournament, when the active schema changes, or when a
    different learned champion revision becomes authoritative. Deterministic lazy
    decay may change model_revision for provenance, but does not invalidate a paired
    future-only Tournament epoch.
    """
    if getattr(service, "_incremental_metrics_installed", False):
        return service

    lock = threading.RLock()
    last_persist = {}
    current_actual = {}
    current_agent = {}
    champion_revisions = {}

    original_score = service._score_shadow_sample
    original_observe = service.observe_shadow
    original_status = service.shadow_status
    original_sync = service.sync_agent

    def ensure_fields(model, action_count):
        n = int(action_count)
        if len(model.get("class_totals") or []) != n:
            model["class_totals"] = [0] * n
        if len(model.get("active_correct_by_class") or []) != n:
            model["active_correct_by_class"] = [0] * n
        if len(model.get("shadow_correct_by_class") or []) != n:
            model["shadow_correct_by_class"] = [0] * n
        model.setdefault("active_abs_error_sum", 0.0)
        model.setdefault("shadow_abs_error_sum", 0.0)
        model.setdefault("observation_opportunities", 0)
        model.setdefault("available_observations", 0)
        model.setdefault("first_observed_ts", None)
        model.setdefault("last_observed_ts", None)
        return model

    def champion_revision(agent_id, policy=None):
        aid = str(agent_id)
        candidate = policy
        if candidate is None:
            candidate = (getattr(service.engine, "models", {}) or {}).get(aid)
        revision = None
        if candidate is not None:
            revision = getattr(candidate, "tournament_revision", None)
            if revision is None:
                # Backward-compatible fallback for policies serialized before 0.14.53.
                revision = getattr(candidate, "model_revision", None)
        if revision is not None:
            revision = str(revision)
            with lock:
                champion_revisions[aid] = revision
            return revision
        with lock:
            return champion_revisions.get(aid)

    def clear_transient_prediction(agent_id, challenger):
        aid = str(agent_id)
        with service.lock:
            runtime = service._shadow_runtime.get(aid)
            if not runtime:
                return
            for key in ("pending", "predictions"):
                values = dict(runtime.get(key) or {})
                values.pop(str(challenger), None)
                runtime[key] = values

    def reset_evaluation_epoch(agent, challenger, action_count, tournament, now, reason,
                               policy=None):
        aid = str(agent["id"])
        model = service._blank_shadow_model(action_count)
        ensure_fields(model, action_count)
        model["prequential_epoch_version"] = PREQUENTIAL_EPOCH_VERSION
        model["evaluation_schema_revision"] = int(tournament.get("schema_revision") or 0)
        model["evaluation_champion_revision"] = champion_revision(aid, policy)
        model["evaluation_started_ts"] = float(now)
        model["evaluation_reason"] = str(reason)
        service._save_shadow_model(aid, challenger, model)
        clear_transient_prediction(aid, challenger)
        with lock:
            last_persist[(aid, str(challenger))] = float(now)
        return model

    def ensure_future_epoch(agent, challenger, action_count, tournament, now):
        aid = str(agent["id"])
        model = service._load_shadow_model(aid, challenger, action_count)
        ensure_fields(model, action_count)
        expected_schema = int(tournament.get("schema_revision") or 0)
        expected_champion = champion_revision(aid)
        model_champion = model.get("evaluation_champion_revision")
        stale = (
            int(model.get("prequential_epoch_version") or 0) != PREQUENTIAL_EPOCH_VERSION
            or model.get("evaluation_started_ts") is None
            or int(model.get("evaluation_schema_revision") or -1) != expected_schema
            or (expected_champion is not None and str(model_champion or "") != str(expected_champion))
        )
        if stale:
            reason = "future_only_upgrade"
            if model.get("evaluation_started_ts") is not None:
                if int(model.get("evaluation_schema_revision") or -1) != expected_schema:
                    reason = "active_schema_changed"
                elif expected_champion is not None and str(model_champion or "") != str(expected_champion):
                    reason = "champion_model_changed"
            model = reset_evaluation_epoch(
                agent, challenger, action_count, tournament, now, reason
            )
        return model

    def maybe_persist(agent_id, challenger, model, now, force=False):
        key = (str(agent_id), str(challenger))
        with lock:
            last = float(last_persist.get(key) or 0.0)
            if not force and now - last < PERSIST_INTERVAL_SECONDS:
                return
            last_persist[key] = now
        service._save_shadow_model(agent_id, challenger, model)

    def sync_with_future_epochs(agent, **kwargs):
        aid = str(agent["id"])
        before = service.state(aid)
        policy = kwargs.get("policy")
        old_challengers = set(before.get("challenger_features") or [])
        old_schema_revision = int(before.get("schema_revision") or 0)
        old_champion = champion_revision(aid)
        new_champion = champion_revision(aid, policy)
        after = original_sync(agent, **kwargs)
        new_challengers = set(after.get("challenger_features") or [])
        schema_changed = int(after.get("schema_revision") or 0) != old_schema_revision
        champion_changed = (
            old_champion is not None and new_champion is not None
            and str(old_champion) != str(new_champion)
        )
        reset_all = schema_changed or champion_changed
        targets = new_challengers if reset_all else (new_challengers - old_challengers)
        if targets:
            actions = [float(x) for x in action_values(agent)]
            now = time.time()
            reason = (
                "active_schema_changed" if schema_changed else
                "champion_model_changed" if champion_changed else
                "challenger_selected"
            )
            for challenger in sorted(targets):
                reset_evaluation_epoch(
                    agent, challenger, len(actions), after, now, reason, policy=policy
                )
        return after

    def score_with_incremental_metrics(agent_id, challenger, pending, actual_idx, action_count, now):
        aid = str(agent_id)
        agent = current_agent.get(aid)
        actions = [float(x) for x in action_values(agent)] if agent is not None else []
        tournament = service.state(aid)
        model = ensure_future_epoch(agent, challenger, action_count, tournament, now)

        active_idx = int(pending["active_index"])
        shadow_idx = int(pending["shadow_index"])
        actual_idx = int(actual_idx)
        if int(action_count) == 2:
            model["class_totals"][actual_idx] += 1
            model["active_correct_by_class"][actual_idx] += int(active_idx == actual_idx)
            model["shadow_correct_by_class"][actual_idx] += int(shadow_idx == actual_idx)
        elif actions:
            raw_actual = current_actual.get(aid)
            actual_value = float(raw_actual) if raw_actual is not None else float(actions[actual_idx])
            model["active_abs_error_sum"] = float(model.get("active_abs_error_sum") or 0.0) + abs(
                float(pending.get("active_value", actions[active_idx])) - actual_value
            )
            model["shadow_abs_error_sum"] = float(model.get("shadow_abs_error_sum") or 0.0) + abs(
                float(pending.get("shadow_value", actions[shadow_idx])) - actual_value
            )

        # Prequential order: paired baseline/challenger scores are recorded first.  The
        # base scorer then learns the observed class/value into its residual table.
        return original_score(agent_id, challenger, pending, actual_idx, action_count, now)

    def observe_with_incremental_metrics(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        states = dict(state_map or getattr(service.engine, "state_map", {}) or {})
        actions = [float(x) for x in action_values(agent)]
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        now = time.time()
        with lock:
            current_agent[aid] = agent
            current_actual[aid] = current

        # Candidate discovery can be based on historical relevance, but availability and
        # metric evidence begin only after selection into a fresh future-only epoch.
        if actions and current is not None:
            tournament = service.state(aid)
            for challenger in tournament.get("challenger_features") or []:
                model = ensure_future_epoch(agent, challenger, len(actions), tournament, now)
                model["observation_opportunities"] = int(model.get("observation_opportunities") or 0) + 1
                raw_state = states.get(challenger)
                raw_text = str((raw_state or {}).get("state") or "").strip().lower()
                available = bool(raw_state) and raw_text not in ("", "unknown", "unavailable", "none")
                if available:
                    scalar = context_scalar(challenger, raw_state, agent)
                    try:
                        available = scalar is not None and math.isfinite(float(scalar))
                    except (TypeError, ValueError):
                        available = False
                if available:
                    model["available_observations"] = int(model.get("available_observations") or 0) + 1
                    if model.get("first_observed_ts") is None:
                        model["first_observed_ts"] = now
                    model["last_observed_ts"] = now
                maybe_persist(aid, challenger, model, now, force=False)

        return original_observe(agent, states, changed_entities)

    def status_with_incremental_metrics(agent):
        payload = original_status(agent)
        actions = [float(x) for x in action_values(agent)]
        tournament = service.state(agent["id"])
        now = time.time()
        for row in payload.get("challengers") or []:
            entity_id = row.get("entity_id")
            model = (
                ensure_future_epoch(agent, entity_id, len(actions), tournament, now)
                if actions else {}
            )
            ensure_fields(model, len(actions))
            metrics = metric_row(model, actions)
            availability, days_observed = availability_stats(model)
            row.update(metrics)
            row["availability"] = availability
            row["days_observed"] = days_observed
            row["evaluation_mode"] = "prequential_future_only"
            row["evaluation_started_ts"] = model.get("evaluation_started_ts")
            row["evaluation_schema_revision"] = model.get("evaluation_schema_revision")
            row["evaluation_champion_revision"] = model.get("evaluation_champion_revision")
            row["evaluation_reason"] = model.get("evaluation_reason")
            # Compatibility alias only. New promotion logic should consume `gain`.
            row["accuracy_gain"] = metrics["gain"] if metrics["metric"] == "balanced_accuracy" else None
        payload["score_definition"] = "gain = Loss(active) - Loss(active + sensor)"
        payload["evaluation_order"] = "predict -> score -> learn"
        payload["evaluation_scope"] = "future events after challenger selection"
        payload["binary_metric"] = "balanced_accuracy"
        payload["continuous_metric"] = "normalized_mae"
        return payload

    service.sync_agent = sync_with_future_epochs
    service._score_shadow_sample = score_with_incremental_metrics
    service.observe_shadow = observe_with_incremental_metrics
    service.shadow_status = status_with_incremental_metrics
    service._incremental_metrics_installed = True
    return service