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

APP_VERSION = "0.14.30"
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
    "agent_training_chunk_hours": 6,
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
    "history_background_pause_ms": 1500,
    "history_background_start_delay_seconds": 60,
    "background_cpu_duty_cycle": 0.20,
    "process_nice": 10,
    "training_cpu_duty_cycle": 0.20,
    "training_archive_batch_rows": 16,
    "training_experience_batch_rows": 64,
    "training_throttle_max_sleep_seconds": 2.0,
    "training_max_continuous_work_ms": 50,
    "manual_agent_training": True,
    "max_concurrent_training_jobs": 1,
    "manual_discovery_hours": 24,
    "teach_rl_feature_min_labels": 12,
    "teach_rl_feature_min_per_binary_class": 5,
    "teach_rl_feature_min_observation_days": 2,
    "teach_rl_feature_score": 0.60,
    "teach_rl_feature_evidence_samples": 24,
    "teach_rl_positive_weight": 6,
    "teach_rl_negative_weight": 3,
    "agent_candidate_future_samples": 40,
    "agent_candidate_future_samples_per_binary_action": 20,
    "agent_candidate_max_accuracy_regression": 0.03,
    "agent_candidate_backup_hours": 24,
}

SUPPORTED_TARGETS = {
    "light": [
        {"property": "power", "label": "Power", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
        {"property": "brightness_pct", "label": "Brightness (%)", "min": 0, "max": 100, "deadband": 3, "exploration_step": 5},
    ],
    "switch": [
        {"property": "power", "label": "Power", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
    ],
    "input_boolean": [
        {"property": "power", "label": "Power", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
    ],
    "climate": [
        {"property": "temperature", "label": "Target temperature", "min": 5, "max": 35, "deadband": 0.3, "exploration_step": 0.2},
    ],
    "cover": [
        {"property": "position", "label": "Position (%)", "min": 0, "max": 100, "deadband": 3, "exploration_step": 5},
    ],
    "fan": [
        {"property": "power", "label": "Power", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
        {"property": "percentage", "label": "Speed (%)", "min": 0, "max": 100, "deadband": 5, "exploration_step": 5},
    ],
    "number": [
        {"property": "value", "label": "Value", "min": 0, "max": 100, "deadband": 0.1, "exploration_step": 5},
    ],
    "input_number": [
        {"property": "value", "label": "Value", "min": 0, "max": 100, "deadband": 0.1, "exploration_step": 5},
    ],
    "media_player": [
        {"property": "power", "label": "Power", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
        {"property": "volume_pct", "label": "Volume (%)", "min": 0, "max": 100, "deadband": 3, "exploration_step": 3},
    ],
    "humidifier": [
        {"property": "humidity", "label": "Target humidity (%)", "min": 30, "max": 80, "deadband": 2, "exploration_step": 2},
    ],
    "water_heater": [
        {"property": "temperature", "label": "Target temperature", "min": 30, "max": 80, "deadband": 0.5, "exploration_step": 0.5},
    ],
    "select": [
        {"property": "option_index", "label": "Selected option", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
    ],
    "input_select": [
        {"property": "option_index", "label": "Selected option", "min": 0, "max": 1, "deadband": 0.5, "exploration_step": 1},
    ],
}

# Domain -> sensor capabilities that usually reduce policy uncertainty.
SENSOR_NEEDS = {
    "light": [
        ("illuminance", "Illuminance", "Lets the agent distinguish dark rooms from daylight without guessing from time."),
        ("occupancy", "Occupancy / presence", "Prevents learning lighting preferences when nobody is using the room."),
        ("activity", "Radar / activity score", "Lets fast lighting follow ESPHome radar scores and AI activity detectors when those signals historically drive the lamp."),
        ("sun", "Sun position", "Adds sunrise, sunset and solar elevation context."),
    ],
    "switch": [
        ("occupancy", "Occupancy / presence", "Helps separate intentional use from background device state."),
        ("activity", "Radar / activity score", "Allows short-series activity scores to reproduce fast occupancy-driven switching."),
    ],
    "input_boolean": [
        ("occupancy", "Occupancy / presence", "Helps connect mode choices with whether people are actually present."),
    ],
    "climate": [
        ("temperature", "Indoor temperature", "Required to understand the thermal state instead of learning only setpoints."),
        ("outdoor_temperature", "Outdoor temperature", "Explains changing heating/cooling demand."),
        ("occupancy", "Occupancy / presence", "Lets comfort policy differ between occupied and empty periods."),
        ("humidity", "Humidity", "Adds comfort and latent-load context."),
        ("window", "Window / door contact", "Avoids learning from periods with open windows or doors."),
    ],
    "cover": [
        ("illuminance", "Illuminance", "Helps connect blind position with glare and daylight."),
        ("sun", "Sun position", "Solar elevation/azimuth is highly informative for shades."),
        ("temperature", "Indoor temperature", "Helps learn solar-gain trade-offs."),
        ("occupancy", "Occupancy / presence", "Prevents optimizing an unused room as if it were occupied."),
        ("window", "Window contact", "Useful for safety and context when the opening is in use."),
    ],
    "fan": [
        ("co2", "CO₂", "A strong demand signal for ventilation."),
        ("humidity", "Humidity", "Important for bathrooms and moisture-driven ventilation."),
        ("occupancy", "Occupancy / presence", "Separates occupied air-quality demand from background ventilation."),
        ("temperature", "Temperature", "Adds thermal-comfort context."),
        ("voc", "VOC / air quality", "Improves ventilation decisions when CO₂ is not the only pollutant."),
    ],
    "media_player": [
        ("occupancy", "Occupancy / presence", "Helps avoid learning media preferences when the room is empty."),
        ("ambient_noise", "Ambient noise", "Can explain preferred listening volume."),
    ],
    "humidifier": [
        ("humidity", "Humidity", "Provides the actual room humidity that should drive the target."),
        ("occupancy", "Occupancy / presence", "Separates comfort preferences from empty-room operation."),
    ],
    "water_heater": [
        ("temperature", "Water temperature", "Provides the thermal state rather than only the target."),
    ],
    "select": [],
    "input_select": [],
}

NUMERIC_ATTRS = {
    "brightness", "current_temperature", "temperature", "humidity", "current_position",
    "percentage", "volume_level", "battery_level", "power", "energy", "pressure",
    "illuminance", "co2", "pm25", "pm10", "volatile_organic_compounds", "signal_strength",
}

def now_ts():
    return time.time()

def iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None

def iso_from_ts(ts):
    return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def sigmoid(x):
    if x >= 0:
        z = math.exp(-min(x, 60))
        return 1 / (1 + z)
    z = math.exp(max(x, -60))
    return z / (1 + z)

def load_options():
    options = dict(DEFAULT_OPTIONS)
    try:
        if OPTIONS_PATH.exists():
            data = json.loads(OPTIONS_PATH.read_text())
            for key in options:
                if key in data:
                    options[key] = data[key]
            # v0.4 migrations: preserve explicit custom values, but move the old shipped
            # defaults to the new event-driven/discovery defaults on upgrade.
            if data.get("poll_seconds") == 2:
                options["poll_seconds"] = 30
            if data.get("auto_agent_min_changes") == 4:
                options["auto_agent_min_changes"] = 2
            if data.get("auto_agent_recent_days") == 7:
                options["auto_agent_recent_days"] = 10
            if data.get("feature_dimensions") == 192:
                options["feature_dimensions"] = 128
            # v0.7.3: migrate the old shipped realtime debounce so existing installs
            # actually receive the local-first fast-light latency improvement.
            if data.get("realtime_inference_debounce_ms") == 75:
                options["realtime_inference_debounce_ms"] = 25
            # 0.14.15: migrate only the previously shipped defaults. Explicit custom
            # values remain untouched; the new defaults make long historical replay
            # suitable for Raspberry Pi class hosts.
            if data.get("agent_training_chunk_hours") == 24:
                options["agent_training_chunk_hours"] = 6
            if data.get("history_background_pause_ms") == 500:
                options["history_background_pause_ms"] = 1500
            # 0.14.16: the old 10 s startup delay still overlapped first Ingress and
            # HA realtime requests on Raspberry Pi. Preserve explicit custom values.
            if data.get("history_background_start_delay_seconds") == 10:
                options["history_background_start_delay_seconds"] = 60
            # 0.14.27: migrate only defaults shipped by earlier releases.
            # Explicit custom budgets stay untouched.
            if data.get("training_cpu_duty_cycle") in (0.55, 0.25):
                options["training_cpu_duty_cycle"] = 0.20
            if data.get("training_max_continuous_work_ms") == 75:
                options["training_max_continuous_work_ms"] = 50
    except Exception as exc:
        print(f"[options] Failed to read options: {exc}", flush=True)
    # 0.9 never starts heavy replay implicitly, including installations with the
    # legacy automatic option. One shared job covers agent AND global bootstrap.
    options["manual_agent_training"] = True
    options["max_concurrent_training_jobs"] = 1
    return options

OPTIONS = load_options()
