"""Runtime-facing Stage-13 confidence semantics and probability calibration service.

This module is deliberately diagnostic-only.  It wraps the public runtime/status payloads
without changing policy choice, ActionIntent construction or Executor dispatch.  It also
provides the one supported entry point for real probabilistic calibration labels: callers
must supply a stable episode id and an independent source.
"""
from __future__ import annotations

import time

from confidence_contract import METRIC_SEMANTICS, contract_descriptor


class ConfidenceCalibrationService:
    """Persist/report independent probability labels through the Stage-13 journal."""

    def __init__(self, engine, journal):
        self.engine = engine
        self.journal = journal

    def record_presence(self, *, area_id, horizon_seconds, episode_id, prediction,
                        observed, source_kind, ts=None, dependency_cluster=None,
                        model_key=None):
        if not area_id:
            raise ValueError("presence calibration requires area_id")
        if not episode_id:
            raise ValueError("presence calibration requires a stable episode_id")
        horizon = int(horizon_seconds)
        if horizon not in (0, 1, 3, 5):
            raise ValueError("presence calibration horizon must be one of 0,1,3,5 seconds")
        home = getattr(getattr(self.engine, "context", None), "home", None)
        version = getattr(home, "VERSION", "unknown")
        key = str(model_key or f"room_belief:v{version}")
        metric = "presence_now" if horizon == 0 else f"presence_{horizon}s"
        return self.journal.record(
            metric_id=metric,
            model_key=key,
            scope_id=str(area_id),
            episode_id=str(episode_id),
            ts=float(time.time() if ts is None else ts),
            prediction=prediction,
            observed=observed,
            source_kind=source_kind,
            dependency_cluster=dependency_cluster,
            independent=True,
        )

    def presence_report(self, *, area_id, horizon_seconds, model_key=None):
        horizon = int(horizon_seconds)
        home = getattr(getattr(self.engine, "context", None), "home", None)
        version = getattr(home, "VERSION", "unknown")
        key = str(model_key or f"room_belief:v{version}")
        metric = "presence_now" if horizon == 0 else f"presence_{horizon}s"
        return self.journal.report(metric, key, str(area_id))


def _live_semantics(payload):
    out = dict(payload or {})
    forecast = dict(out.get("home_forecast") or (out.get("context_meta") or {}).get("home_forecast") or {})
    out["decision_strength"] = out.get("last_confidence")
    out["decision_strength_semantics"] = METRIC_SEMANTICS["decision_strength"]
    out["expected_action_utility"] = out.get("last_expected_reward")
    out["expected_action_utility_semantics"] = METRIC_SEMANTICS["expected_action_utility"]
    out["data_coverage"] = out.get("historical_support")
    out["data_coverage_semantics"] = METRIC_SEMANTICS["data_coverage"]
    out["presence_probability"] = forecast.get("occupancy_in_3s")
    out["presence_probability_horizon_seconds"] = 3
    out["presence_probability_semantics"] = METRIC_SEMANTICS["presence_probability"]
    out["forecast_uncertainty"] = forecast.get("uncertainty")
    out["forecast_uncertainty_semantics"] = METRIC_SEMANTICS["forecast_uncertainty"]
    out["policy_validation_diagnostic"] = {
        "accuracy": out.get("validation_accuracy"),
        "lower_bound": out.get("validation_lower_bound"),
        "samples": out.get("validation_samples"),
        "semantics": "legacy_held_out_diagnostic_not_stage13_final_evaluation",
    }
    out["confidence_contract"] = contract_descriptor()
    return out


def install_runtime_semantics(engine, probability_journal):
    """Expose the same Stage-13 vocabulary from live API/status as Candidate UI/gates."""
    if getattr(engine, "_confidence_runtime_installed", False):
        return engine

    original_runtime_for = engine.runtime_for
    original_status = engine.status

    def runtime_for(agent):
        return _live_semantics(original_runtime_for(agent))

    def status():
        out = dict(original_status() or {})
        out["average_decision_strength"] = out.get("average_confidence")
        out["average_confidence_semantics"] = METRIC_SEMANTICS["decision_strength"]
        out["confidence_contract"] = contract_descriptor()
        return out

    engine.runtime_for = runtime_for
    engine.status = status
    engine.confidence_calibration = ConfidenceCalibrationService(engine, probability_journal)
    engine._confidence_runtime_installed = True
    return engine
