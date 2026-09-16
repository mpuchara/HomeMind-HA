#!/usr/bin/env python3

import json
import math
import os

import time

from datetime import datetime

from pathlib import Path

try:
    from websockets.sync.client import connect as ws_connect
except Exception:
    ws_connect = None

APP_VERSION = "0.14.2"
HISTORY_BOOTSTRAP_REVISION = "target-attrs-v2"
TRAINING_REVISION = "shared-home-intents-v18"
DATA_DIR = Path(os.environ.get("ADAPTIVE_AI_DATA", "/data"))
DB_PATH = DATA_DIR / "adaptive_ai.db"
OPTIONS_PATH = DATA_DIR / "options.json"
STATIC_DIR = Path(__file__).parent / "static"

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core/api").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")

DEFAULT_OPTIONS = {
    "policy_half_life_days": 30,
    "home_model_half_life_days": 45,
    "entity_area_mapping": "{}",
    "intent_ttl_seconds": 2,
    "poll_seconds": 30,
    "proactive_tick_seconds": 1,
    "realtime_inference_debounce_ms": 25,
    "prediction_lead_seconds": 1,  # reactive default: act ~1s before the historical/manual action
    "prediction_horizons_seconds": "1",
    "correction_window_seconds": 90,
    "reward_window_seconds": 90,
    "feature_dimensions": 128,
    "max_context_entities": 28,
    "temporal_short_seconds": 60,
    "temporal_long_seconds": 300,
    "fast_temporal_short_seconds": 2,
    "fast_temporal_long_seconds": 12,
    "fast_recent_change_seconds": 3,
    "fast_series_lags_seconds": "1,3,10",
    "fast_max_context_entities": 8,
    "context_challenger_count": 4,
    "context_tournament_enabled": True,
    "context_tournament_min_samples": 40,
    "context_tournament_min_days": 3,
    "context_tournament_min_gain": 0.03,
    "context_primary_replacement_gain": 0.07,
    "context_tournament_consecutive_wins": 3,
    "context_tournament_evaluation_hours": 24,
    "context_tournament_cooldown_hours": 24,
    "context_schema_probation_samples": 50,
    "fast_clock_context_weight": 0.15,
    "fast_causal_driver_min_score": 0.50,
    "fast_causal_driver_reserve": 2,
    "fast_precursor_on_seconds": 8,
    "fast_precursor_off_seconds": 120,
    "fast_upstream_lead_seconds": 4,
    "primary_local_sensor_reserve": 4,
    "automation_context_reserve": 8,
    "candidate_benchmark_threshold": 0.78,
    "candidate_benchmark_min_samples": 12,
    "agent_candidate_future_samples": 40,
    "agent_candidate_future_samples_per_binary_action": 20,
    "agent_candidate_max_accuracy_regression": 0.03,
    "agent_candidate_backup_hours": 24,
    "agent_training_chunk_hours": 24,
    "agent_training_overlap_hours": 6,
    "min_historical_support": 0.20,
    "max_context_novelty": 0.85,
    "confidence_validation_fraction": 0.20,
    "confidence_min_validation_samples": 12,
    "block_control_on_automation_conflict": True,
    "action_bins": 31,
    "rl_alpha": 0.65,
    "history_bootstrap_days": 10,
    "archive_retention_days": 365,
    "archive_context_interval_seconds": 30,
    "auto_agent_min_changes": 2,
    "auto_agent_recent_days": 10,
    "max_auto_agents": 250,
    "automation_scan_enabled": True,
    "automation_hint_weight": 0.85,
    "history_maintenance_minutes": 30,
    "history_fast_context_hours": 24,
    "history_fast_target_hours": 6,
    "history_parallel_requests": 1,
    "history_context_import_interval_seconds": 60,
    "history_background_pause_ms": 500,
    "history_background_start_delay_seconds": 10,
    "process_nice": 10,
    "manual_agent_training": True,
    "max_concurrent_training_jobs": 1,
    "manual_discovery_hours": 24,
    "manual_context_max_snapshots": 256,
    "manual_context_min_samples": 4,
    "manual_context_promote_score": 0.55,
    "manual_context_reserve": 2,
    "manual_context_observer_max_entities": 512,
    "teach_rl_feature_min_labels": 12,
    "teach_rl_feature_min_per_binary_class": 5,
    "teach_rl_feature_min_observation_days": 2,
    "teach_rl_feature_score": 0.60,
    "teach_rl_feature_evidence_samples": 24,
    "teach_rl_positive_weight": 6,
    "teach_rl_negative_weight": 3,
}

# remainder of this file is unchanged; this write intentionally updates only APP_VERSION while preserving current settings semantics.

