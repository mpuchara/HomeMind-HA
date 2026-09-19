"""Fast-light residual objective: improve timing without relearning basic automation intent.

The working Home Assistant automation is the behavioural baseline.  In Shadow, the
champion policy is allowed to learn only from counterfactual timing decisions that are
later verified by the baseline transition (or by an unambiguous failure).  This makes the
fast-light objective explicit:

* ON earlier when a future ON is actually confirmed;
* OFF earlier after vacancy when a future OFF is actually confirmed;
* strongly penalize false early ON, premature OFF and quick OFF->ON retriggers.

Sensor Tournament challengers use the same paired timing utility instead of plain
same-instant balanced accuracy.  Balanced accuracy is still retained as a safety check,
not the ranking objective.  No challenger creates an ActionIntent or calls Executor/HA.

Weight-only Shadow timing updates intentionally keep ``model_revision`` unchanged.  The
revision is structural identity for Tournament epochs; changing it for every online
weight update would continuously reset future-only challenger evidence.
"""
import json
import math
import threading
import time

from context import action_values, is_fast_reactive_agent, target_value
from settings import OPTIONS
import context_tournament_metrics as metrics_module
import context_tournament_promotion as promotion_module


TIMING_METRIC_MODE = "fast_timing"
TIMING_EPOCH_VERSION = 1
MAX_ACCURACY_DROP = 0.03
MAX_CHALLENGER_FAILURE_RATE = 0.10
RETRIGGER_SECONDS = 15.0


def _finite(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _binary_index(actions, value):
    if not actions or value is None:
        return None
    return min(range(len(actions)), key=lambda i: abs(float(actions[i]) - float(value)))


def _direction_for_idx(actions, idx):
    if idx is None or not actions:
        return None
    return "on" if float(actions[int(idx)]) >= 0.5 else "off"


def _window_for(direction):
    if direction == "on":
        return max(1.0, float(OPTIONS.get("fast_precursor_on_seconds", 8)))
    return max(5.0, float(OPTIONS.get("fast_precursor_off_seconds", 120)))


def _state_bool(state):
    if not state:
        return None
    text = str(state.get("state") or "").strip().lower()
    if text in ("on", "home", "open", "occupied", "detected", "active", "true", "1"):
        return True
    if text in ("off", "not_home", "closed", "unoccupied", "clear", "inactive", "false", "0"):
        return False
    return None


def _primary_occupancy(policy, states):
    meta = dict(getattr(policy, "selection_meta", {}) or {})
    entity_id = (
        meta.get("primary_occupancy_sensor")
        or meta.get("primary_local_sensor")
        or next(iter(meta.get("primary_local_sensors") or []), None)
    )
    return entity_id, _state_bool((states or {}).get(entity_id)) if entity_id else None


def _off_is_safe_to_try(policy, states, runtime):
    _, occupied = _primary_occupancy(policy, states)
    if occupied is not None:
        return not occupied
    forecast = dict((runtime or {}).get("context_meta", {}).get("home_forecast") or {})
    return bool(forecast.get("known")) and _finite(forecast.get("occupancy_now"), 1.0) < 0.5


def _automation_baseline_active(runtime, policy=None):
    if any(bool(row.get("enabled")) for row in ((runtime or {}).get("automation_priors") or [])):
        return True
    meta = dict(getattr(policy, "selection_meta", {}) or {}) if policy is not None else {}
    return bool(meta.get("automation_baseline_current"))


def timing_utility(delay, window):
    """Paired Tournament utility in [-1, 1]; successful larger safe lead is better."""
    return max(0.0, min(1.0, _finite(delay) / max(0.25, _finite(window, 1.0))))


def _blank_benchmark():
    return {
        "version": TIMING_EPOCH_VERSION,
        "on_samples": 0,
        "on_success": 0,
        "on_lead_seconds_sum": 0.0,
        "false_early_on": 0,
        "off_samples": 0,
        "off_success": 0,
        "off_saved_seconds_sum": 0.0,
        "premature_off": 0,
        "false_early_off": 0,
        "retriggers": 0,
        "reward_sum": 0.0,
        "last_event_ts": None,
    }


def benchmark_summary(raw, pending=None):
    row = {**_blank_benchmark(), **dict(raw or {})}
    on_samples = int(row.get("on_samples") or 0)
    off_samples = int(row.get("off_samples") or 0)
    on_success = int(row.get("on_success") or 0)
    off_success = int(row.get("off_success") or 0)
    total = on_samples + off_samples
    failures = (
        int(row.get("false_early_on") or 0)
        + int(row.get("premature_off") or 0)
        + int(row.get("false_early_off") or 0)
        + int(row.get("retriggers") or 0)
    )
    return {
        "mode": "automation_residual_timing",
        "samples": total,
        "on_samples": on_samples,
        "off_samples": off_samples,
        "on_success": on_success,
        "off_success": off_success,
        "mean_on_lead_seconds": (
            float(row.get("on_lead_seconds_sum") or 0.0) / on_success if on_success else None
        ),
        "mean_off_saved_seconds": (
            float(row.get("off_saved_seconds_sum") or 0.0) / off_success if off_success else None
        ),
        "false_early_on": int(row.get("false_early_on") or 0),
        "premature_off": int(row.get("premature_off") or 0),
        "false_early_off": int(row.get("false_early_off") or 0),
        "retriggers": int(row.get("retriggers") or 0),
        "failure_rate": (failures / total) if total else None,
        "mean_reward": float(row.get("reward_sum") or 0.0) / total if total else None,
        "pending": dict(pending or {}),
        "last_event_ts": row.get("last_event_ts"),
    }


def timing_metric_row_factory(base_metric_row):
    def timing_metric_row(model, actions):
        if str((model or {}).get("metric_mode") or "") != TIMING_METRIC_MODE:
            return base_metric_row(model, actions)
        samples = int((model or {}).get("timing_samples") or 0)
        if samples <= 0:
            return {
                "metric": "fast_timing_utility",
                "baseline_score": None,
                "challenger_score": None,
                "gain": None,
                "samples": 0,
                "baseline_loss": None,
                "challenger_loss": None,
                "active_mean_utility": None,
                "challenger_mean_utility": None,
                "safety_ok": False,
            }
        active_sum = float((model or {}).get("timing_active_utility_sum") or 0.0)
        shadow_sum = float((model or {}).get("timing_shadow_utility_sum") or 0.0)
        active_mean = max(-1.0, min(1.0, active_sum / samples))
        shadow_mean = max(-1.0, min(1.0, shadow_sum / samples))
        baseline_score = (active_mean + 1.0) / 2.0
        challenger_score = (shadow_mean + 1.0) / 2.0
        gain = challenger_score - baseline_score

        # Same-instant balanced accuracy remains a guardrail.  It is deliberately not
        # the objective because a correct early transition necessarily disagrees with
        # the still-unchanged automation for a short period.
        safety = base_metric_row(model, actions)
        accuracy_gain = safety.get("gain") if safety.get("metric") == "balanced_accuracy" else None
        shadow_failures = int((model or {}).get("timing_shadow_failures") or 0)
        active_failures = int((model or {}).get("timing_active_failures") or 0)
        shadow_failure_rate = shadow_failures / samples
        active_failure_rate = active_failures / samples
        safety_ok = (
            (accuracy_gain is None or float(accuracy_gain) >= -MAX_ACCURACY_DROP)
            and shadow_failure_rate <= MAX_CHALLENGER_FAILURE_RATE
        )
        return {
            "metric": "fast_timing_utility",
            "baseline_score": baseline_score,
            "challenger_score": challenger_score,
            "gain": gain,
            "samples": samples,
            "baseline_loss": 1.0 - baseline_score,
            "challenger_loss": 1.0 - challenger_score,
            "active_mean_utility": active_mean,
            "challenger_mean_utility": shadow_mean,
            "active_failures": active_failures,
            "challenger_failures": shadow_failures,
            "active_failure_rate": active_failure_rate,
            "challenger_failure_rate": shadow_failure_rate,
            "balanced_accuracy_gain": accuracy_gain,
            "safety_ok": safety_ok,
        }
    return timing_metric_row


def _patch_promotion_for_timing(timing_metric_row):
    """Teach the existing promotion windows to segment timing evidence."""
    if getattr(promotion_module, "_fast_timing_windows_patched", False):
        return

    promotion_module.PROMOTION_EPOCH_VERSION = max(
        2, int(getattr(promotion_module, "PROMOTION_EPOCH_VERSION", 1))
    )
    promotion_module.metric_row = timing_metric_row

    original_reset = promotion_module._reset_window
    original_snapshot = promotion_module._snapshot_seen
    original_window_model = promotion_module._window_model
    original_absorb = promotion_module.absorb_new_scored_evidence
    original_eligibility = promotion_module.promotion_eligibility

    def reset_window(model, action_count, start_ts, hours):
        original_reset(model, action_count, start_ts, hours)
        model["promotion_window_timing_samples"] = 0
        model["promotion_window_timing_active_utility_sum"] = 0.0
        model["promotion_window_timing_shadow_utility_sum"] = 0.0
        model["promotion_window_timing_active_failures"] = 0
        model["promotion_window_timing_shadow_failures"] = 0

    def snapshot_seen(model, action_count):
        original_snapshot(model, action_count)
        model["promotion_seen_timing_samples"] = int(model.get("timing_samples") or 0)
        model["promotion_seen_timing_active_utility_sum"] = float(
            model.get("timing_active_utility_sum") or 0.0
        )
        model["promotion_seen_timing_shadow_utility_sum"] = float(
            model.get("timing_shadow_utility_sum") or 0.0
        )
        model["promotion_seen_timing_active_failures"] = int(
            model.get("timing_active_failures") or 0
        )
        model["promotion_seen_timing_shadow_failures"] = int(
            model.get("timing_shadow_failures") or 0
        )

    def window_model(model, action_count):
        row = original_window_model(model, action_count)
        if str(model.get("metric_mode") or "") == TIMING_METRIC_MODE:
            row.update({
                "metric_mode": TIMING_METRIC_MODE,
                "timing_samples": int(model.get("promotion_window_timing_samples") or 0),
                "timing_active_utility_sum": float(
                    model.get("promotion_window_timing_active_utility_sum") or 0.0
                ),
                "timing_shadow_utility_sum": float(
                    model.get("promotion_window_timing_shadow_utility_sum") or 0.0
                ),
                "timing_active_failures": int(
                    model.get("promotion_window_timing_active_failures") or 0
                ),
                "timing_shadow_failures": int(
                    model.get("promotion_window_timing_shadow_failures") or 0
                ),
                # Window `samples` must follow the objective sample count so empty
                # timing windows cannot inherit unrelated state-transition samples.
                "samples": int(model.get("promotion_window_timing_samples") or 0),
            })
        return row

    def absorb(model, actions, now, config=None):
        if str(model.get("metric_mode") or "") != TIMING_METRIC_MODE:
            return original_absorb(model, actions, now, config)
        cfg = config or promotion_module.tournament_config()
        promotion_module.ensure_promotion_epoch(model, len(actions), now, cfg)
        current = int(model.get("timing_samples") or 0)
        seen = int(model.get("promotion_seen_timing_samples") or 0)
        delta = max(0, current - seen)
        scored_ts = float(model.get("timing_last_scored_ts") or now)
        if delta <= 0:
            promotion_module.advance_windows(model, actions, now, cfg)
            return 0

        promotion_module.advance_windows(model, actions, scored_ts, cfg)
        model["promotion_window_timing_samples"] = int(
            model.get("promotion_window_timing_samples") or 0
        ) + delta
        model["promotion_window_samples"] = int(
            model.get("promotion_window_samples") or 0
        ) + delta
        for total_key, seen_key, window_key in (
            ("timing_active_utility_sum", "promotion_seen_timing_active_utility_sum",
             "promotion_window_timing_active_utility_sum"),
            ("timing_shadow_utility_sum", "promotion_seen_timing_shadow_utility_sum",
             "promotion_window_timing_shadow_utility_sum"),
        ):
            inc = float(model.get(total_key) or 0.0) - float(model.get(seen_key) or 0.0)
            model[window_key] = float(model.get(window_key) or 0.0) + inc
        for total_key, seen_key, window_key in (
            ("timing_active_failures", "promotion_seen_timing_active_failures",
             "promotion_window_timing_active_failures"),
            ("timing_shadow_failures", "promotion_seen_timing_shadow_failures",
             "promotion_window_timing_shadow_failures"),
        ):
            inc = max(0, int(model.get(total_key) or 0) - int(model.get(seen_key) or 0))
            model[window_key] = int(model.get(window_key) or 0) + inc
        snapshot_seen(model, len(actions))
        promotion_module.advance_windows(model, actions, now, cfg)
        return delta

    def eligibility(model, actions, now, last_promotion_ts=None, options=None):
        row = original_eligibility(model, actions, now, last_promotion_ts, options)
        if str(model.get("metric_mode") or "") == TIMING_METRIC_MODE:
            metrics = timing_metric_row(model, actions)
            checks = dict(row.get("checks") or {})
            checks["timing_safety"] = bool(metrics.get("safety_ok"))
            row["checks"] = checks
            row["ready"] = all(checks.values())
            row["metric"] = metrics.get("metric")
            row["balanced_accuracy_gain"] = metrics.get("balanced_accuracy_gain")
            row["challenger_failure_rate"] = metrics.get("challenger_failure_rate")
        return row

    promotion_module._reset_window = reset_window
    promotion_module._snapshot_seen = snapshot_seen
    promotion_module._window_model = window_model
    promotion_module.absorb_new_scored_evidence = absorb
    promotion_module.promotion_eligibility = eligibility
    promotion_module._fast_timing_windows_patched = True


def install(service):
    """Install fast-light Shadow objective and timing-aware Tournament scoring."""
    if getattr(service, "_fast_light_objective_installed", False):
        return service

    engine = service.engine
    store = service.store
    lock = threading.RLock()
    persistence_lock = threading.RLock()
    persistence_event = threading.Event()
    active_runtime = {}
    pair_runtime = {}
    benchmark_cache = {}
    dirty_benchmarks = {}
    dirty_models = {}
    persistence_stats = {"queued": 0, "flushed_metrics": 0, "flushed_models": 0, "flushes": 0, "errors": 0}

    with store.lock, store.conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS fast_light_timing_metrics (
                   agent_id TEXT PRIMARY KEY,
                   metrics_json TEXT NOT NULL DEFAULT '{}',
                   updated_ts REAL NOT NULL
               )"""
        )

    def load_benchmark(agent_id):
        aid = str(agent_id)
        with lock:
            cached = benchmark_cache.get(aid)
            if cached is not None:
                return dict(cached)
        with store.conn() as c:
            row = c.execute(
                "SELECT metrics_json FROM fast_light_timing_metrics WHERE agent_id=?",
                (aid,),
            ).fetchone()
        if not row:
            metrics = _blank_benchmark()
        else:
            try:
                raw = json.loads(row["metrics_json"] or "{}")
            except Exception:
                raw = {}
            metrics = {**_blank_benchmark(), **(raw if isinstance(raw, dict) else {})}
        with lock:
            benchmark_cache[aid] = dict(metrics)
        return dict(metrics)

    def queue_benchmark(agent_id, metrics):
        aid = str(agent_id)
        snapshot = dict(metrics)
        with lock:
            benchmark_cache[aid] = snapshot
        with persistence_lock:
            dirty_benchmarks[aid] = snapshot
            persistence_stats["queued"] += 1
            wake = len(dirty_benchmarks) + len(dirty_models) >= 32
        if wake:
            persistence_event.set()

    def queue_model_checkpoint(agent, policy):
        aid = str(agent["id"])
        with persistence_lock:
            dirty_models[aid] = id(policy)
            persistence_stats["queued"] += 1
            wake = len(dirty_benchmarks) + len(dirty_models) >= 32
        if wake:
            persistence_event.set()

    def flush_persistence():
        with persistence_lock:
            metrics_batch = dict(dirty_benchmarks)
            model_batch = dict(dirty_models)
            dirty_benchmarks.clear()
            dirty_models.clear()
        if not metrics_batch and not model_batch:
            return 0
        try:
            if metrics_batch:
                now = time.time()
                packed = [
                    (aid, json.dumps(metrics, separators=(",", ":"), sort_keys=True), now)
                    for aid, metrics in metrics_batch.items()
                ]
                with store.lock, store.conn() as c:
                    c.executemany(
                        """INSERT INTO fast_light_timing_metrics(agent_id,metrics_json,updated_ts)
                           VALUES(?,?,?)
                           ON CONFLICT(agent_id) DO UPDATE SET
                             metrics_json=excluded.metrics_json,updated_ts=excluded.updated_ts""",
                        packed,
                    )
            model_rows = []
            for aid, expected_identity in model_batch.items():
                policy = engine.models.get(aid)
                if policy is not None and id(policy) == expected_identity:
                    model_rows.append((aid, policy.serialize()))
            if model_rows:
                store.save_models_batch(model_rows)
            with persistence_lock:
                persistence_stats["flushed_metrics"] += len(metrics_batch)
                persistence_stats["flushed_models"] += len(model_rows)
                persistence_stats["flushes"] += 1
            return len(metrics_batch) + len(model_rows)
        except Exception:
            with persistence_lock:
                for aid, metrics in metrics_batch.items():
                    dirty_benchmarks.setdefault(aid, metrics)
                for aid, identity in model_batch.items():
                    dirty_models.setdefault(aid, identity)
                persistence_stats["errors"] += 1
            raise

    def persistence_writer():
        while not engine.stop_event.is_set():
            persistence_event.wait(5.0)
            persistence_event.clear()
            try:
                flush_persistence()
            except Exception as exc:
                try:
                    store.event(None, "warning", "fast_light_persistence_failed",
                                f"Deferred fast-light persistence failed: {type(exc).__name__}: {exc}", None)
                except Exception:
                    pass
                time.sleep(0.1)
        try:
            flush_persistence()
        except Exception:
            pass

    def persistence_snapshot():
        with persistence_lock:
            return {
                **persistence_stats,
                "pending_metrics": len(dirty_benchmarks),
                "pending_models": len(dirty_models),
            }

    def benchmark_for(agent_id):
        aid = str(agent_id)
        with lock:
            state = active_runtime.get(aid) or {}
            cached = state.get("metrics")
            pending = state.get("pending")
        metrics = dict(cached) if cached is not None else load_benchmark(aid)
        display_pending = {}
        if pending:
            display_pending = {
                "direction": pending.get("direction"),
                "age_seconds": max(0.0, time.time() - float(pending.get("started_ts") or time.time())),
                "window_seconds": pending.get("window"),
            }
        return benchmark_summary(metrics, display_pending)

    def persist_active_metrics(aid, state):
        state["metrics"]["last_event_ts"] = time.time()
        queue_benchmark(aid, state["metrics"])

    def learn_weight_only(agent, candidate, reward):
        if candidate is None or not candidate.get("features"):
            return
        policy = engine.models.get(agent["id"])
        if policy is None:
            return
        horizon = int(candidate.get("horizon") or min(policy.horizons))
        if horizon not in policy.heads:
            horizon = min(policy.horizons)
        action_idx = int(candidate.get("action_idx") or 0)
        # Do not call MultiHorizonPolicy.update(): this is a weight-only online update,
        # not a structural model change. Keeping model_revision stable prevents Sensor
        # Tournament future-only epochs from being reset after every timing sample.
        policy.heads[horizon].update(action_idx, candidate["features"], reward)
        queue_model_checkpoint(agent, policy)

    def reward_candidate(agent, state, candidate, *, success=False, false_timing=False,
                         premature_off=False, retrigger=False, resolved_ts=None):
        if not candidate:
            return None
        direction = str(candidate.get("direction") or "")
        now = float(resolved_ts if resolved_ts is not None else time.time())
        delay = max(0.0, now - float(candidate.get("started_ts") or now)) if success else None
        result = engine.executor.reward_engine.evaluate(
            timing_direction=direction,
            baseline_delay=delay,
            timing_window=float(candidate.get("window") or _window_for(direction)),
            false_timing=bool(false_timing),
            premature_off=bool(premature_off),
            retrigger=bool(retrigger),
        )
        metrics = state["metrics"]
        if direction == "on":
            metrics["on_samples"] = int(metrics.get("on_samples") or 0) + 1
            if success:
                metrics["on_success"] = int(metrics.get("on_success") or 0) + 1
                metrics["on_lead_seconds_sum"] = float(
                    metrics.get("on_lead_seconds_sum") or 0.0
                ) + float(delay or 0.0)
            elif false_timing:
                metrics["false_early_on"] = int(metrics.get("false_early_on") or 0) + 1
        elif direction == "off":
            metrics["off_samples"] = int(metrics.get("off_samples") or 0) + 1
            if success:
                metrics["off_success"] = int(metrics.get("off_success") or 0) + 1
                metrics["off_saved_seconds_sum"] = float(
                    metrics.get("off_saved_seconds_sum") or 0.0
                ) + float(delay or 0.0)
            elif premature_off:
                metrics["premature_off"] = int(metrics.get("premature_off") or 0) + 1
            elif false_timing:
                metrics["false_early_off"] = int(metrics.get("false_early_off") or 0) + 1
            if retrigger:
                metrics["retriggers"] = int(metrics.get("retriggers") or 0) + 1
        metrics["reward_sum"] = float(metrics.get("reward_sum") or 0.0) + float(result.value)
        persist_active_metrics(agent["id"], state)
        learn_weight_only(agent, candidate, result.value)
        rt = engine.runtime.setdefault(agent["id"], {})
        rt["fast_light_objective"] = benchmark_summary(metrics)
        rt["last_reward_components"] = dict(result.components)
        rt["last_reward"] = result.value
        rt["last_reward_reason"] = (
            "fast-light baseline timing confirmed" if success else
            "fast-light premature OFF" if premature_off else
            "fast-light quick retrigger" if retrigger else
            "fast-light timing prediction not confirmed"
        )
        store.event(
            agent["id"], "info" if result.value >= 0 else "warning", "fast_light_timing_outcome",
            "Fast-light timing outcome",
            {
                "direction": direction,
                "reward": float(result.value),
                "baseline_delay_seconds": delay,
                "window_seconds": float(candidate.get("window") or 0.0),
                "success": bool(success),
                "false_timing": bool(false_timing),
                "premature_off": bool(premature_off),
                "retrigger": bool(retrigger),
            },
        )
        return result

    def active_objective(agent, states, now):
        if not is_fast_reactive_agent(agent) or agent.get("target_property") != "power":
            return
        if str(agent.get("mode") or "") != "shadow":
            return
        aid = str(agent["id"])
        rt = engine.runtime.get(aid) or {}
        policy = engine.models.get(aid)
        if policy is None or not _automation_baseline_active(rt, policy):
            return
        actions = [float(x) for x in action_values(agent)]
        if len(actions) != 2:
            return
        current = target_value((states or {}).get(agent["target_entity"]), agent["target_property"])
        if current is None:
            return
        current_idx = _binary_index(actions, current)
        prediction = rt.get("last_prediction")
        predicted_idx = _binary_index(actions, prediction)
        if predicted_idx is None:
            return

        with lock:
            state = active_runtime.setdefault(aid, {
                "last_target_idx": current_idx,
                "pending": None,
                "last_off_success": None,
                "metrics": load_benchmark(aid),
            })
            previous_idx = state.get("last_target_idx")
            pending = state.get("pending")

        changed = previous_idx is not None and int(previous_idx) != int(current_idx)
        if pending:
            direction = pending.get("direction")
            elapsed = max(0.0, now - float(pending.get("started_ts") or now))
            _, occupied = _primary_occupancy(policy, states)
            if changed and int(pending.get("desired_idx")) == int(current_idx):
                reward_candidate(agent, state, pending, success=True, resolved_ts=now)
                if direction == "off":
                    state["last_off_success"] = {**pending, "baseline_off_ts": now}
                state["pending"] = None
                pending = None
            elif direction == "off" and occupied is True:
                reward_candidate(agent, state, pending, premature_off=True, resolved_ts=now)
                state["pending"] = None
                pending = None
            elif elapsed > float(pending.get("window") or _window_for(direction)):
                reward_candidate(agent, state, pending, false_timing=True, resolved_ts=now)
                state["pending"] = None
                pending = None

        # A baseline OFF followed by a quick ON means the earlier counterfactual OFF was
        # too aggressive.  Apply a second negative update to the exact earlier context.
        last_off = state.get("last_off_success")
        if changed and current_idx == 1 and last_off:
            age = now - float(last_off.get("baseline_off_ts") or 0.0)
            if 0.0 <= age <= RETRIGGER_SECONDS:
                reward_candidate(agent, state, last_off, retrigger=True, resolved_ts=now)
            state["last_off_success"] = None
        elif last_off and now - float(last_off.get("baseline_off_ts") or 0.0) > RETRIGGER_SECONDS:
            state["last_off_success"] = None

        if state.get("pending") is None and int(predicted_idx) != int(current_idx):
            direction = _direction_for_idx(actions, predicted_idx)
            if direction != "off" or _off_is_safe_to_try(policy, states, rt):
                try:
                    features, _, _ = policy.features(states, engine.temporal_history, at_ts=now)
                except Exception:
                    features = {}
                horizon = int(rt.get("prediction_horizon") or min(policy.horizons))
                state["pending"] = {
                    "direction": direction,
                    "desired_idx": int(predicted_idx),
                    "action_idx": int(predicted_idx),
                    "started_ts": float(now),
                    "window": _window_for(direction),
                    "horizon": horizon,
                    "features": dict(features or {}),
                }

        state["last_target_idx"] = int(current_idx)
        rt["fast_light_objective"] = benchmark_summary(state["metrics"], state.get("pending"))

    def ensure_timing_fields(model):
        model["metric_mode"] = TIMING_METRIC_MODE
        model.setdefault("timing_samples", 0)
        model.setdefault("timing_active_utility_sum", 0.0)
        model.setdefault("timing_shadow_utility_sum", 0.0)
        model.setdefault("timing_active_failures", 0)
        model.setdefault("timing_shadow_failures", 0)
        model.setdefault("timing_last_scored_ts", None)
        return model

    def record_pair(model, active_utility, shadow_utility, now, *, active_failure=False,
                    shadow_failure=False, direction=None):
        ensure_timing_fields(model)
        model["timing_samples"] = int(model.get("timing_samples") or 0) + 1
        model["timing_active_utility_sum"] = float(
            model.get("timing_active_utility_sum") or 0.0
        ) + float(active_utility)
        model["timing_shadow_utility_sum"] = float(
            model.get("timing_shadow_utility_sum") or 0.0
        ) + float(shadow_utility)
        model["timing_active_failures"] = int(model.get("timing_active_failures") or 0) + int(active_failure)
        model["timing_shadow_failures"] = int(model.get("timing_shadow_failures") or 0) + int(shadow_failure)
        model["timing_last_scored_ts"] = float(now)
        if direction:
            model[f"timing_{direction}_samples"] = int(model.get(f"timing_{direction}_samples") or 0) + 1

    def prospective(agent, actions, current_idx, desired_idx, policy, states, runtime, now):
        if desired_idx is None or int(desired_idx) == int(current_idx):
            return None
        direction = _direction_for_idx(actions, desired_idx)
        if direction == "off" and not _off_is_safe_to_try(policy, states, runtime):
            return None
        return {
            "desired_idx": int(desired_idx),
            "direction": direction,
            "started_ts": float(now),
            "window": _window_for(direction),
        }

    def update_pair_before_prediction(agent, states, now):
        if not is_fast_reactive_agent(agent) or agent.get("target_property") != "power":
            return
        actions = [float(x) for x in action_values(agent)]
        if len(actions) != 2:
            return
        aid = str(agent["id"])
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        current_idx = _binary_index(actions, current)
        if current_idx is None:
            return
        policy = engine.models.get(aid)
        if policy is None:
            return
        _, occupied = _primary_occupancy(policy, states)
        tournament = service.state(aid)
        with lock:
            agent_state = pair_runtime.setdefault(aid, {"last_target_idx": current_idx, "challengers": {}})
            previous_idx = agent_state.get("last_target_idx")
        changed = previous_idx is not None and int(previous_idx) != int(current_idx)

        for challenger in list(tournament.get("challenger_features") or []):
            model = service._load_shadow_model(aid, challenger, len(actions))
            ensure_timing_fields(model)
            with lock:
                state = agent_state["challengers"].setdefault(
                    challenger, {"active_pending": None, "shadow_pending": None}
                )
            active_pending = state.get("active_pending")
            shadow_pending = state.get("shadow_pending")
            scored = False

            if changed:
                def success_utility(pending):
                    if pending and int(pending.get("desired_idx")) == int(current_idx):
                        delay = max(0.0, now - float(pending.get("started_ts") or now))
                        return timing_utility(delay, pending.get("window"))
                    return 0.0
                active_u = success_utility(active_pending)
                shadow_u = success_utility(shadow_pending)
                record_pair(model, active_u, shadow_u, now,
                            direction=_direction_for_idx(actions, current_idx))
                state["active_pending"] = None
                state["shadow_pending"] = None
                scored = True
            else:
                active_failure = False
                shadow_failure = False
                for name in ("active_pending", "shadow_pending"):
                    pending = state.get(name)
                    if not pending:
                        continue
                    elapsed = max(0.0, now - float(pending.get("started_ts") or now))
                    failed = elapsed > float(pending.get("window") or 0.0)
                    if pending.get("direction") == "off" and occupied is True:
                        failed = True
                    if failed:
                        if name == "active_pending":
                            active_failure = True
                        else:
                            shadow_failure = True
                        state[name] = None
                if active_failure or shadow_failure:
                    record_pair(
                        model,
                        -1.0 if active_failure else 0.0,
                        -1.0 if shadow_failure else 0.0,
                        now,
                        active_failure=active_failure,
                        shadow_failure=shadow_failure,
                    )
                    scored = True
            if scored:
                service._save_shadow_model(aid, challenger, model)
        with lock:
            agent_state["last_target_idx"] = int(current_idx)

    original_observe = service.observe_shadow
    original_status = service.shadow_status
    original_runtime_for = engine.runtime_for

    base_metric_row = metrics_module.metric_row
    timing_metric_row = timing_metric_row_factory(base_metric_row)
    metrics_module.metric_row = timing_metric_row
    _patch_promotion_for_timing(timing_metric_row)

    def observe_with_fast_timing(agent, state_map=None, changed_entities=None):
        states = dict(state_map or getattr(engine, "state_map", {}) or {})
        now = time.time()
        update_pair_before_prediction(agent, states, now)
        result = original_observe(agent, states, changed_entities)

        if is_fast_reactive_agent(agent) and agent.get("target_property") == "power":
            actions = [float(x) for x in action_values(agent)]
            current = target_value(states.get(agent["target_entity"]), agent["target_property"])
            current_idx = _binary_index(actions, current)
            policy = engine.models.get(agent["id"])
            if len(actions) == 2 and current_idx is not None and policy is not None:
                aid = str(agent["id"])
                runtime = engine.runtime.get(aid) or {}
                active_idx = _binary_index(actions, runtime.get("last_prediction"))
                with service.lock:
                    predictions = dict((service._shadow_runtime.get(aid) or {}).get("predictions") or {})
                tournament = service.state(aid)
                with lock:
                    agent_state = pair_runtime.setdefault(aid, {"last_target_idx": current_idx, "challengers": {}})
                for challenger in list(tournament.get("challenger_features") or []):
                    pred = predictions.get(challenger) or {}
                    shadow_idx = pred.get("shadow_index")
                    with lock:
                        state = agent_state["challengers"].setdefault(
                            challenger, {"active_pending": None, "shadow_pending": None}
                        )
                        for name, desired_idx in (("active_pending", active_idx), ("shadow_pending", shadow_idx)):
                            pending = state.get(name)
                            if pending and desired_idx is not None and int(pending.get("desired_idx")) != int(desired_idx):
                                state[name] = None
                                pending = None
                            if state.get(name) is None:
                                state[name] = prospective(
                                    agent, actions, current_idx, desired_idx, policy, states, runtime, now
                                )
                    model = service._load_shadow_model(aid, challenger, len(actions))
                    if str(model.get("metric_mode") or "") != TIMING_METRIC_MODE:
                        ensure_timing_fields(model)
                        service._save_shadow_model(aid, challenger, model)

        active_objective(agent, states, now)
        return result

    def status_with_fast_timing(agent):
        payload = original_status(agent)
        actions = [float(x) for x in action_values(agent)]
        if is_fast_reactive_agent(agent) and agent.get("target_property") == "power" and len(actions) == 2:
            for row in payload.get("challengers") or []:
                model = service._load_shadow_model(agent["id"], row.get("entity_id"), len(actions))
                ensure_timing_fields(model)
                timing = timing_metric_row(model, actions)
                row.update(timing)
                row["timing_objective"] = True
            payload["binary_metric"] = "fast_timing_utility"
            payload["safety_metric"] = "balanced_accuracy"
            payload["score_definition"] = (
                "gain = normalized safe timing utility(active+sensor) - normalized safe timing utility(active)"
            )
        return payload

    def runtime_with_fast_timing(agent):
        payload = original_runtime_for(agent)
        if is_fast_reactive_agent(agent) and agent.get("target_property") == "power":
            payload["fast_light_objective"] = benchmark_for(agent["id"])
        return payload

    threading.Thread(
        target=persistence_writer,
        name="adaptive-ai-fast-light-writer",
        daemon=True,
    ).start()
    service.fast_light_persistence_snapshot = persistence_snapshot
    service.observe_shadow = observe_with_fast_timing
    service.shadow_status = status_with_fast_timing
    engine.runtime_for = runtime_with_fast_timing
    service.fast_light_benchmark = lambda agent: benchmark_for(agent["id"])
    service._fast_light_objective_installed = True
    return service
