#!/usr/bin/env python3
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
import gc
from control import timing_for, legal_value, same_value
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode

try:
    from websockets.sync.client import connect as ws_connect
except Exception:
    ws_connect = None

APP_VERSION = "0.8.0"
HISTORY_BOOTSTRAP_REVISION = "target-attrs-v2"
TRAINING_REVISION = "esphome-sensor-context-v17"
DATA_DIR = Path(os.environ.get("ADAPTIVE_AI_DATA", "/data"))
DB_PATH = DATA_DIR / "adaptive_ai.db"
OPTIONS_PATH = DATA_DIR / "options.json"
STATIC_DIR = Path(__file__).parent / "static"

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core/api").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")

DEFAULT_OPTIONS = {
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
    except Exception as exc:
        print(f"[options] Failed to read options: {exc}", flush=True)
    return options


OPTIONS = load_options()


class Store:
    def __init__(self, path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self._init()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-20000")
        try:
            with c:
                yield c
        finally:
            c.close()

    def _init(self):
        with self.lock, self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'paused',
                    input_entities TEXT NOT NULL DEFAULT '["*"]',
                    target_entity TEXT NOT NULL,
                    target_property TEXT NOT NULL,
                    min_value REAL NOT NULL,
                    max_value REAL NOT NULL,
                    confidence_threshold REAL NOT NULL DEFAULT 0.75,
                    deadband REAL NOT NULL DEFAULT 1.0,
                    action_interval REAL NOT NULL DEFAULT 30.0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rl_models (
                    agent_id TEXT PRIMARY KEY,
                    model_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rl_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    action_index INTEGER NOT NULL,
                    action_value REAL NOT NULL,
                    reward REAL NOT NULL,
                    reason TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    user_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_agent ON rl_feedback(agent_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    agent_id TEXT,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_id ON events(id DESC);
                CREATE TABLE IF NOT EXISTS entity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    state TEXT,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    context_user_id TEXT,
                    source TEXT NOT NULL,
                    UNIQUE(entity_id, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_entity_history_ts ON entity_history(ts);
                CREATE INDEX IF NOT EXISTS idx_entity_history_entity_ts ON entity_history(entity_id, ts);
                CREATE TABLE IF NOT EXISTS historical_experiences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    target_history_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    action_index INTEGER NOT NULL,
                    action_value REAL NOT NULL,
                    reward REAL NOT NULL,
                    dwell_seconds REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    user_id TEXT,
                    UNIQUE(agent_id, target_history_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hist_exp_agent ON historical_experiences(agent_id, id);
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._ensure_column(c, "agents", "exploration_step", "REAL NOT NULL DEFAULT 5.0")
            self._ensure_column(c, "agents", "auto_created", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(c, "agents", "micro_exploration", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(c, "agents", "exploration_interval", "REAL NOT NULL DEFAULT 21600")
            for field in ("ack_timeout", "settling_seconds", "manual_hold_seconds"):
                self._ensure_column(c, "agents", field, "REAL NOT NULL DEFAULT 0")
            self._ensure_column(c, "agents", "training_state", "TEXT NOT NULL DEFAULT 'candidate'")
            self._ensure_column(c, "agents", "benchmark_score", "REAL")
            self._ensure_column(c, "agents", "benchmark_samples", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(c, "agents", "benchmark_source", "TEXT")
            self._ensure_column(c, "agents", "benchmark_detail_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(c, "agents", "benchmark_updated_at", "TEXT")
            self._ensure_column(c, "agents", "training_cursor_ts", "REAL")
            self._ensure_column(c, "agents", "training_window_start_ts", "REAL")
            self._ensure_column(c, "agents", "training_window_end_ts", "REAL")
            self._ensure_column(c, "agents", "training_progress", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(c, "agents", "training_updated_at", "TEXT")
            self._ensure_column(c, "rl_feedback", "source", "TEXT NOT NULL DEFAULT 'live'")
            c.execute("UPDATE agents SET mode='shadow' WHERE mode='learn'")
            # v0.7.8 lifecycle migration: dormant becomes PAUSED, candidate becomes TRAINING.
            c.execute("UPDATE agents SET training_state='paused', mode='paused' WHERE training_state='dormant'")
            c.execute("UPDATE agents SET training_state='training', mode='paused' WHERE training_state='candidate'")
            # Completed v0.7.6 candidates already reached the end of the local archive.
            # Seed their cursor once so Resume can continue from there rather than rebuild.
            bounds = c.execute("SELECT MIN(ts), MAX(ts) FROM entity_history").fetchone()
            if bounds and bounds[1] is not None:
                c.execute("""UPDATE agents SET training_window_start_ts=COALESCE(training_window_start_ts, ?),
                           training_window_end_ts=COALESCE(training_window_end_ts, ?),
                           training_cursor_ts=COALESCE(training_cursor_ts, ?),
                           training_progress=CASE WHEN training_state IN ('qualified','paused') THEN 1.0 ELSE training_progress END,
                           training_updated_at=COALESCE(training_updated_at, ?)
                           WHERE training_state IN ('qualified','paused')""",
                          (bounds[0], bounds[1], bounds[1], iso_now()))

    @staticmethod
    def _ensure_column(c, table, column, ddl):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def list_agents(self):
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id) AS feedback_count,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id AND f.reward > 0) AS positive_count,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id AND f.reward < 0) AS negative_count,
                       (SELECT AVG(f.reward) FROM rl_feedback f WHERE f.agent_id=a.id) AS average_reward,
                       (SELECT COUNT(*) FROM historical_experiences h WHERE h.agent_id=a.id) AS historical_count,
                       (SELECT AVG(h.reward) FROM historical_experiences h WHERE h.agent_id=a.id) AS historical_average_reward
                FROM agents a ORDER BY a.created_at
                """
            ).fetchall()
        return [self._agent_dict(r) for r in rows]

    def get_agent(self, agent_id):
        with self.conn() as c:
            row = c.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id) AS feedback_count,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id AND f.reward > 0) AS positive_count,
                       (SELECT COUNT(*) FROM rl_feedback f WHERE f.agent_id=a.id AND f.reward < 0) AS negative_count,
                       (SELECT AVG(f.reward) FROM rl_feedback f WHERE f.agent_id=a.id) AS average_reward,
                       (SELECT COUNT(*) FROM historical_experiences h WHERE h.agent_id=a.id) AS historical_count,
                       (SELECT AVG(h.reward) FROM historical_experiences h WHERE h.agent_id=a.id) AS historical_average_reward
                FROM agents a WHERE a.id=?
                """,
                (agent_id,),
            ).fetchone()
        return self._agent_dict(row) if row else None

    @staticmethod
    def _agent_dict(row):
        d = dict(row)
        try:
            d["input_entities"] = json.loads(d.get("input_entities") or '["*"]')
        except Exception:
            d["input_entities"] = ["*"]
        d["enabled"] = bool(d["enabled"])
        d["auto_created"] = bool(d.get("auto_created"))
        d["micro_exploration"] = bool(d.get("micro_exploration"))
        d["average_reward"] = float(d["average_reward"]) if d.get("average_reward") is not None else None
        d["historical_average_reward"] = float(d["historical_average_reward"]) if d.get("historical_average_reward") is not None else None
        d["training_state"] = str(d.get("training_state") or "training")
        d["benchmark_score"] = float(d["benchmark_score"]) if d.get("benchmark_score") is not None else None
        d["benchmark_samples"] = int(d.get("benchmark_samples") or 0)
        for key in ("training_cursor_ts", "training_window_start_ts", "training_window_end_ts"):
            d[key] = float(d[key]) if d.get(key) is not None else None
        d["training_progress"] = clamp(float(d.get("training_progress") or 0.0), 0.0, 1.0)
        try:
            d["benchmark_detail"] = json.loads(d.get("benchmark_detail_json") or "{}")
        except Exception:
            d["benchmark_detail"] = {}
        return d

    def create_agent(self, payload):
        agent_id = str(uuid.uuid4())[:8]
        with self.lock, self.conn() as c:
            c.execute(
                """
                INSERT INTO agents
                (id,name,mode,input_entities,target_entity,target_property,min_value,max_value,
                 confidence_threshold,deadband,action_interval,exploration_step,auto_created,micro_exploration,
                 exploration_interval,enabled,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    agent_id,
                    payload["name"],
                    "paused",
                    json.dumps(payload.get("input_entities") or ["*"]),
                    payload["target_entity"],
                    payload["target_property"],
                    float(payload["min_value"]),
                    float(payload["max_value"]),
                    float(payload.get("confidence_threshold", 0.75)),
                    float(payload.get("deadband", 1.0)),
                    float(payload.get("action_interval", 30)),
                    float(payload.get("exploration_step", 5.0)),
                    1 if payload.get("auto_created") else 0,
                    1 if payload.get("micro_exploration") else 0,
                    float(payload.get("exploration_interval", 21600)),
                    1,
                    iso_now(),
                ),
            )
            c.execute("UPDATE agents SET training_state='paused', mode='paused', training_progress=0, training_updated_at=? WHERE id=?", (iso_now(), agent_id))
        timing = {k: payload[k] for k in ("ack_timeout", "settling_seconds", "manual_hold_seconds") if k in payload}
        if timing:
            self.update_agent(agent_id, timing)
        self.event(agent_id, "info", "agent_created", f"Created RL agent {payload['name']}", payload)
        return self.get_agent(agent_id)

    def update_agent(self, agent_id, payload):
        allowed = {
            "name": str, "mode": str, "min_value": float, "max_value": float,
            "confidence_threshold": float, "deadband": float, "action_interval": float,
            "exploration_step": float, "exploration_interval": float,
            "ack_timeout": float, "settling_seconds": float, "manual_hold_seconds": float,
            "micro_exploration": int, "enabled": int,
            "input_entities": json.dumps,
        }
        updates, values = [], []
        reset_model = any(k in payload for k in ("min_value", "max_value", "input_entities"))
        for key, caster in allowed.items():
            if key in payload:
                val = payload[key]
                if key in ("enabled", "micro_exploration"):
                    val = 1 if bool(val) else 0
                else:
                    val = caster(val)
                updates.append(f"{key}=?")
                values.append(val)
        if updates:
            values.append(agent_id)
            with self.lock, self.conn() as c:
                c.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id=?", values)
        if reset_model:
            self.clear_learning(agent_id)
        self.event(agent_id, "info", "agent_updated", "Agent settings updated", payload)
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id):
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM rl_feedback WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM historical_experiences WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM rl_models WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        self.event(agent_id, "info", "agent_deleted", "Agent deleted", None)

    def add_feedback(self, agent_id, action_index, action_value, reward, reason, features, user_id=None, source="live"):
        with self.lock, self.conn() as c:
            c.execute(
                """INSERT INTO rl_feedback(agent_id,created_at,action_index,action_value,reward,reason,features_json,user_id,source)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (agent_id, iso_now(), int(action_index), float(action_value), float(reward), reason,
                 json.dumps({str(k): v for k, v in features.items()}), user_id, source),
            )

    def list_feedback(self, agent_id, limit=100):
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM rl_feedback WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d.pop("features_json"))
            out.append(d)
        return out

    def get_model(self, agent_id):
        with self.conn() as c:
            row = c.execute("SELECT model_json FROM rl_models WHERE agent_id=?", (agent_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_model(self, agent_id, model):
        raw = json.dumps(model, separators=(",", ":"))
        with self.lock, self.conn() as c:
            c.execute(
                """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json, updated_at=excluded.updated_at""",
                (agent_id, raw, iso_now()),
            )

    def clear_learning(self, agent_id):
        """Full rebuild reset. Raw entity_history is intentionally retained."""
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM rl_feedback WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM historical_experiences WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM rl_models WHERE agent_id=?", (agent_id,))
            c.execute("""UPDATE agents SET training_state='training', mode='paused', benchmark_score=NULL, benchmark_samples=0,
                       benchmark_source=NULL, benchmark_detail_json='{}', benchmark_updated_at=NULL,
                       training_cursor_ts=NULL, training_window_start_ts=NULL, training_window_end_ts=NULL,
                       training_progress=0, training_updated_at=? WHERE id=?""", (iso_now(), agent_id))
        self.event(agent_id, "warning", "learning_reset",
                   "Full rebuild reset: policy/benchmark/cursor cleared; local raw history retained", None)

    def set_training_state(self, agent_id, state, score=None, samples=0, source=None, detail=None, demote_control=False):
        state = str(state or "training")
        if state not in ("training", "qualified", "paused"):
            raise ValueError("invalid training state")
        raw = json.dumps(detail or {}, separators=(",", ":"), ensure_ascii=False)
        # Training is offline-only. A finished pass becomes SHADOW only when it clears
        # the >78% benchmark; otherwise the whole agent is PAUSED to save CPU.
        mode = "shadow" if state == "qualified" else "paused"
        with self.lock, self.conn() as c:
            c.execute("""UPDATE agents SET training_state=?, benchmark_score=?, benchmark_samples=?, benchmark_source=?,
                       benchmark_detail_json=?, benchmark_updated_at=?, mode=?, training_updated_at=? WHERE id=?""",
                      (state, score, int(samples), source, raw, iso_now(), mode, iso_now(), agent_id))

    def set_training_progress(self, agent_id, start_ts, cursor_ts, end_ts):
        start_ts = float(start_ts); cursor_ts = float(cursor_ts); end_ts = float(end_ts)
        progress = 1.0 if end_ts <= start_ts else clamp((cursor_ts - start_ts) / (end_ts - start_ts), 0.0, 1.0)
        with self.lock, self.conn() as c:
            c.execute("""UPDATE agents SET training_window_start_ts=?, training_cursor_ts=?, training_window_end_ts=?,
                       training_progress=?, training_updated_at=? WHERE id=?""",
                      (start_ts, cursor_ts, end_ts, progress, iso_now(), agent_id))

    def set_partial_benchmark(self, agent_id, stat):
        agent = self.get_agent(agent_id)
        if not agent:
            return
        per_action = stat.get("per_action") or {}
        accs = [float(v.get("correct") or 0) / max(1, int(v.get("samples") or 0))
                for v in per_action.values() if int(v.get("samples") or 0) > 0]
        score = (sum(accs) / len(accs)) if accs else (float(stat.get("correct") or 0) / max(1, int(stat.get("samples") or 0)))
        detail = dict(agent.get("benchmark_detail") or {})
        detail["counts"] = {
            "samples": int(stat.get("samples") or 0), "correct": int(stat.get("correct") or 0),
            "per_action": per_action, "automation_rules": int(stat.get("automation_rules") or 0),
            "origin_counts": {str(k): int(v or 0) for k, v in (stat.get("origin_counts") or {}).items()},
        }
        detail["origin_counts"] = dict(detail["counts"]["origin_counts"])
        raw = json.dumps(detail, separators=(",", ":"), ensure_ascii=False)
        with self.lock, self.conn() as c:
            c.execute("""UPDATE agents SET benchmark_score=?, benchmark_samples=?, benchmark_source='recorded-behaviour',
                       benchmark_detail_json=?, benchmark_updated_at=?, training_updated_at=? WHERE id=?""",
                      (score, int(stat.get("samples") or 0), raw, iso_now(), iso_now(), agent_id))

    def training_agent_ids(self, unstarted_only=False):
        with self.conn() as c:
            sql = "SELECT id FROM agents WHERE enabled=1 AND training_state='training'"
            if unstarted_only:
                sql += " AND training_cursor_ts IS NULL"
            sql += " ORDER BY created_at"
            return [r[0] for r in c.execute(sql).fetchall()]

    def candidate_agent_ids(self):
        # Compatibility name used by discovery: only brand-new, not resumable jobs.
        return self.training_agent_ids(unstarted_only=True)

    def pause_stale_training_agents(self):
        with self.lock, self.conn() as c:
            cur = c.execute("UPDATE agents SET training_state='paused', mode='paused', training_updated_at=? WHERE training_state='training'", (iso_now(),))
            return int(cur.rowcount or 0)

    def qualified_agents(self):
        return [a for a in self.list_agents() if a.get("enabled") and a.get("training_state") == "qualified"]

    def clear_historical_models(self):
        """Rebuild offline policies without deleting live feedback or the long-term archive."""
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM historical_experiences")
            c.execute("DELETE FROM rl_models")
            c.execute("""UPDATE agents SET training_state='training', mode='paused', benchmark_score=NULL, benchmark_samples=0,
                       benchmark_source=NULL, benchmark_detail_json='{}', benchmark_updated_at=NULL,
                       training_cursor_ts=NULL, training_window_start_ts=NULL, training_window_end_ts=NULL,
                       training_progress=0, training_updated_at=?""", (iso_now(),))
        self.meta_set("candidate_qualification_complete", "0")
        self.event(None, "info", "training_revision", "Rebuilding offline RL policies for the new predictive feature revision", None)

    def event(self, agent_id, level, kind, message, data=None):
        try:
            with self.lock, self.conn() as c:
                c.execute(
                    "INSERT INTO events(created_at,agent_id,level,kind,message,data_json) VALUES(?,?,?,?,?,?)",
                    (iso_now(), agent_id, level, kind, message, json.dumps(data) if data is not None else None),
                )
                c.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 5000)")
        except Exception as exc:
            print(f"[event] {exc}", flush=True)

    def list_events(self, limit=100):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            raw_data = d.pop("data_json", None)
            d["data"] = json.loads(raw_data) if raw_data else None
            out.append(d)
        return out


    def find_agent_by_target(self, entity_id, property_name):
        with self.lock, self.conn() as c:
            row = c.execute("SELECT id FROM agents WHERE target_entity=? AND target_property=?", (entity_id, property_name)).fetchone()
        return self.get_agent(row[0]) if row else None

    def archive_upsert(self, entity_id, ts, state, attributes=None, user_id=None, source="live"):
        attrs = json.dumps(attributes or {}, separators=(",", ":"), ensure_ascii=False)
        with self.lock, self.conn() as c:
            c.execute(
                """INSERT INTO entity_history(entity_id,ts,state,attributes_json,context_user_id,source)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(entity_id,ts) DO UPDATE SET
                     state=excluded.state,
                     attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE entity_history.attributes_json END,
                     context_user_id=COALESCE(excluded.context_user_id, entity_history.context_user_id),
                     source=CASE WHEN excluded.source='ha_history_full' THEN excluded.source ELSE entity_history.source END""",
                (entity_id, float(ts), None if state is None else str(state), attrs, user_id, source),
            )

    def archive_batch(self, rows):
        if not rows:
            return 0
        packed = [
            (entity_id, float(ts), None if state is None else str(state),
             json.dumps(attrs or {}, separators=(",", ":"), ensure_ascii=False), user_id, source)
            for entity_id, ts, state, attrs, user_id, source in rows
        ]
        with self.lock, self.conn() as c:
            c.executemany(
                """INSERT INTO entity_history(entity_id,ts,state,attributes_json,context_user_id,source)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(entity_id,ts) DO UPDATE SET
                     state=excluded.state,
                     attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE entity_history.attributes_json END,
                     context_user_id=COALESCE(excluded.context_user_id, entity_history.context_user_id),
                     source=CASE WHEN excluded.source='ha_history_full' THEN excluded.source ELSE entity_history.source END""",
                packed,
            )
        return len(packed)

    def archive_rows(self, start_ts=None, end_ts=None, entity_id=None):
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        if entity_id:
            where.append("entity_id=?"); vals.append(entity_id)
        sql = "SELECT * FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY ts,id"
        with self.conn() as c:
            rows = c.execute(sql, vals).fetchall()
        return [dict(r) for r in rows]

    def archive_count(self, start_ts=None, end_ts=None, entity_ids=None):
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        sql = "SELECT COUNT(*) FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "")
        with self.conn() as c:
            return int(c.execute(sql, vals).fetchone()[0] or 0)

    def archive_iter(self, start_ts=None, end_ts=None, entity_ids=None, chunk_size=2000):
        """Stream history rows in bounded batches instead of materializing the archive."""
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        sql = "SELECT * FROM entity_history" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY ts,id"
        with self.conn() as c:
            cursor = c.execute(sql, vals)
            while True:
                batch = cursor.fetchmany(max(100, int(chunk_size)))
                if not batch:
                    break
                for row in batch:
                    yield dict(row)

    def archive_rows_for_entities(self, start_ts, end_ts, entity_ids):
        return list(self.archive_iter(start_ts=start_ts, end_ts=end_ts, entity_ids=entity_ids, chunk_size=2000))

    def archive_stats(self):
        with self.conn() as c:
            r = c.execute("SELECT COUNT(*) n, MIN(ts) min_ts, MAX(ts) max_ts, COUNT(DISTINCT entity_id) entities FROM entity_history").fetchone()
            by_source = {x[0]: x[1] for x in c.execute("SELECT source,COUNT(*) FROM entity_history GROUP BY source").fetchall()}
        d = dict(r)
        d["days"] = ((d["max_ts"] - d["min_ts"]) / 86400.0) if d.get("min_ts") and d.get("max_ts") else 0.0
        d["by_source"] = by_source
        return d

    def purge_archive(self, keep_days):
        if float(keep_days) <= 0:
            return
        cutoff = now_ts() - float(keep_days) * 86400.0
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM entity_history WHERE ts<?", (cutoff,))

    def add_historical_experience(self, agent_id, target_history_id, action_index, action_value, reward, dwell_seconds, features, user_id=None):
        raw = json.dumps({str(k): v for k, v in features.items()}, separators=(",", ":"))
        with self.lock, self.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO historical_experiences
                   (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (agent_id, int(target_history_id), iso_now(), int(action_index), float(action_value), float(reward),
                 float(dwell_seconds), raw, user_id),
            )
            return cur.rowcount > 0

    def list_historical_experiences(self, agent_id):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM historical_experiences WHERE agent_id=? ORDER BY id", (agent_id,)).fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["features"]={int(k):v for k,v in json.loads(d.pop("features_json")).items()}; out.append(d)
        return out

    def meta_get(self, key, default=None):
        with self.conn() as c:
            r=c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key, value):
        with self.lock, self.conn() as c:
            c.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


STORE = Store(DB_PATH)


class HAClient:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.token = token
        self.last_ok = None
        self.last_error = "Not connected yet"

    def request(self, method, path, payload=None, timeout=10):
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                self.last_ok = now_ts()
                self.last_error = None
                return json.loads(raw.decode("utf-8")) if raw else None
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self.last_error = str(exc)
            raise

    def states(self):
        return self.request("GET", "states")

    def service(self, domain, service, data):
        return self.request("POST", f"services/{domain}/{service}", data)

    def history(self, entity_ids, start_dt, end_dt, minimal=True, no_attributes=True, significant=True, timeout=20):
        params = {"filter_entity_id": ",".join(entity_ids), "end_time": end_dt}
        if minimal:
            params["minimal_response"] = ""
        if no_attributes:
            params["no_attributes"] = ""
        if significant:
            params["significant_changes_only"] = ""
        query = urlencode(params, doseq=True)
        # HA treats the presence of flag params as true; urlencode gives = which is accepted.
        return self.request("GET", f"history/period/{quote(start_dt, safe='')}?{query}", timeout=timeout)

    def automation_config(self, automation_id, timeout=8):
        return self.request("GET", f"config/automation/config/{quote(str(automation_id), safe='')}", timeout=timeout)


HA = HAClient(HA_BASE_URL, HA_TOKEN)


ENTITY_ID_RE = re.compile(r"\b[a-z_][a-z0-9_]*\.[a-zA-Z0-9_]+\b")
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{20,64}$", re.I)

def _extract_entities(obj):
    out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("entity_id", "entity_ids"):
                vals = value if isinstance(value, list) else [value]
                for item in vals:
                    if isinstance(item, str):
                        out.update(ENTITY_ID_RE.findall(item))
            # Service/action names such as light.turn_on match the entity-id shape but
            # are not entities. Their target/data blocks are still recursively parsed.
            if key not in ("action", "service"):
                out.update(_extract_entities(value))
    elif isinstance(obj, list):
        for value in obj:
            out.update(_extract_entities(value))
    elif isinstance(obj, str):
        out.update(ENTITY_ID_RE.findall(obj))
    return out

def _extract_device_ids(obj):
    out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("device_id", "device_ids"):
                vals = value if isinstance(value, list) else [value]
                for item in vals:
                    if isinstance(item, str) and DEVICE_ID_RE.match(item):
                        out.add(item)
            out.update(_extract_device_ids(value))
    elif isinstance(obj, list):
        for value in obj:
            out.update(_extract_device_ids(value))
    return out


def automation_action_targets(actions, registry):
    """Only literal command destinations, never condition/template references."""
    found = set()
    if isinstance(actions, list):
        for item in actions:
            found.update(automation_action_targets(item, registry))
    elif isinstance(actions, dict):
        command = actions.get("action", actions.get("service"))
        device_action = actions.get("device_id") and actions.get("domain") and actions.get("type")
        if command or device_action:
            destinations = [actions.get("target") or {}, actions.get("data") or {}]
            if device_action:
                destinations.append(actions)
            for destination in destinations:
                if not isinstance(destination, dict):
                    continue
                for key in ("entity_id", "device_id", "area_id"):
                    values = destination.get(key, [])
                    for value in values if isinstance(values, list) else [values]:
                        if not isinstance(value, str) or "{{" in value or "{%" in value:
                            continue
                        if key == "entity_id":
                            found.update(x.strip() for x in value.split(',') if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x.strip()))
                        else:
                            found.update(eid for eid, reg in registry.items() if reg.get(key) == value)
        for key in ("sequence", "default", "then", "else", "parallel"):
            found.update(automation_action_targets(actions.get(key), registry))
        for choice in actions.get("choose", []) or []:
            found.update(automation_action_targets(choice.get("sequence"), registry))
        repeat = actions.get("repeat")
        if isinstance(repeat, dict):
            found.update(automation_action_targets(repeat.get("sequence"), registry))
    return found


class AutomationKnowledge:
    """Best-effort read-only analysis of existing HA automations.

    Automations never become labels or rewards. They only provide a structural prior:
    entities used in triggers/conditions of an automation that acts on a target get
    duplicated as hint features so offline RL can converge with fewer samples.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.by_target = {}
        self.automations = []
        self.last_scan = None
        self.error = None

    def status(self):
        with self.lock:
            return {
                "automation_count": len(self.automations),
                "target_count": len(self.by_target),
                "last_scan": self.last_scan,
                "error": self.error,
            }

    def hints_for_target(self, entity_id):
        with self.lock:
            infos = list(self.by_target.get(entity_id, []))
        entities = set()
        for info in infos:
            entities.update(info.get("context_entities") or [])
        return entities, infos

    def scan(self, state_map, entity_registry=None, force=False):
        if not force and not OPTIONS.get("automation_scan_enabled", True):
            return 0
        entity_registry = entity_registry or {}
        device_entities = {}
        for eid, reg in entity_registry.items():
            did = reg.get("device_id") if isinstance(reg, dict) else None
            if did:
                device_entities.setdefault(did, set()).add(eid)
        autos = [s for eid, s in state_map.items() if eid.startswith("automation.")]
        by_target = {}
        parsed = []
        failures = 0
        configs = {}
        ids = [(st, (st.get("attributes") or {}).get("id")) for st in autos]
        ids_to_fetch = [(st, aid) for st, aid in ids if aid]
        def fetch_one(pair):
            st, aid = pair
            try:
                return st.get("entity_id"), HA.automation_config(aid, timeout=2), None
            except Exception as exc:
                return st.get("entity_id"), None, exc
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ids_to_fetch)))) as pool:
            for eid, cfg, err in pool.map(fetch_one, ids_to_fetch):
                if isinstance(cfg, dict):
                    configs[eid] = cfg
                elif err is not None:
                    failures += 1
        for st in autos:
            attrs = st.get("attributes") or {}
            automation_id = attrs.get("id")
            config = configs.get(st.get("entity_id")) or {}
            actions = config.get("actions", config.get("action", []))
            triggers = config.get("triggers", config.get("trigger", []))
            conditions = config.get("conditions", config.get("condition", []))
            action_entities = automation_action_targets(actions, entity_registry)
            context_entities = _extract_entities(triggers) | _extract_entities(conditions)
            context_devices = _extract_device_ids(triggers) | _extract_device_ids(conditions)
            for did in context_devices:
                context_entities.update(device_entities.get(did, ()))
            # Do not let the controlled target itself become a prior input merely because
            # it appears in the action block. The ordinary state remains in the context.
            info = {
                "entity_id": st.get("entity_id"),
                "name": attrs.get("friendly_name") or st.get("entity_id"),
                "automation_id": automation_id,
                "enabled": str(st.get("state")).lower() == "on",
                "last_triggered": attrs.get("last_triggered"),
                "target_entities": sorted(action_entities),
                "context_entities": sorted(context_entities - action_entities),
            }
            parsed.append(info)
            for target in action_entities:
                by_target.setdefault(target, []).append(info)
        with self.lock:
            self.by_target = by_target
            self.automations = parsed
            self.last_scan = now_ts()
            self.error = None if not failures else f"{failures} automation config(s) unavailable; remaining automations were still scanned"
        STORE.event(None, "info", "automation_scan", f"Scanned {len(parsed)} Home Assistant automation(s) as RL feature priors", {"automations": len(parsed), "targets": len(by_target), "config_failures": failures})
        return len(parsed)


AUTOMATION_KNOWLEDGE = AutomationKnowledge()


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def parse_state_value(state):
    if state is None:
        return None
    s = state.get("state")
    if s in (None, "unknown", "unavailable", "none", "None", ""):
        return None
    low = str(s).lower()
    if low in ("on", "home", "open", "detected", "occupied", "active", "playing", "heat", "cool"):
        return 1.0
    if low in ("off", "not_home", "closed", "clear", "unoccupied", "idle", "paused"):
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return str(s)


def target_value(state, property_name):
    if not state or str(state.get("state")).lower() in ("unavailable", "unknown"):
        return None
    attrs = state.get("attributes") or {}
    domain = state.get("entity_id", "").split(".", 1)[0]
    try:
        if property_name == "power" and domain in ("light", "switch", "input_boolean", "fan", "media_player"):
            low = str(state.get("state")).lower()
            return 0.0 if low in ("off", "unavailable", "unknown", "idle") else 1.0
        if domain == "light" and property_name == "brightness_pct":
            if str(state.get("state")).lower() == "off":
                return 0.0
            b = attrs.get("brightness")
            if b is None:
                return 0.0 if str(state.get("state")).lower() == "off" else None
            return float(b) * 100.0 / 255.0
        if domain == "climate" and property_name == "temperature":
            v = attrs.get("temperature")
            return None if v is None else float(v)
        if domain == "cover" and property_name == "position":
            v = attrs.get("current_position")
            return None if v is None else float(v)
        if domain == "fan" and property_name == "percentage":
            v = attrs.get("percentage")
            return None if v is None else float(v)
        if domain in ("number", "input_number") and property_name == "value":
            return float(state.get("state"))
        if domain == "media_player" and property_name == "volume_pct":
            v = attrs.get("volume_level")
            return None if v is None else float(v) * 100.0
        if domain == "humidifier" and property_name == "humidity":
            v = attrs.get("humidity")
            return None if v is None else float(v)
        if domain == "water_heater" and property_name == "temperature":
            v = attrs.get("temperature")
            return None if v is None else float(v)
        if domain in ("select", "input_select") and property_name == "option_index":
            opts = list(attrs.get("options") or [])
            try:
                return float(opts.index(str(state.get("state"))))
            except (ValueError, TypeError):
                return None
        v = attrs.get(property_name)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def target_call(entity_id, property_name, value):
    domain = entity_id.split(".", 1)[0]
    if property_name == "power" and domain in ("light", "switch", "input_boolean", "fan", "media_player"):
        return domain, "turn_on" if value >= 0.5 else "turn_off", {"entity_id": entity_id}
    if domain == "light" and property_name == "brightness_pct":
        # Brightness is the complete light policy for auto-discovered dimmable lights.
        # Treat the 0% arm as a real OFF command rather than turn_on(brightness=0),
        # which is interpreted inconsistently by different light integrations.
        if float(value) <= 0.5:
            return domain, "turn_off", {"entity_id": entity_id}
        return domain, "turn_on", {"entity_id": entity_id, "brightness_pct": int(round(value))}
    if domain == "climate" and property_name == "temperature":
        return domain, "set_temperature", {"entity_id": entity_id, "temperature": round(value, 2)}
    if domain == "cover" and property_name == "position":
        return domain, "set_cover_position", {"entity_id": entity_id, "position": int(round(value))}
    if domain == "fan" and property_name == "percentage":
        return domain, "set_percentage", {"entity_id": entity_id, "percentage": int(round(value))}
    if domain in ("number", "input_number") and property_name == "value":
        return domain, "set_value", {"entity_id": entity_id, "value": value}
    if domain == "media_player" and property_name == "volume_pct":
        return domain, "volume_set", {"entity_id": entity_id, "volume_level": clamp(value / 100.0, 0, 1)}
    if domain == "humidifier" and property_name == "humidity":
        return domain, "set_humidity", {"entity_id": entity_id, "humidity": int(round(value))}
    if domain == "water_heater" and property_name == "temperature":
        return domain, "set_temperature", {"entity_id": entity_id, "temperature": round(value, 2)}
    if domain in ("select", "input_select") and property_name == "option_index":
        state = ENGINE.state_map.get(entity_id) if "ENGINE" in globals() else None
        options = list(((state or {}).get("attributes") or {}).get("options") or [])
        if not options:
            raise ValueError(f"No options available for {entity_id}")
        idx = int(clamp(round(value), 0, len(options) - 1))
        return domain, "select_option", {"entity_id": entity_id, "option": options[idx]}
    raise ValueError(f"Unsupported target {domain}.{property_name}")


def stable_hash(text):
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def numeric_scale(state, attr_name=None):
    attrs = state.get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    device_class = str(attrs.get("device_class") or "").lower()
    entity_id = state.get("entity_id", "").lower()
    token = " ".join((unit, device_class, entity_id, str(attr_name or "").lower()))
    if "temperature" in token or "°c" in token or "°f" in token:
        return 35.0
    if "%" in token or "humidity" in token or "battery" in token or "position" in token or "brightness" in token:
        return 100.0
    if "co2" in token or "ppm" in token:
        return 2000.0
    if "illuminance" in token or "lux" in token:
        return 1000.0
    if "power" in token or unit in ("w", "kw"):
        return 5000.0 if unit != "kw" else 5.0
    if "pressure" in token or "hpa" in token:
        return 1000.0
    if "energy" in token or "kwh" in token:
        return 100.0
    return 10.0



COMMON_STATE_VALUES = {
    "off": -1.0, "closed": -1.0, "not_home": -1.0, "clear": -1.0, "unoccupied": -1.0, "idle": -0.5, "standby": -0.5,
    "on": 1.0, "open": 1.0, "home": 1.0, "playing": 1.0, "detected": 1.0, "occupied": 1.0, "active": 1.0, "heat": 0.8, "cool": -0.8,
    "heating": 0.8, "cooling": -0.8, "dry": 0.4, "fan_only": 0.2, "auto": 0.1,
    "unavailable": 0.0, "unknown": 0.0,
}

CONTEXT_DOMAINS = {
    "sensor", "binary_sensor", "person", "device_tracker", "sun", "weather", "light", "switch",
    "climate", "cover", "fan", "media_player", "input_boolean", "input_number", "number", "select",
    "input_select", "humidifier", "water_heater", "vacuum", "alarm_control_panel", "lock",
}


def parse_horizons(agent=None):
    raw = str(OPTIONS.get("prediction_horizons_seconds", "1"))
    vals = []
    for part in re.split(r"[,; ]+", raw.strip()):
        if not part:
            continue
        try:
            v = int(float(part))
            if 1 <= v <= 3600:
                vals.append(v)
        except Exception:
            pass
    # v0.6 is intentionally reactive rather than long-horizon predictive.
    # The default uses one reactive head. The value 1 represents the product goal
    # (beat a typical manual action by about a second), not a t-1s extrapolation of
    # the world. A precursor event wakes inference immediately.
    vals = sorted(set(vals))[:4]
    return vals or [max(1, int(OPTIONS.get("prediction_lead_seconds", 1)))]


def state_scalar(state):
    """Convert one HA state into a bounded scalar without hashing entity identity.

    Discrete HA states are handled *before* numeric coercion. This matters for presence,
    motion and on/off entities: v0.5 accidentally converted ON/OFF to 1/0 and then scaled
    them as generic numbers, producing roughly +0.1/0.0. v0.6 gives categorical state
    transitions a strong centred representation such as ON=+1 and OFF=-1.
    """
    if state is None:
        return None
    text = str(state.get("state") or "").strip().lower()
    if text in COMMON_STATE_VALUES:
        return COMMON_STATE_VALUES[text]
    raw = parse_state_value(state)
    if raw is None:
        return None
    if is_number(raw):
        return math.tanh(float(raw) / max(numeric_scale(state), 1e-6))
    text = str(raw).strip().lower()
    # Stable category embedding in [-0.85, 0.85]. It is local to this entity's slot,
    # therefore collisions between different HA entities are impossible.
    h = stable_hash(text)
    return ((h % 2001) / 1000.0 - 1.0) * 0.85



ELECTRICAL_UNITS = {
    # Voltage
    "v", "mv", "kv", "µv", "uv",
    # Current
    "a", "ma", "ka", "µa", "ua",
    # Active / apparent / reactive power
    "w", "mw", "kw", "gw", "va", "mva", "kva", "mva",
    "var", "mvar", "kvar", "mvar", "varh", "kvarh",
    # Electrical energy / charge
    "wh", "mwh", "kwh", "gwh", "ah", "mah", "kah",
    # Grid/electrical quantities
    "hz", "khz", "mhz", "ohm", "kohm", "mohm", "ω", "kω", "mω",
}


def _normalized_unit(state):
    attrs = (state or {}).get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
    unit = unit.replace(" ", "").replace("μ", "µ").replace("Ω", "ω")
    return unit


def is_electrical_measurement_entity(entity_id, state):
    """Return True *only* when the entity reports an electrical engineering unit.

    v0.7.12 deliberately removes name-, domain-, device-class- and whole-device
    blacklists.  Home Assistant can expose useful behavioural context under almost any
    domain/name: phone sensors, cars, people, weather, template/virtual entities, camera
    scores and ESPHome radar channels are all valid candidates.  We exclude an entity
    only when its ``unit_of_measurement`` itself is unambiguously electrical (W, V, A,
    VA, var, Wh/kWh, Hz, ohm, etc.).

    This means a sensor named ``Still Energy`` with unit ``%`` remains eligible, as does
    a ``power`` device_class with no electrical unit.  Conversely ``sensor.foo`` with
    unit ``W`` is excluded regardless of its name or device class.
    """
    return _normalized_unit(state) in ELECTRICAL_UNITS


def electrical_context_exclusions(state_map, registry):
    """Entity-level electrical-unit exclusion only.

    Do not blacklist physical devices.  If a Shelly/ESPHome/phone exposes both an
    electrical measurement and useful non-electrical context, only the W/V/A/... entity
    is removed.  Every sibling without an electrical unit remains a learning candidate.
    """
    excluded = {
        eid for eid, st in (state_map or {}).items()
        if is_electrical_measurement_entity(eid, st)
    }
    return excluded, {
        "detected_electrical_entities": len(excluded),
        "detected_electrical_devices": 0,
        "excluded_electrical_context_entities": len(excluded),
    }


def is_context_candidate_entity(entity_id, state, excluded_entities=None):
    """Broad candidate gate: everything parseable is eligible unless explicitly blocked.

    Feature selection happens later from historical relevance.  Keeping this gate broad
    is intentional: an unusual phone/car/template/camera entity must be allowed to prove
    that it predicts the target rather than being rejected by a hand-written whitelist.
    """
    if entity_id in set(excluded_entities or ()):
        return False
    return parse_state_value(state) is not None


def parse_fast_series_lags():
    raw = str(OPTIONS.get("fast_series_lags_seconds", "1,3,10"))
    vals = []
    for part in re.split(r"[,; ]+", raw.strip()):
        if not part:
            continue
        try:
            v = float(part)
            if 0.25 <= v <= 60:
                vals.append(v)
        except Exception:
            pass
    vals = sorted(set(vals))[:3]
    while len(vals) < 3:
        vals.append((1.0, 3.0, 10.0)[len(vals)])
    return vals[:3]


def context_scalar(entity_id, state, agent=None):
    """Scalar used in an explicit entity slot.

    Directly controllable Home Assistant entities and their device siblings are never
    fed into an agent. Electrical telemetry is blocked only by explicit electrical units;
    all other parseable HA state is allowed to compete during historical feature selection.
    """
    if state is None:
        return None
    try:
        if target_options_for_state(state):
            return None
    except Exception:
        pass
    if is_electrical_measurement_entity(entity_id, state):
        return None
    return state_scalar(state)


def entity_capability_tags(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    dc = str(attrs.get("device_class") or "").lower()
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    name = str(attrs.get("friendly_name") or "").lower()
    text = f"{entity_id.lower()} {dc} {unit} {name}"
    domain = entity_id.split(".", 1)[0]
    caps = set()
    if (domain in ("person", "device_tracker") or dc in ("occupancy", "motion", "presence")
            or any(x in text for x in ("occupancy", "presence", "motion", "obecno"))):
        caps.add("occupancy")
    # ESPHome mmWave/camera helpers often expose the actual causal signal as a numeric
    # percentage or score rather than a binary_sensor. Examples from real installations:
    # "Still Energy" (%), "Move Energy" (%), and "AI detection" (points).
    activity_terms = (
        "ai detection", "aidetection", "detection score", "camera score",
        "still energy", "move energy", "moving energy", "radar energy",
        "still target", "moving target", "move target",
    )
    if any(x in text for x in activity_terms):
        caps.add("activity")
    if "illuminance" in text or "lux" in text or unit == "lx": caps.add("illuminance")
    if domain == "sun" or "sun elevation" in text or "solar elevation" in text: caps.add("sun")
    if "temperature" in text or "°c" in unit or "°f" in unit:
        caps.add("temperature")
        if any(x in text for x in ("outdoor", "outside", "external", "zewn")): caps.add("outdoor_temperature")
    if "humidity" in text: caps.add("humidity")
    if "co2" in text or "carbon dioxide" in text: caps.add("co2")
    if "voc" in text or "air quality" in text: caps.add("voc")
    if dc in ("window", "door", "opening") or any(x in text for x in ("window", "door", "okno", "drzwi")): caps.add("window")
    if "power" in text or unit in ("w", "kw"): caps.add("power")
    if "noise" in text or "sound" in text: caps.add("ambient_noise")
    return caps


def _name_tokens(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    text = f"{entity_id} {attrs.get('friendly_name') or ''}".lower()
    return {x for x in re.split(r"[^a-z0-9ąćęłńóśźż]+", text) if len(x) >= 3}


def is_fast_reactive_agent(agent):
    """Fast binary targets should follow local sensor edges, not minute-scale context."""
    domain = str(agent.get("target_entity") or "").split(".", 1)[0]
    return agent.get("target_property") == "power" and domain in ("light", "switch", "input_boolean")


def _context_locality(agent, entity_id, state, registry, target_state=None):
    target = agent["target_entity"]
    target_state = target_state or {}
    target_reg = registry.get(target) or {}
    reg = registry.get(entity_id) or {}
    overlap = _name_tokens(target, target_state) & _name_tokens(entity_id, state)
    same_device = bool(target_reg.get("device_id") and reg.get("device_id") == target_reg.get("device_id"))
    same_area = bool(target_reg.get("area_id") and reg.get("area_id") == target_reg.get("area_id"))
    semantic = bool(overlap)
    caps = entity_capability_tags(entity_id, state)
    occupancy = "occupancy" in caps
    activity = "activity" in caps
    local = same_device or same_area or semantic
    return {
        "same_device": same_device, "same_area": same_area, "semantic": semantic,
        "occupancy": occupancy, "activity": activity, "local": local, "overlap": sorted(overlap),
    }



def occupancy_state_bool(state):
    """Return a robust boolean for an occupancy/presence entity.

    Home Assistant binary_sensors normally expose on/off, while some ESPHome/template
    sensors can expose detected/clear, occupied/unoccupied, or numeric 0/1.  Driver
    discovery must treat all of those representations identically.
    """
    if state is None:
        return None
    text = str(state.get("state") or "").strip().lower()
    if text in ("on", "home", "open", "detected", "occupied", "active", "true", "yes"):
        return True
    if text in ("off", "not_home", "closed", "clear", "unoccupied", "inactive", "false", "no"):
        return False
    raw = parse_state_value(state)
    if is_number(raw):
        return float(raw) >= 0.5
    return None


def transition_edges(rows, value_fn):
    """Return False/True edge timestamps, skipping the first observed state."""
    out = {False: [], True: []}
    have_prev = False
    prev = None
    for row in rows or []:
        value = value_fn(archived_state(row))
        if value is None:
            continue
        value = bool(value)
        if not have_prev:
            prev = value
            have_prev = True
            continue
        if value != prev:
            out[value].append(float(row["ts"]))
            prev = value
    return out


def edge_association_f1(sensor_times, target_times, window_before, post_slop=1.0):
    """Bidirectional edge association score for one transition direction.

    Recall asks whether each target transition had a matching sensor edge shortly before
    it. Precision asks whether each sensor edge was actually followed by the matching
    target transition.  The second term prevents a noisy/high-rate radar from winning just
    because one of its many edges happens to be close to every light action.
    """
    sensor_times = list(sensor_times or [])
    target_times = list(target_times or [])
    if not sensor_times or not target_times:
        return 0.0

    def has_between(times, lo, hi):
        i = bisect_left(times, float(lo))
        return i < len(times) and times[i] <= float(hi)

    recall_hits = sum(
        1 for t in target_times
        if has_between(sensor_times, float(t) - float(window_before), float(t) + float(post_slop))
    )
    precision_hits = sum(
        1 for t in sensor_times
        if has_between(target_times, float(t) - float(post_slop), float(t) + float(window_before))
    )
    recall = recall_hits / max(1, len(target_times))
    precision = precision_hits / max(1, len(sensor_times))
    if recall + precision <= 1e-12:
        return 0.0
    return 2.0 * recall * precision / (recall + precision)


def balanced_presence_driver_score(sensor_edges, target_edges):
    """Balanced ON/OFF causal score used to discover the real occupancy driver.

    ON is expected to be close to the presence edge. OFF deliberately allows the wider
    historical window used by legacy HA automations, because the purpose is to discover
    the sensor that *caused* a delayed OFF and then anchor desired-state learning back to
    that sensor edge.
    """
    directional = []
    for positive, window in (
        (True, float(OPTIONS.get("fast_precursor_on_seconds", 8))),
        (False, float(OPTIONS.get("fast_precursor_off_seconds", 120))),
    ):
        target_times = list((target_edges or {}).get(positive) or [])
        if not target_times:
            continue
        directional.append(edge_association_f1(
            (sensor_edges or {}).get(positive) or [], target_times, window, post_slop=1.0
        ))
    return sum(directional) / len(directional) if directional else 0.0



def _recent_numeric_before(rows, ts, window_before, post_slop=1.0):
    """Most recent numeric sensor value near a target transition."""
    lo = float(ts) - float(window_before)
    hi = float(ts) + float(post_slop)
    best = None
    for row in rows or []:
        rts = float(row.get("ts") or 0.0)
        if rts < lo or rts > hi:
            continue
        raw = parse_state_value(archived_state(row))
        if not is_number(raw):
            continue
        if best is None or rts > best[0]:
            best = (rts, float(raw))
    return None if best is None else best[1]


def numeric_activity_driver_score(sensor_rows, target_edges):
    """Balanced separability score for fast numeric behavioural sensors.

    This catches ESPHome signals such as LD2411 ``Still Energy`` (%) and camera
    ``AI detection`` scores. A useful sensor should be systematically different near
    target ON versus OFF transitions. The score is intentionally bounded and only used
    for entities already classified as behavioural/activity sensors.
    """
    on_vals = []
    off_vals = []
    for t in (target_edges or {}).get(True, []) or []:
        v = _recent_numeric_before(sensor_rows, t, float(OPTIONS.get("fast_precursor_on_seconds", 8)), 1.0)
        if v is not None:
            on_vals.append(v)
    for t in (target_edges or {}).get(False, []) or []:
        v = _recent_numeric_before(sensor_rows, t, float(OPTIONS.get("fast_precursor_off_seconds", 120)), 1.0)
        if v is not None:
            off_vals.append(v)
    if len(on_vals) < 2 or len(off_vals) < 2:
        return 0.0
    all_vals = sorted(on_vals + off_vals)
    lo, hi = all_vals[0], all_vals[-1]
    if abs(hi - lo) < 1e-9:
        return 0.0
    def median(xs):
        ys = sorted(xs); n = len(ys)
        return ys[n//2] if n % 2 else 0.5 * (ys[n//2-1] + ys[n//2])
    mon, moff = median(on_vals), median(off_vals)
    threshold = 0.5 * (mon + moff)
    if mon >= moff:
        on_ok = sum(v >= threshold for v in on_vals) / len(on_vals)
        off_ok = sum(v < threshold for v in off_vals) / len(off_vals)
    else:
        on_ok = sum(v <= threshold for v in on_vals) / len(on_vals)
        off_ok = sum(v > threshold for v in off_vals) / len(off_vals)
    balanced = 0.5 * (on_ok + off_ok)
    separation = min(1.0, abs(mon - moff) / max(1e-9, hi - lo))
    # Require better-than-chance class separation; sample coverage keeps tiny histories
    # from immediately becoming a dominant driver.
    skill = clamp((balanced - 0.5) * 2.0, 0.0, 1.0)
    coverage = clamp(min(len(on_vals), len(off_vals)) / 6.0, 0.0, 1.0)
    return clamp((0.75 * skill + 0.25 * separation) * coverage, 0.0, 1.0)

def is_esphome_sensor_entity(entity_id, registry):
    """True for ESPHome sensor/binary_sensor Entity Registry entries.

    ESPHome devices frequently expose configuration number/select/switch entities next
    to the actual LD24xx presence/radar sensors. Those configuration controls must never
    make the physical sensor channels disappear from the learning universe.
    """
    reg = (registry or {}).get(entity_id) or {}
    platform = str(reg.get("platform") or reg.get("integration") or "").strip().lower()
    domain = str(entity_id or "").split(".", 1)[0]
    return platform == "esphome" and domain in ("sensor", "binary_sensor")


def controllable_context_exclusions(state_map, registry):
    """Exclude actuators without blacklisting useful ESPHome sensing siblings.

    Every directly controllable entity is excluded from every agent input. Device-wide
    sibling exclusion is reserved for *real actuator* domains (light/switch/climate/etc.).
    Configuration-like number/select entities are deliberately NOT allowed to blacklist
    their whole physical ESPHome device. Even on a real ESPHome actuator device,
    sensor/binary_sensor siblings remain eligible; explicit electrical-unit filtering is
    applied separately afterwards.

    This fixes LD2411/LD24xx layouts such as ``binary_sensor.kitchen_presence_presence``
    sharing one ESPHome device with threshold numbers, engineering-mode selects/switches
    and radar diagnostics.
    """
    registry = registry or {}
    controllable_entities = set()
    actuator_devices = set()
    strong_actuator_domains = {
        "light", "switch", "climate", "cover", "fan", "media_player",
        "humidifier", "water_heater",
    }
    for eid, st in (state_map or {}).items():
        try:
            is_controllable = bool(target_options_for_state(st))
        except Exception:
            is_controllable = False
        if not is_controllable:
            continue
        controllable_entities.add(eid)
        domain = str(eid).split(".", 1)[0]
        device_id = (registry.get(eid) or {}).get("device_id")
        if device_id and domain in strong_actuator_domains:
            actuator_devices.add(device_id)

    excluded = set(controllable_entities)
    rescued_esphome_sensors = 0
    if actuator_devices:
        for eid, reg in registry.items():
            if (reg or {}).get("device_id") not in actuator_devices:
                continue
            if eid in controllable_entities:
                continue
            if is_esphome_sensor_entity(eid, registry):
                rescued_esphome_sensors += 1
                continue
            excluded.add(eid)
    return excluded, {
        "detected_controllable_entities": len(controllable_entities),
        "detected_controllable_devices": len(actuator_devices),
        "excluded_controllable_context_entities": len(excluded),
        "esphome_sensor_sibling_overrides": rescued_esphome_sensors,
    }

def select_context_entities(agent, state_map, registry, hint_entities, max_entities=None, relevance_scores=None):
    """Select a compact model from the broad all-entity candidate universe.

    v0.7.12 screens every parseable HA entity except controllable-device inputs and
    entities carrying explicitly electrical units. Historical relevance then chooses a
    compact subset for live inference. This keeps CPU low without hand-written semantic
    blacklists that can accidentally remove phone, car, weather, virtual, camera-score or
    custom ESPHome context. Local/causal occupancy still receives priority for fast lights.
    """
    target = agent["target_entity"]
    domain = target.split(".", 1)[0]
    target_state = state_map.get(target) or {}
    target_reg = registry.get(target) or {}
    target_tokens = _name_tokens(target, target_state)
    need_caps = {x[0] for x in SENSOR_NEEDS.get(domain, [])}
    hints = set(hint_entities or ())
    requested = set(agent.get("input_entities") or ["*"])
    excluded_control_entities, exclusion_meta = controllable_context_exclusions(state_map, registry)
    excluded_electrical_entities, electrical_meta = electrical_context_exclusions(state_map, registry)
    excluded_context_entities = excluded_control_entities | excluded_electrical_entities
    limit_by_dims = max(4, (int(OPTIONS.get("feature_dimensions", 128)) - 9) // 4)
    limit = min(int(max_entities or OPTIONS.get("max_context_entities", 28)), limit_by_dims)
    fast = is_fast_reactive_agent(agent)
    if fast:
        # Fast binary behaviour is normally driven by a handful of causal sensors.
        # Keeping dozens of weak features makes a simple occupancy rule harder to clone
        # and costs CPU.  Prefer a compact short-series schema.
        limit = min(limit, max(2, int(OPTIONS.get("fast_max_context_entities", 8))))
    ranked = []
    considered = 0
    esphome_candidates = 0
    for eid, st in state_map.items():
        if "*" not in requested and eid not in requested:
            continue
        edomain = eid.split(".", 1)[0]
        if not is_context_candidate_entity(eid, st, excluded_context_entities):
            continue
        reg = registry.get(eid) or {}
        considered += 1
        if is_esphome_sensor_entity(eid, registry):
            esphome_candidates += 1
        if eid == target:
            continue
        loc = _context_locality(agent, eid, st, registry, target_state)
        score = 0.0
        reasons = []
        rel = float((relevance_scores or {}).get(eid, 0.0))
        causal_min = float(OPTIONS.get("fast_causal_driver_min_score", 0.50))
        if fast and (loc["occupancy"] or loc.get("activity")) and rel >= causal_min:
            # A historically proven behavioural driver outranks geography/name heuristics.
            # This includes binary presence and numeric ESPHome radar/AI activity scores.
            score += 4800.0 * clamp(rel, 0.0, 1.0); reasons.append("causal-behaviour")
        if fast and loc["occupancy"] and loc["local"]:
            # Primary room occupancy is the most important structural cue for fast lights.
            score += 5200; reasons.append("local-primary")
        elif fast and loc["local"]:
            score += 2200; reasons.append("local-context")
        if eid in hints:
            score += 1500 if fast else 3500
            reasons.append("automation")
        if rel > 0:
            score += (1800.0 if fast else 1400.0) * clamp(rel, 0.0, 1.0)
            reasons.append("historical-precursor")
        if loc["same_device"]:
            score += 1200 if fast else 900; reasons.append("same-device")
        if loc["same_area"]:
            score += 1100 if fast else 700; reasons.append("same-area")
        caps = entity_capability_tags(eid, st)
        matching = need_caps & caps
        if matching:
            score += 620 + 90 * len(matching); reasons.append("sensor-fit")
        if edomain in ("person", "device_tracker", "sun", "weather"):
            score += 180
        elif edomain in ("sensor", "binary_sensor"):
            score += 120
        overlap = target_tokens & _name_tokens(eid, st)
        if overlap:
            score += min(420 if fast else 220, (100 if fast else 55) * len(overlap)); reasons.append("semantic")
        changed = parse_ts((st or {}).get("last_changed"))
        if changed:
            age = max(0.0, now_ts() - changed)
            score += 80.0 * math.exp(-age / 21600.0)
        ranked.append((score, eid, reasons, loc))
    ranked.sort(key=lambda x: (-x[0], x[1]))

    # First reserve historically proven occupancy drivers, regardless of HA area/name.
    # A radar that repeatedly precedes the lamp's ON and OFF transitions is more causal
    # than a merely co-located temperature/presence entity.
    selected = []
    if fast:
        causal_min = float(OPTIONS.get("fast_causal_driver_min_score", 0.50))
        causal_reserve = min(limit, max(0, int(OPTIONS.get("fast_causal_driver_reserve", 2))))
        causal_ranked = [
            x for x in ranked
            if (x[3]["occupancy"] or x[3].get("activity")) and float((relevance_scores or {}).get(x[1], 0.0)) >= causal_min
        ]
        causal_ranked.sort(key=lambda x: (-float((relevance_scores or {}).get(x[1], 0.0)), -x[0], x[1]))
        selected.extend([eid for _, eid, _, _ in causal_ranked[:causal_reserve]])

    # Then reserve structurally local occupancy/context before filling the remainder with
    # upstream/global features. Causal history wins if HA area metadata is wrong/missing.
    reserve = min(limit, max(0, int(OPTIONS.get("primary_local_sensor_reserve", 4)))) if fast else 0
    local_ranked = [x for x in ranked if x[3]["local"]]
    local_occupancy_ranked = [x for x in local_ranked if x[3]["occupancy"]]
    local_target_count = min(limit, len(selected) + reserve)
    for _, eid, _, _ in local_occupancy_ranked + local_ranked:
        if eid not in selected:
            selected.append(eid)
        if len(selected) >= local_target_count:
            break

    # Existing HA automations are the behavioural benchmark. Preserve a bounded number
    # of their trigger/condition entities in the explicit schema (unless the entity is
    # an actuator/electrical input filtered above) so the RL policy has access to the
    # same causal signals the old rules used. Local occupancy still wins the first slots
    # for fast lights; automation inputs fill the next reserved slots.
    automation_reserve = min(
        max(0, limit - len(selected)),
        max(0, int(OPTIONS.get("automation_context_reserve", 8))),
    )
    automation_ranked = [x for x in ranked if "automation" in x[2]]
    added_automation = 0
    for _, eid, _, _ in automation_ranked:
        if eid in selected:
            continue
        selected.append(eid)
        added_automation += 1
        if added_automation >= automation_reserve or len(selected) >= limit:
            break

    if fast:
        # Do not fill the remaining slots with arbitrary whole-home context.  After local
        # and automation-reserved signals, only keep historically specific precursors or
        # sensors that match the target's declared needs.  This is intentionally sparse:
        # a lamp controlled by one presence sensor should look like a one-sensor problem.
        for _, eid, reasons, loc in ranked:
            if eid in selected:
                continue
            rel = float((relevance_scores or {}).get(eid, 0.0))
            caps = entity_capability_tags(eid, state_map.get(eid) or {})
            useful = ("*" not in requested) or loc["local"] or bool(need_caps & caps) or rel >= 0.25 or "automation" in reasons
            if useful:
                selected.append(eid)
            if len(selected) >= limit:
                break
    else:
        for _, eid, _, _ in ranked:
            if eid not in selected:
                selected.append(eid)
            if len(selected) >= limit:
                break
    selected_set = set(selected)
    rationale = {eid: reasons for _, eid, reasons, _ in ranked if eid in selected_set}
    primary_local = [eid for _, eid, _, loc in ranked if eid in selected_set and loc["local"] and loc["occupancy"]]
    occupancy_selected = [x for x in ranked if x[1] in selected_set and x[3]["occupancy"]]
    occupancy_selected.sort(key=lambda x: (
        -float((relevance_scores or {}).get(x[1], 0.0)),
        0 if "causal-behaviour" in x[2] else 1,
        0 if x[3]["local"] else 1,
        -x[0], x[1],
    ))
    primary_occupancy = occupancy_selected[0][1] if occupancy_selected else (primary_local[0] if primary_local else None)
    causal_scores = {
        eid: round(float((relevance_scores or {}).get(eid, 0.0)), 4)
        for _, eid, reasons, loc in occupancy_selected[:6]
        if float((relevance_scores or {}).get(eid, 0.0)) > 0
    }
    behavioural_selected = [x for x in ranked if x[1] in selected_set and (x[3]["occupancy"] or x[3].get("activity"))]
    behavioural_selected.sort(key=lambda x: (-float((relevance_scores or {}).get(x[1], 0.0)), -x[0], x[1]))
    behavioural_scores = {
        eid: round(float((relevance_scores or {}).get(eid, 0.0)), 4)
        for _, eid, _, _ in behavioural_selected[:8]
        if float((relevance_scores or {}).get(eid, 0.0)) > 0
    }
    upstream = [eid for _, eid, reasons, loc in ranked if eid in selected_set and not loc["local"] and ("automation" in reasons or "historical-precursor" in reasons or "causal-behaviour" in reasons)]
    esphome_selected = sum(1 for eid in selected if is_esphome_sensor_entity(eid, registry))
    return selected, {
        "considered_entities": considered, "selected_entities": len(selected),
        "selection_reasons": rationale,
        "primary_local_sensors": primary_local[:4],
        "primary_local_sensor": primary_local[0] if primary_local else None,
        "primary_occupancy_sensor": primary_occupancy,
        "causal_presence_scores": causal_scores,
        "causal_behaviour_scores": behavioural_scores,
        "primary_behavioural_drivers": [eid for _, eid, _, _ in behavioural_selected[:4]],
        "upstream_sensors": upstream[:8],
        "automation_reserved_entities": min(added_automation, automation_reserve),
        "fast_local_profile": bool(fast),
        "esphome_context_candidates": esphome_candidates,
        "esphome_selected_context": esphome_selected,
        **exclusion_meta, **electrical_meta,
    }

class ExplicitFeatureSchema:
    VERSION = 10
    def __init__(self, dims, entities):
        self.dims = int(dims)
        self.entities = list(entities)
        max_entities = max(1, (self.dims - 9) // 4)
        self.entities = self.entities[:max_entities]

    def export(self):
        return {"version": self.VERSION, "dims": self.dims, "entities": self.entities}

    @classmethod
    def from_export(cls, raw, dims):
        if not raw or int(raw.get("version", 0)) != cls.VERSION or int(raw.get("dims", -1)) != int(dims):
            return None
        return cls(dims, raw.get("entities") or [])

    def labels(self):
        out = {0: ["bias"], 1: ["time:hour_sin"], 2: ["time:hour_cos"], 3: ["time:dow_sin"], 4: ["time:dow_cos"]}
        idx = 5
        for eid in self.entities:
            for suffix in ("value", "lag_delta_1", "lag_delta_2", "lag_delta_3"):
                if idx >= self.dims: break
                out[idx] = [f"{eid}:{suffix}"]; idx += 1
        # Use every remaining slot for deterministic pairwise interactions between the
        # highest-ranked entities. This adds a small non-linear residual without a heavy
        # neural runtime on Raspberry Pi.
        vals = self.entities[:8]
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                if idx >= self.dims:
                    break
                out[idx] = [f"interaction:{vals[i]}×{vals[j]}"]
                idx += 1
            if idx >= self.dims:
                break
        return out


class TemporalHistory:
    """Small in-memory timeline used by live inference and historical replay."""
    def __init__(self, maxlen=96):
        self.samples = {}
        self.maxlen = maxlen

    def add(self, entity_id, ts, state):
        if not entity_id or state is None: return
        dq = self.samples.setdefault(entity_id, deque(maxlen=self.maxlen))
        t = float(ts)
        if dq and abs(dq[-1][0] - t) < 1e-6:
            dq[-1] = (t, state)
        elif not dq or t >= dq[-1][0]:
            dq.append((t, state))

    def previous(self, entity_id, at_ts):
        dq = self.samples.get(entity_id)
        if not dq: return None
        target = float(at_ts)
        for ts, st in reversed(dq):
            if ts <= target:
                return st
        return None

    def last_change_ts(self, entity_id, fallback_state=None):
        dq = self.samples.get(entity_id)
        if dq:
            return dq[-1][0]
        return parse_ts((fallback_state or {}).get("last_changed"))


class HistoricalTemporalTracker:
    """Indexed as-of snapshots; callers may request times in any order.

    Dwells of different agents overlap. A shared forward-only cursor silently
    exposed future context to earlier samples. Binary search prevents that leak.
    """
    def __init__(self, rows, watched_entities=None):
        watched = set(watched_entities or ())
        self.index = {}
        for row in rows:
            eid = row["entity_id"]
            if watched and eid not in watched:
                continue
            times, states = self.index.setdefault(eid, ([], []))
            times.append(float(row["ts"]))
            states.append(archived_state(row))
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)

    def advance(self, ts):
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)
        for eid, (times, states) in self.index.items():
            end = bisect_right(times, float(ts))
            if end:
                self.state_map[eid] = states[end - 1]
                self.history.samples[eid] = deque(zip(times[max(0,end-64):end], states[max(0,end-64):end]), maxlen=64)

    def directional_transition_before(self, entity_id, at_ts, positive, window):
        data = self.index.get(entity_id)
        if not data:
            return None
        times, states = data
        end = bisect_right(times, float(at_ts))
        start = max(1, bisect_right(times, float(at_ts) - float(window)) - 1)
        for i in range(end - 1, start - 1, -1):
            cur = state_scalar(states[i]); prev = state_scalar(states[i - 1])
            if cur is None or prev is None:
                continue
            if positive and cur > 0.25 and prev <= 0.25:
                return times[i]
            if not positive and cur < -0.25 and prev >= -0.25:
                return times[i]
        return None

    def first_directional_transition_after(self, entity_id, start_ts, end_ts, positive):
        data = self.index.get(entity_id)
        if not data:
            return None
        times, states = data
        start = max(1, bisect_right(times, float(start_ts)))
        end = bisect_right(times, float(end_ts))
        for i in range(start, end):
            cur = state_scalar(states[i]); prev = state_scalar(states[i - 1])
            if cur is None or prev is None:
                continue
            if positive and cur > 0.25 and prev <= 0.25:
                return times[i]
            if not positive and cur < -0.25 and prev >= -0.25:
                return times[i]
        return None


def build_explicit_features(schema, state_map, temporal, at_ts=None, agent=None, excluded_entities=None):
    at_ts = float(at_ts if at_ts is not None else now_ts())
    dt = datetime.fromtimestamp(at_ts).astimezone()
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    dow = dt.weekday()
    fast_profile = bool(agent and is_fast_reactive_agent(agent))
    clock_weight = clamp(float(OPTIONS.get("fast_clock_context_weight", 0.15)), 0.0, 1.0) if fast_profile else 1.0
    vec = {
        0: 1.0,
        1: clock_weight * math.sin(2 * math.pi * hour / 24),
        2: clock_weight * math.cos(2 * math.pi * hour / 24),
        3: clock_weight * math.sin(2 * math.pi * dow / 7),
        4: clock_weight * math.cos(2 * math.pi * dow / 7),
    }
    labels = schema.labels()
    default_short_s = float(OPTIONS.get("temporal_short_seconds", 60))
    default_long_s = float(OPTIONS.get("temporal_long_seconds", 300))
    base_values = {}
    idx = 5
    usable = 0
    excluded_entities = set(excluded_entities or ())
    for eid in schema.entities:
        st = state_map.get(eid)
        cur = None if eid in excluded_entities else (context_scalar(eid, st, agent) if st else None)
        if cur is None:
            cur = 0.0
        else:
            usable += 1
        caps = entity_capability_tags(eid, st or {})
        sharp = fast_profile
        if sharp:
            # Fast targets learn from a compact causal time series rather than a static
            # whole-home snapshot. Current value plus 1/3/10 s deltas captures occupancy
            # edges, direction and very short trends without carrying minute-scale memory.
            lags = parse_fast_series_lags()
            lag_values = []
            for lag_s in lags:
                lag_st = None if eid in excluded_entities else (temporal.previous(eid, at_ts - lag_s) if temporal else None)
                lag_v = context_scalar(eid, lag_st, agent) if lag_st else cur
                lag_values.append(cur - (lag_v if lag_v is not None else cur))
            vals = (cur, lag_values[0], lag_values[1], lag_values[2])
        else:
            short_s = default_short_s
            long_s = default_long_s
            recent_tau = max(30.0, long_s)
            if eid in excluded_entities:
                short_st = long_st = None
                short_v = long_v = cur
                changed_ts = None
                recent = 0.0
            else:
                short_st = temporal.previous(eid, at_ts - short_s) if temporal else None
                long_st = temporal.previous(eid, at_ts - long_s) if temporal else None
                short_v = context_scalar(eid, short_st, agent) if short_st else cur
                long_v = context_scalar(eid, long_st, agent) if long_st else cur
                changed_ts = temporal.last_change_ts(eid, st) if temporal else parse_ts((st or {}).get("last_changed"))
                age = max(0.0, at_ts - float(changed_ts)) if changed_ts else 86400.0
                recent = math.exp(-age / max(0.25, recent_tau))
            vals = (cur, cur - (short_v if short_v is not None else cur), cur - (long_v if long_v is not None else cur), recent)
        base_values[eid] = cur
        if agent and eid == agent.get("target_entity"):
            target_name = str(agent.get("target_property") or "target")
            for off, suffix in enumerate((target_name, f"{target_name}_delta_short", f"{target_name}_delta_long", "recent_change")):
                if idx + off < schema.dims:
                    labels[idx + off] = [f"{eid}:{suffix}"]
        for v in vals:
            if idx >= schema.dims: break
            if abs(float(v)) > 1e-12: vec[idx] = float(v)
            idx += 1
    top = schema.entities[:8]
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            if idx >= schema.dims:
                break
            v = base_values.get(top[i], 0.0) * base_values.get(top[j], 0.0)
            if abs(v) > 1e-12:
                vec[idx] = v
            idx += 1
        if idx >= schema.dims:
            break
    return vec, labels, {
        "usable_entities": usable, "selected_entities": len(schema.entities), "dimensions": schema.dims,
        "excluded_controllable_inputs": len(set(schema.entities) & excluded_entities),
    }


def action_values(agent):
    lo, hi = float(agent["min_value"]), float(agent["max_value"])
    if agent["target_property"] == "option_index":
        return [float(i) for i in range(int(round(lo)), int(round(hi)) + 1)]
    if agent["target_property"] == "power" or hi - lo <= 1.01:
        return [lo, hi]
    bins = max(5, int(OPTIONS["action_bins"]))
    step = (hi - lo) / (bins - 1)
    return [lo + i * step for i in range(bins)]


class DiagonalLinUCB:
    """Per-horizon lightweight RL head with context-support statistics."""
    def __init__(self, dims, actions, alpha=0.65, model=None):
        self.dims = int(dims); self.actions = [float(x) for x in actions]; self.alpha = float(alpha)
        n = len(self.actions)
        valid = bool(model and int(model.get("dims", -1)) == self.dims and len(model.get("actions", [])) == n)
        if valid:
            self.a = model["a"]; self.b = model["b"]
            self.counts = model.get("counts", [0] * n); self.reward_sums = model.get("reward_sums", [0.0] * n)
            self.ctx_sum = model.get("ctx_sum", [[0.0] * self.dims for _ in range(n)])
            self.ctx_sq = model.get("ctx_sq", [[0.0] * self.dims for _ in range(n)])
            self.total_updates = int(model.get("total_updates", sum(self.counts)))
            self.validation_weight = float(model.get("validation_weight", 0.0))
            self.validation_correct_weight = float(model.get("validation_correct_weight", 0.0))
            self.validation_samples = int(model.get("validation_samples", 0))
            self.validation_pred_weight = list(model.get("validation_pred_weight", [0.0] * n))
            self.validation_pred_correct_weight = list(model.get("validation_pred_correct_weight", [0.0] * n))
            if len(self.validation_pred_weight) != n:
                self.validation_pred_weight = [0.0] * n
            if len(self.validation_pred_correct_weight) != n:
                self.validation_pred_correct_weight = [0.0] * n
        else:
            self.a = [[1.0] * self.dims for _ in range(n)]; self.b = [[0.0] * self.dims for _ in range(n)]
            self.counts = [0] * n; self.reward_sums = [0.0] * n
            self.ctx_sum = [[0.0] * self.dims for _ in range(n)]; self.ctx_sq = [[0.0] * self.dims for _ in range(n)]
            self.total_updates = 0
            self.validation_weight = 0.0
            self.validation_correct_weight = 0.0
            self.validation_samples = 0
            self.validation_pred_weight = [0.0] * n
            self.validation_pred_correct_weight = [0.0] * n

    def _arm(self, action_idx, x):
        aa, bb = self.a[action_idx], self.b[action_idx]
        mean = 0.0; uncertainty_sq = 0.0; active = 0
        for idx, value in x.items():
            if idx >= self.dims: continue
            inv = 1.0 / max(aa[idx], 1e-9)
            mean += (bb[idx] * inv) * value
            uncertainty_sq += value * value * inv; active += 1
        uncertainty = math.sqrt(max(0.0, uncertainty_sq) / max(1, active))
        return mean, uncertainty

    def context_support(self, action_idx, x):
        # Novelty is judged against the whole historical context distribution, while
        # local action coverage still matters. This avoids unfairly marking a continuous
        # brightness/setpoint bin as OOD merely because that exact bin was used rarely.
        total = max(0, int(self.total_updates))
        if total < 3:
            return (0.0, 1.0)
        local_n = int(self.counts[action_idx])
        z2 = 0.0; used = 0
        for idx, value in x.items():
            if idx <= 0 or idx >= self.dims: continue
            ss = sum(a[idx] for a in self.ctx_sum)
            sq = sum(a[idx] for a in self.ctx_sq)
            mean = ss / total
            var = max(0.020, sq / total - mean * mean)
            z = (value - mean) / math.sqrt(var)
            z2 += min(16.0, z * z); used += 1
        dist = math.sqrt(z2 / max(1, used))
        novelty = clamp(1.0 - math.exp(-dist / 2.6), 0.0, 1.0)
        global_coverage = 1.0 - math.exp(-total / 20.0)
        local_coverage = 1.0 - math.exp(-local_n / 3.0)
        support = clamp(global_coverage * (1.0 - novelty) * (0.55 + 0.45 * local_coverage), 0.0, 1.0)
        return support, novelty

    def evaluate(self, x):
        arms = []
        for i, value in enumerate(self.actions):
            mean, uncertainty = self._arm(i, x)
            support, novelty = self.context_support(i, x)
            arms.append({"index": i, "value": value, "mean": mean, "uncertainty": uncertainty,
                         "ucb": mean + self.alpha * uncertainty, "count": int(self.counts[i]),
                         "support": support, "novelty": novelty})
        return arms

    def choose(self, x, explore=False, allowed_indices=None):
        arms = self.evaluate(x)
        allowed = set(allowed_indices) if allowed_indices is not None else set(range(len(arms)))
        candidates = [a for a in arms if a["index"] in allowed] or arms
        key = "ucb" if explore else "mean"
        best_score = max(a[key] for a in candidates)
        tied = [a for a in candidates if abs(a[key] - best_score) < 1e-12]
        chosen = dict(random.choice(tied) if explore else min(tied, key=lambda a: a["index"]))
        structural = self.structural_confidence(arms, chosen["index"])
        calibration = self.calibration(chosen["index"])
        confidence = min(structural, calibration["ceiling"])
        chosen["structural_confidence"] = structural
        chosen["validation_accuracy"] = calibration["accuracy"]
        chosen["validation_lower_bound"] = calibration["ceiling"]
        chosen["validation_samples"] = calibration["samples"]
        return chosen, confidence, arms

    def structural_confidence(self, arms, chosen_idx):
        if self.total_updates <= 0: return 0.0
        chosen = arms[chosen_idx]
        ranked = sorted((a["mean"] for a in arms), reverse=True)
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else abs(ranked[0])
        margin_score = 2.0 * abs(sigmoid(3.5 * margin) - 0.5)
        uncertainty_score = math.exp(-1.6 * chosen["uncertainty"])
        coverage = 1.0 - math.exp(-self.total_updates / max(18.0, len(self.actions) * 1.8))
        local_coverage = 1.0 - math.exp(-chosen["count"] / 4.0)
        raw = coverage * (0.38 * uncertainty_score + 0.27 * margin_score + 0.18 * local_coverage + 0.17 * chosen["support"])
        return clamp(raw, 0.0, 0.995)

    @staticmethod
    def _wilson_lower(correct, total, z=1.0):
        if total <= 0:
            return 0.0
        p = clamp(float(correct) / float(total), 0.0, 1.0)
        den = 1.0 + (z * z) / total
        centre = p + (z * z) / (2.0 * total)
        spread = z * math.sqrt(max(0.0, p * (1.0 - p) / total + (z * z) / (4.0 * total * total)))
        return clamp((centre - spread) / den, 0.0, 1.0)

    def calibration(self, predicted_idx):
        min_samples = max(4, int(OPTIONS.get("confidence_min_validation_samples", 12)))
        global_total = float(self.validation_weight)
        global_correct = float(self.validation_correct_weight)
        action_total = float(self.validation_pred_weight[predicted_idx]) if predicted_idx < len(self.validation_pred_weight) else 0.0
        action_correct = float(self.validation_pred_correct_weight[predicted_idx]) if predicted_idx < len(self.validation_pred_correct_weight) else 0.0
        # Use action-specific reliability when it has enough held-out support, otherwise
        # fall back to the global held-out reliability. Confidence is never allowed above
        # a conservative Wilson lower bound. This makes "90% confidence" mean the policy
        # has actually been close to that reliable on unseen recent history.
        # A common OFF action cannot certify an untested ON action.
        total, correct = action_total, action_correct
        accuracy = (correct / total) if total > 0 else 0.0
        if total < min_samples:
            # No out-of-sample proof yet: cap confidence below Control defaults.
            progress = clamp(total / max(1.0, float(min_samples)), 0.0, 1.0)
            ceiling = 0.25 + 0.35 * progress
        else:
            ceiling = self._wilson_lower(correct, total, z=1.0)
        return {"accuracy": accuracy, "ceiling": clamp(ceiling, 0.0, 0.995), "samples": int(round(total))}

    def validate(self, action_idx, x, reward):
        reward = clamp(float(reward), -1.0, 1.0)
        if reward < 0.15:
            return
        arms = self.evaluate(x)
        predicted = max(arms, key=lambda a: a["mean"])["index"]
        # Positive reward means the logged desired state was accepted; negative reward
        # means that action was quickly rejected, so predicting a different action counts
        # as the correct held-out decision.
        # Rejecting one setting does not establish that every other setting is right.
        correct = predicted == action_idx
        weight = max(0.25, abs(reward))
        self.validation_weight += weight
        self.validation_correct_weight += weight if correct else 0.0
        self.validation_samples += 1
        self.validation_pred_weight[predicted] += weight
        if correct:
            self.validation_pred_correct_weight[predicted] += weight

    def update(self, action_idx, x, reward):
        reward = clamp(float(reward), -1.0, 1.0)
        aa, bb = self.a[action_idx], self.b[action_idx]
        ss, sq = self.ctx_sum[action_idx], self.ctx_sq[action_idx]
        for idx, value in x.items():
            if idx >= self.dims: continue
            aa[idx] += value * value; bb[idx] += reward * value
            ss[idx] += value; sq[idx] += value * value
        self.counts[action_idx] += 1; self.reward_sums[action_idx] += reward; self.total_updates += 1

    def export(self):
        return {"version": 4, "dims": self.dims, "actions": self.actions, "alpha": self.alpha,
                "a": self.a, "b": self.b, "counts": self.counts, "reward_sums": self.reward_sums,
                "ctx_sum": self.ctx_sum, "ctx_sq": self.ctx_sq, "total_updates": self.total_updates,
                "validation_weight": self.validation_weight,
                "validation_correct_weight": self.validation_correct_weight,
                "validation_samples": self.validation_samples,
                "validation_pred_weight": self.validation_pred_weight,
                "validation_pred_correct_weight": self.validation_pred_correct_weight}


class MultiHorizonPolicy:
    VERSION = 9
    def __init__(self, agent, state_map, registry, hint_entities, model=None, relevance_scores=None):
        self.agent = agent
        self.dims = int(OPTIONS.get("feature_dimensions", 128))
        self.actions = action_values(agent)
        self.alpha = float(OPTIONS.get("rl_alpha", 0.65))
        self.horizons = parse_horizons(agent)
        self.registry = dict(registry or {})
        excluded_control, control_meta = controllable_context_exclusions(state_map, self.registry)
        excluded_electrical, electrical_meta = electrical_context_exclusions(state_map, self.registry)
        self.excluded_context_entities = excluded_control | excluded_electrical
        self.context_exclusion_meta = {**control_meta, **electrical_meta}
        valid_model = bool(model and int(model.get("version", 0)) == self.VERSION)
        raw_schema = (model or {}).get("schema") if valid_model else None
        self.schema = ExplicitFeatureSchema.from_export(raw_schema, self.dims)
        selection_meta = dict((model or {}).get("selection_meta") or {}) if valid_model else {}
        if self.schema is None:
            selected, selection_meta = select_context_entities(agent, state_map, registry, hint_entities, relevance_scores=relevance_scores)
            self.schema = ExplicitFeatureSchema(self.dims, selected)
        if not selection_meta or not selection_meta.get("selection_reasons"):
            _, fresh_meta = select_context_entities(agent, state_map, registry, hint_entities, max_entities=len(self.schema.entities), relevance_scores=relevance_scores)
            fresh_meta["selected_entities"] = len(self.schema.entities)
            fresh_meta["selection_reasons"] = {k: v for k, v in fresh_meta.get("selection_reasons", {}).items() if k in set(self.schema.entities)}
            fresh_meta["primary_local_sensors"] = [x for x in fresh_meta.get("primary_local_sensors", []) if x in set(self.schema.entities)]
            fresh_meta["primary_local_sensor"] = next(iter(fresh_meta["primary_local_sensors"]), None)
            if fresh_meta.get("primary_occupancy_sensor") not in set(self.schema.entities):
                fresh_meta["primary_occupancy_sensor"] = fresh_meta.get("primary_local_sensor")
            fresh_meta["causal_presence_scores"] = {k:v for k,v in (fresh_meta.get("causal_presence_scores") or {}).items() if k in set(self.schema.entities)}
            fresh_meta["causal_behaviour_scores"] = {k:v for k,v in (fresh_meta.get("causal_behaviour_scores") or {}).items() if k in set(self.schema.entities)}
            fresh_meta["primary_behavioural_drivers"] = [x for x in fresh_meta.get("primary_behavioural_drivers", []) if x in set(self.schema.entities)]
            fresh_meta["upstream_sensors"] = [x for x in fresh_meta.get("upstream_sensors", []) if x in set(self.schema.entities)]
            selection_meta = fresh_meta
        selection_meta.update(self.context_exclusion_meta)
        self.selection_meta = selection_meta
        raw_heads = (model or {}).get("heads", {}) if model and int(model.get("version", 0)) == self.VERSION else {}
        self.heads = {h: DiagonalLinUCB(self.dims, self.actions, self.alpha, raw_heads.get(str(h))) for h in self.horizons}

    @property
    def total_updates(self):
        return max([h.total_updates for h in self.heads.values()] or [0])

    def features(self, state_map, temporal, at_ts=None):
        # Re-evaluate direct controllability from the live map on every inference. The
        # registry snapshot covers sibling entities; registry updates clear policy caches.
        excluded_control, _ = controllable_context_exclusions(state_map, self.registry)
        excluded_electrical, _ = electrical_context_exclusions(state_map, self.registry)
        self.excluded_context_entities = excluded_control | excluded_electrical
        return build_explicit_features(
            self.schema, state_map, temporal, at_ts, self.agent,
            excluded_entities=self.excluded_context_entities,
        )

    def choose(self, features, explore=False, allowed_indices=None):
        # v0.6 defaults to one reactive head. Multiple heads remain configurable for
        # experimentation, but the normal product path evaluates the current event-time
        # context and acts immediately.
        candidates = []
        for h, head in self.heads.items():
            chosen, conf, arms = head.choose(features, explore=explore, allowed_indices=allowed_indices)
            utility = chosen["mean"] + 0.20 * conf + 0.18 * chosen["support"] - 0.12 * chosen["novelty"] - 0.012 * (h / 60.0)
            candidates.append((utility, h, chosen, conf, arms))
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, horizon, chosen, conf, arms = candidates[0]
        return chosen, conf, arms, horizon, chosen.get("support", 0.0), chosen.get("novelty", 1.0)

    def update(self, horizon, action_idx, features, reward):
        self.heads[int(horizon)].update(action_idx, features, reward)

    def update_all(self, action_idx, features_by_horizon, reward):
        for h, features in features_by_horizon.items():
            if int(h) in self.heads:
                self.heads[int(h)].update(action_idx, features, reward)

    def export(self):
        return {"version": self.VERSION, "dims": self.dims, "actions": self.actions, "horizons": self.horizons,
                "schema": self.schema.export(), "selection_meta": self.selection_meta,
                "heads": {str(h): head.export() for h, head in self.heads.items()}}


def capability_inventory(state_map, entity_ids=None):
    caps = set()
    allowed = set(entity_ids) if entity_ids is not None else None
    for entity_id, state in state_map.items():
        if allowed is not None and entity_id not in allowed:
            continue
        if is_electrical_measurement_entity(entity_id, state):
            continue
        attrs = state.get("attributes") or {}
        dc = str(attrs.get("device_class") or "").lower()
        unit = str(attrs.get("unit_of_measurement") or "").lower()
        text = f"{entity_id.lower()} {dc} {unit} {str(attrs.get('friendly_name') or '').lower()}"
        domain = entity_id.split(".", 1)[0]
        if domain in ("person", "device_tracker") or any(t in text for t in ("occupancy", "presence", "motion")):
            caps.add("occupancy")
        if "illuminance" in text or "lux" in text or unit == "lx":
            caps.add("illuminance")
        if domain == "sun" or "solar elevation" in text or "sun elevation" in text:
            caps.add("sun")
        if "temperature" in text or "°c" in unit or "°f" in unit:
            caps.add("temperature")
            if any(t in text for t in ("outdoor", "outside", "zewn", "external")):
                caps.add("outdoor_temperature")
        if "humidity" in text or unit == "%" and "humidity" in entity_id.lower():
            caps.add("humidity")
        if "co2" in text or "carbon dioxide" in text:
            caps.add("co2")
        if "voc" in text or "volatile organic" in text or "air quality" in text:
            caps.add("voc")
        if dc in ("door", "window", "opening") or any(t in text for t in ("window", "door", "okno", "drzwi")):
            caps.add("window")
        if "power" in text or unit in ("w", "kw"):
            caps.add("power")
        if any(t in text for t in ("noise", "sound level", "decibel", "db")):
            caps.add("ambient_noise")
    return caps


def sensor_recommendations(agent, state_map, policy_confidence, selected_entities=None):
    domain = agent["target_entity"].split(".", 1)[0]
    # Recommendations should describe the context this agent can actually use, not just
    # whether a sensor of that class exists somewhere else in the house.
    present = capability_inventory(state_map, selected_entities) if selected_entities else capability_inventory(state_map)
    recs = []
    for cap, label, reason in SENSOR_NEEDS.get(domain, []):
        if cap not in present:
            recs.append({"capability": cap, "label": label, "reason": reason, "priority": "high" if policy_confidence < 0.65 else "medium"})
    # Always make the uncertainty limitation explicit: this is a heuristic, not causal discovery.
    if not recs and policy_confidence < 0.45:
        recs.append({
            "capability": "more_feedback", "label": "More RL feedback",
            "reason": "The expected sensor classes are already present; low confidence currently comes mainly from too few rewarded interactions.",
            "priority": "medium",
        })
    return recs[:4], sorted(present)




def archived_state(row):
    try:
        attrs = json.loads(row.get("attributes_json") or "{}")
    except Exception:
        attrs = {}
    return {
        "entity_id": row["entity_id"],
        "state": row.get("state"),
        "attributes": attrs,
        "context": {"user_id": row.get("context_user_id")},
        "last_changed": iso_from_ts(row["ts"]),
        "last_updated": iso_from_ts(row["ts"]),
    }


def target_options_for_state(state):
    if not state:
        return []
    domain = state.get("entity_id", "").split(".", 1)[0]
    attrs = state.get("attributes") or {}
    out = []
    for opt in SUPPORTED_TARGETS.get(domain, []):
        item = dict(opt)
        if domain == "light" and item["property"] == "brightness_pct":
            modes = attrs.get("supported_color_modes") or []
            if attrs.get("brightness") is None and not any(m not in ("onoff", None) for m in modes):
                continue
        if domain == "climate" and item["property"] == "temperature" and attrs.get("temperature") is None:
            continue
        if domain == "cover" and item["property"] == "position" and attrs.get("current_position") is None:
            continue
        if domain == "fan" and item["property"] == "percentage" and attrs.get("percentage") is None:
            continue
        if domain == "media_player" and item["property"] == "volume_pct" and attrs.get("volume_level") is None:
            continue
        if domain == "humidifier" and item["property"] == "humidity" and attrs.get("humidity") is None:
            continue
        if domain == "water_heater" and item["property"] == "temperature" and attrs.get("temperature") is None:
            continue
        if domain in ("select", "input_select") and item["property"] == "option_index":
            options = list(attrs.get("options") or [])
            if len(options) < 2:
                continue
            item["min"] = 0
            item["max"] = len(options) - 1
            item["deadband"] = 0.5
            item["exploration_step"] = 1
            item["option_labels"] = options
        if domain in ("number", "input_number"):
            if attrs.get("min") is not None:
                item["min"] = float(attrs["min"])
            if attrs.get("max") is not None:
                item["max"] = float(attrs["max"])
            if attrs.get("step") is not None:
                item["deadband"] = max(float(attrs["step"]), 1e-6)
                item["exploration_step"] = max(float(attrs["step"]), (item["max"] - item["min"]) * 0.02)
        if domain in ("climate", "water_heater"):
            item["min"] = float(attrs.get("min_temp", item["min"]))
            item["max"] = float(attrs.get("max_temp", item["max"]))
            step = float(attrs.get("target_temp_step") or .5)
            item["deadband"] = max(.01, step * .5)
            item["exploration_step"] = step
        if domain == "humidifier":
            item["min"] = float(attrs.get("min_humidity", item["min"]))
            item["max"] = float(attrs.get("max_humidity", item["max"]))
        out.append(item)
    return out


def historical_acceptance_seconds(agent):
    domain = agent["target_entity"].split(".", 1)[0]
    return {
        "light": 300, "switch": 600, "input_boolean": 600,
        "climate": 1800, "cover": 900, "fan": 600,
        "number": 600, "input_number": 600, "media_player": 300,
        "humidifier": 900, "water_heater": 1800, "select": 600, "input_select": 600,
    }.get(domain, 600)


def historical_reward(agent, dwell_seconds, user_id=None, next_user_id=None):
    """Infer a conservative offline reward from how long a desired state persisted.

    v0.6 deliberately does *not* use the live 90 s correction window as a historical
    rejection rule. A hallway light that is ON for 15 seconds can be exactly right.
    Strong negative historical evidence is reserved for a rapid user correction of a
    non-user/automatic action (or an almost immediate user re-correction).
    """
    dwell = max(0.0, float(dwell_seconds))
    prop = str(agent.get("target_property") or "")
    domain = str(agent.get("target_entity") or "").split(".", 1)[0]

    if prop in ("power", "option_index"):
        tau = 10.0
        correction_window = 8.0
    elif prop in ("brightness_pct", "position", "percentage", "volume_pct", "value"):
        tau = 30.0
        correction_window = 12.0
    elif domain in ("climate", "humidifier", "water_heater"):
        tau = 300.0
        correction_window = 45.0
    else:
        tau = 60.0
        correction_window = 15.0

    # A quick explicit human override of an automatic/external action is the clearest
    # negative preference signal we can recover from Recorder history.
    if next_user_id and not user_id and dwell <= correction_window:
        severity = 1.0 - 0.45 * (dwell / max(correction_window, 1.0))
        return -clamp(severity, 0.45, 1.0)
    # If the same user changes a setting almost immediately, treat it as a likely
    # correction; after that, a short dwell is allowed to be intentional.
    if next_user_id and user_id and dwell <= min(2.0, correction_window):
        return -clamp(1.0 - 0.25 * dwell, 0.5, 1.0)

    # Persistence itself is positive desired-state evidence. Saturation is domain aware:
    # binary lights become informative within seconds; HVAC setpoints need minutes.
    reward = 0.15 + 0.85 * (1.0 - math.exp(-dwell / max(tau, 1.0)))
    if dwell < 0.5:
        reward *= 0.35
    # Automatic/external actions are useful (especially existing HA automations), but
    # explicit user-originated states remain slightly more authoritative.
    if not user_id:
        reward *= 0.80
    return clamp(reward, 0.02, 1.0)


def default_action_interval(entity_id, target_property):
    domain = str(entity_id or "").split(".", 1)[0]
    if domain in ("light", "switch", "input_boolean", "media_player", "select", "input_select"):
        return 1.0
    if domain in ("fan", "cover"):
        return 2.0
    if domain in ("number", "input_number"):
        return 2.0
    if domain in ("climate", "humidifier", "water_heater"):
        return 10.0
    return 5.0


class HistoryManager(threading.Thread):
    """Bootstraps HA Recorder history, keeps a longer local archive, discovers active targets,
    and turns logged trajectories into offline contextual-RL experiences."""
    daemon = True

    def __init__(self, engine):
        super().__init__(name="adaptive-ai-history")
        self.engine = engine
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.phase = "waiting"
        self.progress = 0.0
        self.message = "Waiting for Home Assistant"
        self.last_run = None
        self.discovered_controllable = 0
        self.discovered_active = 0
        self.discovery_eligible = 0
        self.discovery_filtered_config = 0
        self.discovery_inactive = 0
        self.auto_created = 0
        self.trained_new = 0
        self.error = None
        # Status must remain cheap while Recorder import is busy. In v0.3.3 the UI
        # could sit on “Connecting…” because /api/status waited for a COUNT() on the
        # archive while a large write transaction held the Store lock. Keep a cache
        # instead and refresh it only at safe checkpoints.
        try:
            self.archive_cache = STORE.archive_stats()
        except Exception:
            self.archive_cache = {"n": 0, "min_ts": None, "max_ts": None, "entities": 0, "days": 0.0, "by_source": {}}
        self.cycle_started_at = None
        self.phase_started_at = None
        self.eta_seconds = None
        self.stage_eta_seconds = None
        self.progress_rate_per_min = None
        self._progress_samples = []
        self.chunk_done = 0
        self.chunk_total = 0
        self.work_done = 0
        self.work_total = 0
        self.work_unit = None
        self.eta_source = None
        self.phase_detail = None
        self.context_candidate_count = 0
        self.esphome_candidate_count = 0
        self.esphome_sensor_sibling_overrides = 0
        self.agent_jobs = set()
        self.agent_jobs_lock = threading.RLock()
        if bool(OPTIONS.get("manual_agent_training", True)):
            paused = STORE.pause_stale_training_agents()
            if paused:
                STORE.event(None, "info", "manual_training_migration", f"Paused {paused} unfinished automatic training job(s); resume manually when ready", None)

    def status(self):
        with self.lock:
            d = {
                "phase": self.phase, "progress": self.progress, "message": self.message,
                "last_run": self.last_run, "controllable": self.discovered_controllable,
                "active": self.discovered_active, "eligible": self.discovery_eligible,
                "filtered_config": self.discovery_filtered_config, "inactive": self.discovery_inactive,
                "auto_created": self.auto_created, "trained_new": self.trained_new, "error": self.error,
                "eta_seconds": self.eta_seconds, "stage_eta_seconds": self.stage_eta_seconds,
                "elapsed_seconds": (now_ts() - self.cycle_started_at) if self.cycle_started_at else 0,
                "progress_rate_per_min": self.progress_rate_per_min,
                "chunk_done": self.chunk_done, "chunk_total": self.chunk_total,
                "work_done": self.work_done, "work_total": self.work_total, "work_unit": self.work_unit,
                "eta_source": self.eta_source, "phase_detail": self.phase_detail,
                "context_candidates": self.context_candidate_count,
                "esphome_context_candidates": self.esphome_candidate_count,
                "esphome_sensor_sibling_overrides": self.esphome_sensor_sibling_overrides,
                "archive": dict(self.archive_cache),
            }
        return d

    def qualified_context_entities(self):
        """Context Recorder maintenance set after candidate qualification.

        Live state_changed events still archive the whole home, but expensive historical
        refreshes only need features consumed by qualified policies.
        """
        ids = set()
        for agent in STORE.qualified_agents():
            model = STORE.get_model(agent["id"]) or {}
            schema = model.get("schema") or {}
            ids.update(schema.get("entities") or [])
        return sorted(ids)

    def _training_bounds(self):
        stats = STORE.archive_stats()
        end_ts = float(stats.get("max_ts") or now_ts())
        start_ts = float(stats.get("min_ts") or (end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400.0))
        return start_ts, end_ts

    def _start_agent_job(self, agent_id, rebuild=False):
        agent = STORE.get_agent(agent_id)
        if not agent:
            return False
        with self.agent_jobs_lock:
            if agent_id in self.agent_jobs:
                return False
            max_jobs = max(1, int(OPTIONS.get("max_concurrent_training_jobs", 1)))
            if len(self.agent_jobs) >= max_jobs:
                return False
            self.agent_jobs.add(agent_id)

        if rebuild:
            STORE.clear_learning(agent_id)
            self.engine.models.pop(agent_id, None)
            self.engine.runtime.pop(agent_id, None)
        else:
            # Resume preserves policy, experiences, benchmark counts and cursor.
            STORE.set_training_state(
                agent_id, "training", score=agent.get("benchmark_score"),
                samples=agent.get("benchmark_samples") or 0, source=agent.get("benchmark_source"),
                detail=agent.get("benchmark_detail") or {},
            )

        def worker():
            try:
                self._run_agent_indexing(agent_id, rebuild=rebuild)
            except Exception as exc:
                STORE.event(agent_id, "error", "agent_index_failed", str(exc), {"trace": traceback.format_exc(limit=6)})
                # Keep TRAINING + cursor: a later Resume continues from the last checkpoint.
            finally:
                with self.agent_jobs_lock:
                    self.agent_jobs.discard(agent_id)
                # Explicitly collect the large temporary replay structures after each job.
                gc.collect()

        threading.Thread(target=worker, name=f"adaptive-ai-index-{agent_id}", daemon=True).start()
        return True

    def _eligible_rebuild_context(self):
        with self.engine.lock:
            current = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        excluded_control, control_meta = controllable_context_exclusions(current, registry)
        excluded_electrical, _ = electrical_context_exclusions(current, registry)
        excluded = excluded_control | excluded_electrical
        out = []
        esphome = 0
        for eid, st in current.items():
            if not is_context_candidate_entity(eid, st, excluded):
                continue
            out.append(eid)
            if is_esphome_sensor_entity(eid, registry):
                esphome += 1
        out = sorted(set(out))
        with self.lock:
            self.context_candidate_count = len(out)
            self.esphome_candidate_count = esphome
            self.esphome_sensor_sibling_overrides = int(control_meta.get("esphome_sensor_sibling_overrides") or 0)
        return out

    def _eligible_presence_context(self):
        """Small high-resolution refresh set for fast behavioural driver discovery.

        Kept under the historical method name for compatibility. It includes classic
        occupancy plus non-diagnostic ESPHome radar/AI activity scores.
        """
        with self.engine.lock:
            current = dict(self.engine.state_map)
        eligible = set(self._eligible_rebuild_context())
        return sorted(
            eid for eid in eligible
            if entity_capability_tags(eid, current.get(eid) or {}) & {"occupancy", "activity"}
        )

    def _refresh_agent_history(self, agent, start_ts, end_ts, rebuild=False):
        """Explicit jobs backfill only what they need from Recorder.

        Resume is cheap: target + already selected feature entities from the saved cursor.
        Rebuild is intentionally broader so a newly added sensor can enter feature selection.
        """
        if end_ts <= start_ts:
            return
        target = agent["target_entity"]
        if rebuild:
            context_ids = [eid for eid in self._eligible_rebuild_context() if eid != target]
        else:
            model = STORE.get_model(agent["id"]) or {}
            context_ids = [eid for eid in ((model.get("schema") or {}).get("entities") or []) if eid != target]
        try:
            self._import_section(
                [target], start_ts, end_ts, batch_size=1, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=self.progress, progress_hi=self.progress,
                label=f"Agent {agent['id']} · target history", max_hours=6, parallel_requests=1,
                inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
            )
            if context_ids:
                with self.engine.lock:
                    live_states = dict(self.engine.state_map)
                fast_ids = [eid for eid in context_ids if entity_capability_tags(eid, live_states.get(eid) or {}) & {"occupancy", "activity"}]
                regular_ids = [eid for eid in context_ids if eid not in set(fast_ids)]
                if fast_ids:
                    self._import_section(
                        fast_ids, start_ts, end_ts, batch_size=30, minimal=True, no_attributes=True,
                        source="ha_history_fast_context", progress_lo=self.progress, progress_hi=self.progress,
                        label=f"Agent {agent['id']} · high-resolution behavioural context", max_hours=6, parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
                if regular_ids:
                    self._import_section(
                        regular_ids, start_ts, end_ts, batch_size=50, minimal=True, no_attributes=True,
                        source="ha_history_minimal", progress_lo=self.progress, progress_hi=self.progress,
                        label=f"Agent {agent['id']} · context history", max_hours=12, parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
            self.refresh_archive_cache()
        except Exception as exc:
            # A Recorder backfill failure must not destroy resumability. Train from the
            # locally available archive and leave a diagnostic event.
            STORE.event(agent["id"], "warning", "agent_history_refresh_partial", str(exc), None)

    def _run_agent_indexing(self, agent_id, rebuild=False):
        agent = STORE.get_agent(agent_id)
        if not agent:
            return
        archive_start, archive_end = self._training_bounds()
        start_ts = archive_start if rebuild or agent.get("training_window_start_ts") is None else float(agent["training_window_start_ts"])
        cursor = start_ts if rebuild or agent.get("training_cursor_ts") is None else max(start_ts, float(agent["training_cursor_ts"]))
        # Explicit Resume/Rebuild reaches current Recorder time, not merely the previous
        # local archive maximum. Resume backfills only selected features; Rebuild scans
        # the broader eligible sensor pool so newly added sensors can be discovered.
        target_end = max(now_ts(), archive_end, float(agent.get("training_window_end_ts") or archive_end))
        refresh_start = start_ts if rebuild else max(start_ts, cursor - max(60.0, float(OPTIONS.get("agent_training_overlap_hours", 12)) * 3600.0))
        self._refresh_agent_history(agent, refresh_start, target_end, rebuild=rebuild)
        # Recorder refresh may extend the archive, but the requested pass still ends at
        # the captured current time for deterministic progress.
        STORE.set_training_progress(agent_id, start_ts, cursor, target_end)
        STORE.event(agent_id, "info", "agent_rebuild_started" if rebuild else "agent_resume_started",
                    "Full historical rebuild started from archive beginning" if rebuild else
                    "Training resumed from saved historical cursor",
                    {"start_ts": start_ts, "cursor_ts": cursor, "end_ts": target_end})

        if cursor >= target_end - 0.5:
            # Nothing new to index. Preserve PAUSED unless a prior completed score already passes.
            score = float(agent.get("benchmark_score") or 0.0)
            threshold = float(OPTIONS.get("candidate_benchmark_threshold", 0.78))
            state = "qualified" if score > threshold else "paused"
            STORE.set_training_state(agent_id, state, score=agent.get("benchmark_score"),
                                     samples=agent.get("benchmark_samples") or 0, source=agent.get("benchmark_source"),
                                     detail=agent.get("benchmark_detail") or {})
            STORE.set_training_progress(agent_id, start_ts, target_end, target_end)
            return

        chunk_s = max(6.0, float(OPTIONS.get("agent_training_chunk_hours", 48))) * 3600.0
        overlap_s = max(0.0, min(chunk_s * 0.5, float(OPTIONS.get("agent_training_overlap_hours", 12)) * 3600.0))
        while cursor < target_end - 0.5 and not self.stop_event.is_set():
            chunk_end = min(target_end, cursor + chunk_s)
            chunk_start = max(start_ts, cursor - overlap_s) if cursor > start_ts else start_ts
            final = chunk_end >= target_end - 0.5
            self.train_from_archive(
                chunk_start, chunk_end, qualify=final, agent_ids={agent_id}, include_candidates=True,
                benchmark=True, accumulate_benchmark=True,
            )
            cursor = chunk_end
            STORE.set_training_progress(agent_id, start_ts, cursor, target_end)
            STORE.event(agent_id, "info", "agent_index_checkpoint",
                        f"Historical indexing checkpoint {((cursor-start_ts)/max(1.0,target_end-start_ts)):.0%}",
                        {"cursor_ts": cursor, "end_ts": target_end, "final": final})
            if not final:
                self.stop_event.wait(max(0.0, float(OPTIONS.get("history_background_pause_ms", 250))) / 1000.0)

    def request_agent_rebuild(self, agent_id):
        return self._start_agent_job(agent_id, rebuild=True)

    def request_agent_resume(self, agent_id):
        agent = STORE.get_agent(agent_id)
        if not agent or agent.get("training_state") not in ("paused", "training"):
            return False
        return self._start_agent_job(agent_id, rebuild=False)

    def resume_incomplete_jobs(self):
        # A restart during a rebuild must not lose progress. Jobs with a persisted cursor
        # are resumed automatically; brand-new candidates are handled by the normal full pass.
        for aid in STORE.training_agent_ids(unstarted_only=False):
            a = STORE.get_agent(aid)
            if a and a.get("training_cursor_ts") is not None:
                self._start_agent_job(aid, rebuild=False)

    def start_cycle(self):
        with self.lock:
            self.cycle_started_at = now_ts()
            self.phase_started_at = self.cycle_started_at
            self.eta_seconds = None
            self.stage_eta_seconds = None
            self.progress_rate_per_min = None
            self._progress_samples = []
            self.chunk_done = 0
            self.chunk_total = 0

    def refresh_archive_cache(self):
        # Called by the history thread only, never synchronously from the UI.
        try:
            stats = STORE.archive_stats()
        except Exception:
            return
        with self.lock:
            self.archive_cache = stats

    def set_status(self, phase=None, progress=None, message=None, chunk_done=None, chunk_total=None,
                   stage_eta_seconds=None, work_done=None, work_total=None, work_unit=None,
                   eta_source=None, phase_detail=None):
        now = now_ts()
        with self.lock:
            if phase is not None and phase != self.phase:
                self.phase = phase
                self.phase_started_at = now
                self.stage_eta_seconds = None
                self.eta_seconds = None
                self.progress_rate_per_min = None
                self._progress_samples = []
                self.chunk_done = 0
                self.chunk_total = 0
                self.work_done = 0
                self.work_total = 0
                self.work_unit = None
                self.eta_source = None
                self.phase_detail = None
            if progress is not None:
                p = clamp(float(progress), 0, 1)
                self.progress = p
                if self.cycle_started_at is not None:
                    self._progress_samples.append((now, p))
                    cutoff = now - 600
                    self._progress_samples = [x for x in self._progress_samples if x[0] >= cutoff][-60:]
                    base = None
                    for sample in self._progress_samples:
                        if now - sample[0] >= 12 and p - sample[1] >= 0.005:
                            base = sample
                            break
                    if base is not None:
                        dt = max(1.0, now - base[0])
                        dp = max(1e-6, p - base[1])
                        rate = dp / dt
                        eta = (1.0 - p) / rate if p < 0.999 else 0.0
                        eta = clamp(eta, 0.0, 24 * 3600.0)
                        self.eta_seconds = eta if self.eta_seconds is None else (0.72 * self.eta_seconds + 0.28 * eta)
                        self.progress_rate_per_min = rate * 60.0
            if message is not None:
                self.message = message
            if chunk_done is not None:
                self.chunk_done = int(chunk_done)
            if chunk_total is not None:
                self.chunk_total = int(chunk_total)
            if stage_eta_seconds is not None:
                self.stage_eta_seconds = max(0.0, float(stage_eta_seconds))
            if work_done is not None:
                self.work_done = max(0, int(work_done))
            if work_total is not None:
                self.work_total = max(0, int(work_total))
            if work_unit is not None:
                self.work_unit = str(work_unit)
            if eta_source is not None:
                self.eta_source = str(eta_source)
            if phase_detail is not None:
                self.phase_detail = str(phase_detail)

    def run(self):
        while not self.stop_event.is_set():
            try:
                if not self.engine.state_map:
                    self.stop_event.wait(2)
                    continue
                self.bootstrap_and_train()
                if not bool(OPTIONS.get("manual_agent_training", True)):
                    self.resume_incomplete_jobs()
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.set_status("error", message=self.error)
                STORE.event(None, "error", "history_manager_error", self.error, {"trace": traceback.format_exc(limit=6)})
            mins = max(5, int(OPTIONS["history_maintenance_minutes"]))
            self.stop_event.wait(mins * 60)

    def _archive_history_payload(self, data, source):
        rows = []
        numeric_min_interval = max(5.0, float(OPTIONS.get("history_context_import_interval_seconds", 60)))
        for group in data or []:
            if not group:
                continue
            group_entity = group[0].get("entity_id")
            last_kept_ts = None
            last_item = None
            for idx, item in enumerate(group):
                eid = item.get("entity_id") or group_entity
                ts = parse_ts(item.get("last_changed") or item.get("last_updated"))
                if not eid or not ts:
                    continue
                keep = True
                # Whole-home context can contain very chatty numeric sensors. For the initial
                # historical archive we keep categorical transitions at full resolution, while
                # numeric context is sampled at a bounded interval. All live entities are still
                # available to the policy immediately, and future changes continue to be archived.
                if source == "ha_history_minimal":
                    raw_state = item.get("state")
                    try:
                        float(raw_state)
                        is_numeric = True
                    except (TypeError, ValueError):
                        is_numeric = False
                    if is_numeric and last_kept_ts is not None and ts - last_kept_ts < numeric_min_interval:
                        keep = False
                if keep:
                    rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                                 (item.get("context") or {}).get("user_id"), source))
                    last_kept_ts = ts
                last_item = (eid, ts, item)
            # Preserve the final numeric state of a group even when it fell inside the sampling window.
            if source == "ha_history_minimal" and last_item:
                eid, ts, item = last_item
                if last_kept_ts != ts:
                    rows.append((eid, ts, item.get("state"), item.get("attributes") or {},
                                 (item.get("context") or {}).get("user_id"), source))
        return STORE.archive_batch(rows)

    def _fetch_history_resilient(self, entity_ids, start_ts, end_ts, *, minimal, no_attributes, source, depth=0):
        """Fetch Recorder history without allowing one large query to kill bootstrap.

        A Raspberry Pi can legitimately need >45 s for a 10-day multi-entity Recorder query.
        We therefore request short windows and, on timeout/server errors, automatically split
        first by entity list and then by time. Successful pieces are committed immediately,
        so a restart resumes safely thanks to the archive UNIQUE(entity_id, ts) constraint.
        """
        if not entity_ids or end_ts <= start_ts:
            return 0
        try:
            data = HA.history(
                entity_ids, iso_from_ts(start_ts), iso_from_ts(end_ts),
                minimal=minimal, no_attributes=no_attributes,
                # Controllable-device history needs attribute-only changes too (e.g.
                # brightness 30% → 50% while light.state stays "on"). Using
                # significant_changes_only here hid exactly the activity required for
                # auto-discovery in v0.3.3. Whole-home context remains significant-only.
                significant=(source != "ha_history_full"), timeout=20,
            ) or []
            return self._archive_history_payload(data, source)
        except HTTPError as exc:
            # Authentication / malformed-request errors will not improve by splitting.
            if getattr(exc, "code", 500) in (400, 401, 403, 404):
                raise
            err = exc
        except (URLError, TimeoutError, OSError) as exc:
            err = exc

        span = end_ts - start_ts
        # Split the entity set first. This is normally enough for busy HA databases.
        if len(entity_ids) > 1:
            mid = max(1, len(entity_ids) // 2)
            if depth <= 8:
                return (
                    self._fetch_history_resilient(entity_ids[:mid], start_ts, end_ts,
                                                   minimal=minimal, no_attributes=no_attributes,
                                                   source=source, depth=depth + 1)
                    + self._fetch_history_resilient(entity_ids[mid:], start_ts, end_ts,
                                                     minimal=minimal, no_attributes=no_attributes,
                                                     source=source, depth=depth + 1)
                )
        # A single very chatty entity can still be expensive; reduce its time window.
        if span > 1800 and depth <= 14:
            mid_ts = start_ts + span / 2.0
            return (
                self._fetch_history_resilient(entity_ids, start_ts, mid_ts,
                                               minimal=minimal, no_attributes=no_attributes,
                                               source=source, depth=depth + 1)
                + self._fetch_history_resilient(entity_ids, mid_ts, end_ts,
                                                 minimal=minimal, no_attributes=no_attributes,
                                                 source=source, depth=depth + 1)
            )

        # Do not abort the whole import for one pathological half-hour slice.
        label = ",".join(entity_ids[:2]) + ("…" if len(entity_ids) > 2 else "")
        msg = f"Skipped one Recorder slice after repeated timeouts: {label} ({type(err).__name__}: {err})"
        print(f"[history] {msg}", flush=True)
        STORE.event(None, "warning", "history_slice_skipped", msg, {
            "entities": entity_ids, "start": iso_from_ts(start_ts), "end": iso_from_ts(end_ts)
        })
        return 0

    @staticmethod
    def _time_windows(start_ts, end_ts, max_hours=24):
        step = max(1, int(max_hours)) * 3600.0
        windows = []
        cursor = float(start_ts)
        while cursor < end_ts:
            nxt = min(cursor + step, end_ts)
            windows.append((cursor, nxt))
            cursor = nxt
        # Newest first: useful recent behavior reaches the local archive first.
        windows.reverse()
        return windows

    def _import_section(self, entity_ids, start_ts, end_ts, *, batch_size, minimal, no_attributes, source,
                        progress_lo, progress_hi, label, max_hours=24, parallel_requests=None, inter_chunk_pause_ms=0,
                        on_chunk=None):
        if not entity_ids or end_ts <= start_ts:
            return 0
        windows = self._time_windows(start_ts, end_ts, max_hours=max_hours)
        batches = [entity_ids[i:i + batch_size] for i in range(0, len(entity_ids), batch_size)]
        tasks = [(ws, we, batch, wi, bi)
                 for wi, (ws, we) in enumerate(windows, start=1)
                 for bi, batch in enumerate(batches, start=1)]
        total = max(1, len(tasks))
        inserted = 0
        done = 0
        workers = int(parallel_requests if parallel_requests is not None else OPTIONS.get("history_parallel_requests", 2))
        workers = max(1, min(workers, 3, total))

        def fetch(task):
            ws, we, batch, wi, bi = task
            count = self._fetch_history_resilient(
                batch, ws, we, minimal=minimal, no_attributes=no_attributes, source=source
            )
            return count, wi, bi, batch

        if workers == 1:
            iterator = map(fetch, tasks)
            pool = None
        else:
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ha-history")
            futures = [pool.submit(fetch, task) for task in tasks]
            iterator = (f.result() for f in as_completed(futures))

        stage_started = now_ts()
        try:
            for count, wi, bi, batch in iterator:
                if self.stop_event.is_set():
                    break
                inserted += count
                done += 1
                frac = done / total
                elapsed = max(0.001, now_ts() - stage_started)
                stage_eta = (elapsed / done) * (total - done) if done else None
                self.set_status(
                    progress=progress_lo + (progress_hi - progress_lo) * frac,
                    message=(f"{label}: chunk {done}/{total} · "
                             f"window {wi}/{len(windows)} · batch {bi}/{len(batches)}"),
                    chunk_done=done, chunk_total=total, stage_eta_seconds=stage_eta,
                    work_done=done, work_total=total, work_unit="Recorder chunks",
                    eta_source="measured chunk throughput",
                    phase_detail=f"Recorder: {done}/{total} chunks complete",
                )
                if on_chunk is not None:
                    try:
                        on_chunk(batch)
                    except Exception as exc:
                        print(f"[history] on_chunk callback failed: {exc}", flush=True)
                if workers == 1 and inter_chunk_pause_ms and not self.stop_event.is_set():
                    # Yield CPU/IO to Home Assistant between background Recorder chunks.
                    # This deliberately trades background completion time for a responsive HA UI.
                    self.stop_event.wait(max(0.0, float(inter_chunk_pause_ms)) / 1000.0)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        return inserted

    def _manual_lightweight_cycle(self, current, controllable, end_ts):
        start_ts = end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400.0
        last = parse_ts(STORE.meta_get("manual_discovery_refresh"))
        if last:
            refresh_start = max(end_ts - 2 * 3600.0, float(last) - 300.0)
        else:
            refresh_start = max(start_ts, end_ts - max(6.0, float(OPTIONS.get("manual_discovery_hours", 24))) * 3600.0)
        self.set_status(
            "manual_ready", 0.10,
            "Low-memory discovery: refreshing controllable-device history only",
            phase_detail="Agent training is manual; whole-home context stays idle",
        )
        if controllable and end_ts > refresh_start:
            self._import_section(
                controllable, refresh_start, end_ts, batch_size=8, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=0.10, progress_hi=0.65,
                label="Lightweight target discovery", max_hours=3, parallel_requests=1,
                inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 500)),
            )
        self.refresh_archive_cache()
        created = self.auto_discover_agents(current, start_ts)
        self.auto_created += created
        # Populate diagnostics from current state only; this does not import context history.
        self._eligible_rebuild_context()
        STORE.meta_set("manual_discovery_refresh", iso_from_ts(end_ts))
        STORE.meta_set("training_revision", TRAINING_REVISION)
        self.last_run = now_ts()
        q = len(STORE.qualified_agents())
        waiting = len([a for a in STORE.list_agents() if a.get("enabled") and a.get("training_state") == "paused"])
        self.set_status(
            "ready", 1.0,
            f"Low-memory mode ready · {q} trained / {waiting} waiting for manual training",
            stage_eta_seconds=0, work_done=0, work_total=0, work_unit="agents",
            eta_source="idle", phase_detail="Press Train on one agent; only one training job can run at a time",
        )

    def bootstrap_and_train(self):
        with self.engine.lock:
            current = dict(self.engine.state_map)
        if not current:
            return

        # Device-level actuator exclusion needs Entity Registry device_id metadata. On a
        # healthy realtime connection this normally arrives immediately; give it a brief
        # head start before rebuilding the v0.7.4 policy schemas. Direct controllable
        # entities are filtered even if registry metadata is unavailable.
        if self.engine.ws_connected:
            deadline = time.monotonic() + 5.0
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                with self.engine.lock:
                    if self.engine.entity_registry:
                        break
                self.stop_event.wait(0.10)

        self.start_cycle()
        end_ts = now_ts()
        # Prime live temporal deltas/trends from the local archive so a restart does not
        # need to wait minutes before temporal features become meaningful.
        self.engine.prime_temporal_from_archive(end_ts - max(3600, int(OPTIONS.get("temporal_long_seconds", 300)) * 3), end_ts)
        bootstrap_done = (
            STORE.meta_get("history_bootstrap_complete", "0") == "1"
            and STORE.meta_get("history_bootstrap_revision", "") == HISTORY_BOOTSTRAP_REVISION
        )
        entity_ids = sorted(current)
        controllable = [eid for eid, st in current.items() if target_options_for_state(st)]
        self.discovered_controllable = len(controllable)

        manual_training = bool(OPTIONS.get("manual_agent_training", True))
        training_rebuild = STORE.meta_get("training_revision", "") != TRAINING_REVISION
        if training_rebuild and not manual_training:
            STORE.clear_historical_models()
            self.engine.models.clear()

        # Read existing automations as a feature prior. This never creates rewards or
        # labels; it only identifies upstream trigger/condition entities likely to matter.
        if OPTIONS.get("automation_scan_enabled", True):
            last_scan = AUTOMATION_KNOWLEDGE.status().get("last_scan") or 0
            if training_rebuild or now_ts() - float(last_scan) > 900:
                self.set_status("automation_scan", 0.005, "Reading Home Assistant automations for predictive context hints")
                with self.engine.lock:
                    registry = dict(self.engine.entity_registry)
                AUTOMATION_KNOWLEDGE.scan(current, registry)

        if manual_training:
            self._manual_lightweight_cycle(current, controllable, end_ts)
            return

        if bootstrap_done:
            discovery_start = end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400

            # v0.7.12 broadens the candidate universe to every parseable HA entity except
            # controllable-device inputs and explicit electrical-unit telemetry. Refresh
            # the broad candidate history once on this feature revision so phone/car/
            # weather/virtual/camera-score entities added after the original bootstrap can
            # actually compete on historical relevance. Steady-state Recorder maintenance
            # remains narrow after qualification, so this is a one-time rebuild cost.
            if training_rebuild:
                candidate_ids = self._eligible_rebuild_context()
                if candidate_ids:
                    self.set_status("context_refresh", 0.02,
                                    f"Refreshing {len(candidate_ids)} all-entity context candidates for historical screening")
                    self._import_section(
                        candidate_ids, discovery_start, end_ts, batch_size=50, minimal=True, no_attributes=True,
                        source="ha_history_candidate_context", progress_lo=0.02, progress_hi=0.12,
                        label="Refreshing all eligible context candidates", max_hours=12, parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
                    self.refresh_archive_cache()
                self.set_status("rebuilding", 0.13, "Rebuilding predictive policies from broad historical context")
                self.auto_created = self.auto_discover_agents(current, discovery_start)
                self.trained_new = self.train_from_archive(discovery_start, end_ts, qualify=True, include_candidates=True,
                                                                  progress_lo=0.13, progress_hi=0.55,
                                                                  progress_label="Rebuilding predictive policies")
                self.set_status("ready_enriching", 0.55,
                                "Predictive Shadow policies ready · refreshing only recent Recorder data in background")

            # Maintenance remains small and incremental. Keep it deliberately gentle.
            last = parse_ts(STORE.meta_get("last_history_maintenance"))
            start_ts = max(end_ts - 6 * 3600, (last - 300) if last else end_ts - 6 * 3600)
            self.set_status("importing", 0.58 if training_rebuild else 0.01, "Updating recent local history archive")
            self._import_section(
                controllable, start_ts, end_ts, batch_size=12, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=0.59 if training_rebuild else 0.02, progress_hi=0.72 if training_rebuild else 0.34,
                label="Updating controllable-device history", max_hours=3,
            )
            self.refresh_archive_cache()
            maintenance_context = self.qualified_context_entities() if STORE.meta_get("candidate_qualification_complete", "0") == "1" else entity_ids
            if maintenance_context:
                self._import_section(
                    maintenance_context, start_ts, end_ts, batch_size=50, minimal=True, no_attributes=True,
                    source="ha_history_minimal", progress_lo=0.72 if training_rebuild else 0.34, progress_hi=0.86 if training_rebuild else 0.72,
                    label="Updating qualified-policy context", max_hours=6, parallel_requests=1,
                    inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                )
                self.refresh_archive_cache()
            self.set_status("discovering", 0.88 if training_rebuild else 0.74, "Refreshing active-device discovery")
            self.auto_created += self.auto_discover_agents(current, discovery_start)
            pending_candidates = STORE.candidate_agent_ids()
            if pending_candidates:
                self.set_status("benchmarking", 0.91 if training_rebuild else 0.78,
                                f"Starting full-history indexing for {len(pending_candidates)} new training agent(s)")
                for aid in pending_candidates:
                    self._start_agent_job(aid, rebuild=True)
            self.set_status("training", 0.93 if training_rebuild else 0.82, "Updating qualified policies from new logged rewards")
            self.trained_new += self.train_from_archive(discovery_start, end_ts)
        else:
            start_ts = end_ts - float(OPTIONS["history_bootstrap_days"]) * 86400
            total_hours = float(OPTIONS["history_bootstrap_days"]) * 24.0
            fast_context_hours = max(6.0, min(float(OPTIONS.get("history_fast_context_hours", 24)), total_hours))
            fast_target_hours = max(6.0, min(float(OPTIONS.get("history_fast_target_hours", 24)), total_hours))
            fast_context_start = max(start_ts, end_ts - fast_context_hours * 3600.0)
            fast_target_start = max(start_ts, end_ts - fast_target_hours * 3600.0)

            # Stage 1a: only the newest target history. This is enough to create agents
            # for devices that are clearly in daily use, usually within the first minute
            # or two, rather than waiting for all ten days to finish.
            self.set_status(
                "fast_targets", 0.01,
                f"Fast agent discovery: scanning the newest {fast_target_hours:.0f} h of controllable devices",
            )
            early_created = 0
            def early_discover(batch):
                nonlocal early_created
                subset = {eid: current[eid] for eid in batch if eid in current}
                if subset:
                    n = self.auto_discover_agents(subset, fast_target_start, threshold_override=2, update_active=False)
                    if n:
                        early_created += n
                        self.auto_created += n
            self._import_section(
                controllable, fast_target_start, end_ts, batch_size=12, minimal=False, no_attributes=False,
                source="ha_history_full", progress_lo=0.02, progress_hi=0.14,
                label="Fast discovery · recent controllable-device history", max_hours=6,
                on_chunk=early_discover,
            )
            self.refresh_archive_cache()

            self.set_status("discovering", 0.15, "Creating the first Shadow agents from recent activity")
            created = early_created + self.auto_discover_agents(current, fast_target_start)
            # If the configured activity threshold yields nothing, use a conservative
            # two-change fallback for the fast pass only. This still requires recent
            # observed use, and every agent starts in Shadow.
            if not STORE.list_agents() and created == 0:
                created += self.auto_discover_agents(current, fast_target_start, threshold_override=2)
            self.auto_created = created
            STORE.event(None, "info", "fast_agent_discovery",
                        f"Fast discovery created {created} Shadow agent(s)",
                        {"agents_created": created, "target_hours": fast_target_hours})

            # Stage 1b: recent whole-home context, just enough to build a useful initial
            # policy for the agents already visible in the UI.
            self.set_status(
                "fast_context", 0.17,
                f"Agents discovered · importing the newest {fast_context_hours:.0f} h of whole-home context",
            )
            self._import_section(
                entity_ids, fast_context_start, end_ts, batch_size=50, minimal=True, no_attributes=True,
                source="ha_history_minimal", progress_lo=0.17, progress_hi=0.29,
                label="Fast start · recent whole-home context", max_hours=12,
            )
            self.refresh_archive_cache()

            self.set_status("fast_training", 0.30, "Training first offline-RL policies; Shadow agents are already visible")
            first_count = self.train_from_archive(max(fast_target_start, fast_context_start), end_ts)
            self.trained_new = first_count
            STORE.event(None, "info", "fast_start_ready",
                        "Fast-start policies are ready in Shadow; older target/context history continues in background",
                        {"agents_created": created, "experiences": first_count,
                         "context_hours": fast_context_hours, "target_hours": fast_target_hours})

            # Stage 2: older controllable-device history. This can discover devices used
            # less often than daily. It runs at low concurrency after first agents exist.
            if fast_target_start > start_ts and not self.stop_event.is_set():
                delay = max(0, int(OPTIONS.get("history_background_start_delay_seconds", 10)))
                self.set_status("ready_enriching", 0.32,
                                f"First agents ready · full index continues in {delay}s at low priority")
                if delay:
                    self.stop_event.wait(delay)
                if not self.stop_event.is_set():
                    self.set_status("enriching_targets", 0.33,
                                    "Shadow agents ready · indexing older controllable-device history")
                    self._import_section(
                        controllable, start_ts, fast_target_start, batch_size=10, minimal=False, no_attributes=False,
                        source="ha_history_full", progress_lo=0.33, progress_hi=0.53,
                        label="Background · older controllable-device history", max_hours=12,
                        parallel_requests=1,
                        inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                    )
                    self.refresh_archive_cache()
                    more_created = self.auto_discover_agents(current, start_ts)
                    self.auto_created += more_created
                    if more_created:
                        STORE.event(None, "info", "additional_agents_discovered",
                                    f"Discovered {more_created} additional Shadow agent(s) from older use", None)

            # Stage 3: fill the remaining whole-home context. Agents remain available.
            if fast_context_start > start_ts and not self.stop_event.is_set():
                self.set_status("enriching_context", 0.54,
                                "Shadow agents ready · low-impact full-context indexing continues")
                self._import_section(
                    entity_ids, start_ts, fast_context_start, batch_size=50, minimal=True, no_attributes=True,
                    source="ha_history_minimal", progress_lo=0.54, progress_hi=0.90,
                    label="Background · older whole-home context", max_hours=24,
                    parallel_requests=1,
                    inter_chunk_pause_ms=int(OPTIONS.get("history_background_pause_ms", 250)),
                )
                self.refresh_archive_cache()

            self.set_status("benchmarking", 0.92, "Final full-history behaviour benchmark and candidate qualification")
            self.trained_new += self.train_from_archive(start_ts, end_ts, qualify=True, include_candidates=True,
                                                               progress_lo=0.92, progress_hi=0.995,
                                                               progress_label="Final behaviour benchmark")

        STORE.purge_archive(float(OPTIONS["archive_retention_days"]))
        self.refresh_archive_cache()
        self.last_run = now_ts()
        STORE.meta_set("last_history_maintenance", iso_from_ts(self.last_run))
        if not bootstrap_done:
            STORE.meta_set("history_bootstrap_complete", "1")
            STORE.meta_set("history_bootstrap_revision", HISTORY_BOOTSTRAP_REVISION)
        if training_rebuild:
            # Live feedback rows are retained for audit, but v0.4 feature vectors used a
            # different representation. Replaying them into v0.5 would corrupt the new
            # explicit schema, so the policy rebuild uses the long-term raw HA archive.
            STORE.event(None, "info", "feedback_migration",
                        "Historical policies rebuilt with v0.5 features; older live feedback retained for audit, not replayed", None)
        STORE.meta_set("training_revision", TRAINING_REVISION)
        q = len([a for a in STORE.list_agents() if a.get("training_state") == "qualified"])
        d = len([a for a in STORE.list_agents() if a.get("training_state") == "paused"])
        self.set_status("ready", 1.0,
                        f"Historical benchmark complete · {q} qualified / {d} paused agent(s)",
                        chunk_done=0, chunk_total=0, stage_eta_seconds=0)
    def replay_live_feedback_all(self):
        # Deliberately disabled across feature-schema revisions. Raw entity_history is the
        # canonical source for deterministic policy rebuilds.
        return 0

    def usage_for(self, entity_id, prop, start_ts):
        rows = STORE.archive_rows(start_ts=start_ts, entity_id=entity_id)
        values = []
        last = None
        for r in rows:
            v = target_value(archived_state(r), prop)
            if v is None:
                continue
            if last is None or abs(float(v) - float(last)) > 1e-6:
                values.append((r, float(v)))
                last = float(v)
        return values

    def auto_discover_agents(self, current, start_ts, threshold_override=None, update_active=True):
        threshold = int(threshold_override if threshold_override is not None else OPTIONS["auto_agent_min_changes"])
        recent_days = max(float(OPTIONS["auto_agent_recent_days"]), min(30.0, float(OPTIONS["history_bootstrap_days"])))
        recent_cutoff = now_ts() - recent_days * 86400
        active = 0
        eligible = 0
        filtered_config = 0
        inactive = 0
        created = 0
        max_agents = max(1, int(OPTIONS.get("max_auto_agents", 250)))
        # Remove only stale auto-created agents that the Entity Registry now identifies
        # as configuration/diagnostic/hidden/disabled. Manual agents are never touched.
        existing = STORE.list_agents()
        cleaned = 0
        for old_agent in list(existing):
            if not old_agent.get("auto_created"):
                continue
            reg = self.engine.registry_entry(old_agent["target_entity"]) or {}
            invalid = (
                reg.get("disabled_by") is not None
                or reg.get("hidden_by") is not None
                or reg.get("entity_category") in ("config", "diagnostic")
            )
            if invalid:
                STORE.delete_agent(old_agent["id"])
                self.engine.models.pop(old_agent["id"], None)
                self.engine.runtime.pop(old_agent["id"], None)
                cleaned += 1
        if cleaned:
            STORE.event(None, "info", "auto_agent_cleanup",
                        f"Removed {cleaned} stale auto-agent(s) for config/diagnostic/hidden entities",
                        {"removed": cleaned})
        existing = STORE.list_agents()
        # One primary policy per physical/logical controllable entity. Manual agents also
        # suppress auto-creation for that entity so discovery cannot create duplicates.
        existing_entities = {a["target_entity"] for a in existing}
        for entity_id, state in current.items():
            opts = target_options_for_state(state)
            if not opts:
                continue
            reg = self.engine.registry_entry(entity_id) or {}
            if reg.get("disabled_by") is not None or reg.get("hidden_by") is not None or reg.get("entity_category") in ("config", "diagnostic"):
                filtered_config += 1
                continue
            eligible += 1
            candidates = []
            for opt in opts:
                changes = self.usage_for(entity_id, opt["property"], start_ts)
                last_ts = changes[-1][0]["ts"] if changes else 0
                score = max(0, len(changes) - 1)
                candidates.append((score, last_ts, opt, changes))
            # Existing automations that act on this target are evidence that a device is
            # intentionally controlled, so one observed historical transition is enough
            # to include it in Shadow. It is still trained only from rewards.
            _, automation_infos = AUTOMATION_KNOWLEDGE.hints_for_target(entity_id)
            effective_threshold = 1 if automation_infos else threshold
            candidates.sort(
                key=lambda x: (
                    x[0] >= effective_threshold and x[1] >= recent_cutoff,
                    x[2]["property"] not in ("power", "option_index"),
                    x[0],
                ),
                reverse=True,
            )
            score, last_ts, opt, _ = candidates[0]
            if score < effective_threshold or last_ts < recent_cutoff:
                inactive += 1
                continue
            active += 1
            if entity_id in existing_entities or len(existing_entities) >= max_agents:
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            payload = {
                "name": name,
                "mode": "paused",
                "target_entity": entity_id,
                "target_property": opt["property"],
                "min_value": opt["min"], "max_value": opt["max"],
                "deadband": opt["deadband"], "exploration_step": opt["exploration_step"],
                "confidence_threshold": 0.78, "action_interval": default_action_interval(entity_id, opt["property"]),
                "auto_created": True, "micro_exploration": False, "exploration_interval": 21600,
            }
            STORE.create_agent(payload)
            existing_entities.add(entity_id)
            created += 1
        if update_active:
            self.discovered_active = active
            self.discovery_eligible = eligible
            self.discovery_filtered_config = filtered_config
            self.discovery_inactive = inactive
        return created

    def train_from_archive(self, start_ts, end_ts, *, qualify=False, agent_ids=None, include_candidates=False, benchmark=None, accumulate_benchmark=False, progress_lo=None, progress_hi=None, progress_label=None):
        benchmark = bool(qualify) if benchmark is None else bool(benchmark)
        agents = [a for a in STORE.list_agents() if a["enabled"]]
        if agent_ids is not None:
            wanted = set(agent_ids)
            agents = [a for a in agents if a["id"] in wanted]
        elif STORE.meta_get("candidate_qualification_complete", "0") == "1" and not qualify and not include_candidates:
            # Once the full-history benchmark has run, only proven agents consume CPU
            # in periodic offline training. PAUSED agents wait for explicit Resume.
            agents = [a for a in agents if a.get("training_state") == "qualified"]
        if not agents:
            return 0
        target_map = {}
        for a in agents:
            target_map.setdefault(a["target_entity"], []).append(a)
        archive_row_count = STORE.archive_count(start_ts=start_ts, end_ts=end_ts)
        if archive_row_count <= 0:
            return 0
        progress_enabled = progress_lo is not None and progress_hi is not None and float(progress_hi) > float(progress_lo)
        progress_label = progress_label or "Historical policy rebuild"
        if progress_enabled:
            span = float(progress_hi) - float(progress_lo)
            self.set_status(progress=float(progress_lo), message=f"{progress_label}: screening context candidates",
                            work_done=0, work_total=archive_row_count, work_unit="history rows",
                            eta_source="measured replay throughput",
                            phase_detail="Finding causal precursors and behavioural drivers")

        # Historical precursor relevance: every usable HA entity is considered. Entities
        # that repeatedly change shortly before a real target action receive a structural
        # boost in that agent's explicit context schema. This is feature selection only,
        # never a reward or supervised label.
        recent_change = {}
        activity_counts = {}
        relevance_raw = {a["id"]: {} for a in agents}
        target_action_counts = {a["id"]: 0 for a in agents}
        last_target_value = {}
        precursor_window = max(300.0, float(OPTIONS.get("temporal_long_seconds", 300)) * 2.0)
        archive_span = max(1.0, float(end_ts) - float(start_ts))
        selection_end = float(end_ts) - max(1800.0, archive_span * float(OPTIONS.get("confidence_validation_fraction", .2)))

        # Build a small edge index for fast-light causal driver discovery.  This does not
        # depend on HA area metadata or direct automation target mapping: every eligible
        # occupancy/presence or radar/AI activity sensor is compared with the real ON/OFF transitions of each
        # fast target.  Only the pre-validation slice participates in feature selection.
        with self.engine.lock:
            discovery_states = dict(self.engine.state_map)
            discovery_registry = dict(self.engine.entity_registry)
        excluded_control, _ = controllable_context_exclusions(discovery_states, discovery_registry)
        excluded_electrical, _ = electrical_context_exclusions(discovery_states, discovery_registry)
        discovery_excluded = excluded_control | excluded_electrical
        fast_agents = [a for a in agents if is_fast_reactive_agent(a)]
        behaviour_candidates = {
            eid for eid, st in discovery_states.items()
            if is_context_candidate_entity(eid, st, discovery_excluded)
            and (entity_capability_tags(eid, st) & {"occupancy", "activity"})
        }
        fast_targets = {a["target_entity"] for a in fast_agents}
        fast_edge_rows = {eid: [] for eid in (behaviour_candidates | fast_targets)}

        previous_context = {}
        for row in STORE.archive_iter(start_ts=start_ts, end_ts=end_ts, chunk_size=2000):
            ts = float(row["ts"]); eid = row["entity_id"]
            if ts >= selection_end:
                break
            if eid in fast_edge_rows:
                fast_edge_rows[eid].append(row)
            signature = (row.get("state"), row.get("attributes_json"))
            if previous_context.get(eid) == signature:
                continue
            previous_context[eid] = signature
            activity_counts[eid] = activity_counts.get(eid, 0) + 1
            for agent in target_map.get(eid, []):
                st = archived_state(row)
                val = target_value(st, agent["target_property"])
                if val is None:
                    continue
                prev = last_target_value.get(agent["id"])
                changed = prev is None or abs(float(val) - float(prev)) > max(0.01, float(agent["deadband"]) * 0.05)
                if changed:
                    target_action_counts[agent["id"]] += 1
                    scores = relevance_raw[agent["id"]]
                    for ceid, cts in recent_change.items():
                        if ceid == eid:
                            continue
                        age = ts - cts
                        if is_fast_reactive_agent(agent):
                            agent_window = float(OPTIONS.get("fast_precursor_on_seconds", 8) if float(val) >= 0.5 else OPTIONS.get("fast_precursor_off_seconds", 120))
                        else:
                            agent_window = precursor_window
                        if 0.0 <= age <= agent_window:
                            # Fast lights use a much sharper precursor kernel: a kitchen
                            # sensor one minute old should not outrank the dedicated stair
                            # sensor that just changed. Slow plants keep the broad window.
                            tau = float(OPTIONS.get("fast_recent_change_seconds", 3)) if is_fast_reactive_agent(agent) else max(30.0, precursor_window / 2.0)
                            scores[ceid] = scores.get(ceid, 0.0) + math.exp(-age / max(0.5, tau))
                    last_target_value[agent["id"]] = float(val)
            recent_change[eid] = ts

        occupancy_edge_index = {
            eid: transition_edges(fast_edge_rows.get(eid) or [], occupancy_state_bool)
            for eid in behaviour_candidates
            if "occupancy" in entity_capability_tags(eid, discovery_states.get(eid) or {})
        }
        activity_candidates = {
            eid for eid in behaviour_candidates
            if "activity" in entity_capability_tags(eid, discovery_states.get(eid) or {})
        }
        fast_driver_scores = {}
        for agent in fast_agents:
            midpoint = (float(agent["min_value"]) + float(agent["max_value"])) / 2.0
            target_edges = transition_edges(
                fast_edge_rows.get(agent["target_entity"]) or [],
                lambda st, a=agent, mid=midpoint: (None if target_value(st, a["target_property"]) is None else target_value(st, a["target_property"]) >= mid),
            )
            scores = {}
            for eid, sensor_edges in occupancy_edge_index.items():
                score = balanced_presence_driver_score(sensor_edges, target_edges)
                if score > 0:
                    scores[eid] = score
            for eid in activity_candidates:
                score = numeric_activity_driver_score(fast_edge_rows.get(eid) or [], target_edges)
                if score > 0:
                    scores[eid] = max(scores.get(eid, 0.0), score)
            fast_driver_scores[agent["id"]] = scores

        for agent in agents:
            raw = relevance_raw.get(agent["id"], {})
            actions_n = max(1, int(target_action_counts.get(agent["id"], 0)))
            adjusted = {}
            for eid, hit_score in raw.items():
                # hit_strength is roughly the share of target actions preceded by this
                # entity, weighted towards close-in-time changes. expected_presence is the
                # chance of seeing at least one change in the precursor window solely from
                # its normal reporting rate (Poisson approximation). Their ratio is a
                # simple temporal lift estimate.
                hit_strength = clamp(float(hit_score) / actions_n, 0.0, 1.0)
                rate = float(activity_counts.get(eid, 0)) / archive_span
                # With exponentially distributed background updates and the same
                # exp(-age/tau) precursor kernel, E[kernel] = lambda/(lambda+1/tau).
                # This is much stricter than merely asking whether an update happened
                # somewhere inside the window and strongly suppresses high-rate sensors.
                tau = float(OPTIONS.get("fast_recent_change_seconds", 3)) if is_fast_reactive_agent(agent) else max(30.0, precursor_window / 2.0)
                expected_recency = clamp(rate / max(rate + 1.0 / max(0.5, tau), 1e-9), 0.0, 1.0)
                lift = hit_strength / max(0.03, expected_recency)
                specificity = clamp((lift - 1.0) / 2.0, 0.0, 1.0)
                adjusted[eid] = hit_strength * (0.10 + 0.90 * specificity)
            peak = max(adjusted.values()) if adjusted else 0.0
            normalized = ({k: v / peak for k, v in adjusted.items()} if peak > 0 else {})
            # Edge association is deliberately merged *after* generic normalization so a
            # true one-sensor behavioural rule can reach relevance ~=1 even when geography/name based
            # precursor heuristics missed it.  This fixes ESPHome LD24xx presence radars
            # used through group/device/script automations.
            for eid, driver_score in (fast_driver_scores.get(agent["id"]) or {}).items():
                normalized[eid] = max(float(normalized.get(eid, 0.0)), float(driver_score))
            self.engine.context_relevance[agent["id"]] = normalized
            # If this training revision is being rebuilt, force schema creation after the
            # precursor scores are known.
            if STORE.get_model(agent["id"]) is None:
                self.engine.models.pop(agent["id"], None)

        if progress_enabled:
            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20
            self.set_status(progress=screening_end, message=f"{progress_label}: context screening complete; replaying recorded behaviour",
                            work_done=0, work_total=archive_row_count, work_unit="history rows",
                            eta_source="measured replay throughput",
                            phase_detail=f"Screened context for {len(agents)} agent(s); starting chronological replay")
        policies = {a["id"]: self.engine.policy(a) for a in agents}
        automation_infos_by_agent = {
            a["id"]: list(AUTOMATION_KNOWLEDGE.hints_for_target(a["target_entity"])[1] or [])
            for a in agents
        }
        benchmark_stats = {}
        for a in agents:
            prior = ((a.get("benchmark_detail") or {}).get("counts") or {}) if accumulate_benchmark else {}
            benchmark_stats[a["id"]] = {
                "samples": int(prior.get("samples") or 0),
                "correct": int(prior.get("correct") or 0),
                "per_action": {str(k): {"samples": int(v.get("samples") or 0), "correct": int(v.get("correct") or 0)}
                               for k, v in (prior.get("per_action") or {}).items()},
                "automation_rules": max(int(prior.get("automation_rules") or 0), len(automation_infos_by_agent[a["id"]])),
                "origin_counts": {str(k): int(v or 0) for k, v in (prior.get("origin_counts") or {}).items()},
            }


        # One chronological replay cursor per prediction horizon. A 30 s head sees the
        # house exactly as it looked 30 s before the historical action; a 5 min HVAC head
        # sees the state 5 min earlier. All heads receive the same reward for the action.
        horizons = sorted({h for p in policies.values() for h in p.horizons})
        watched_entities = {eid for p in policies.values() for eid in p.schema.entities}
        replay_entities = set(watched_entities) | set(target_map.keys())
        rows = STORE.archive_rows_for_entities(start_ts, end_ts, replay_entities)
        if not rows:
            return 0
        timeline = HistoricalTemporalTracker(rows, watched_entities)
        trackers = {h: timeline for h in horizons}
        pending = {}
        last_value = {}
        new_count = 0
        heldout_updates = []
        validation_fraction = clamp(float(OPTIONS.get("confidence_validation_fraction", 0.20)), 0.05, 0.40)
        validation_span = max(1800.0, (float(end_ts) - float(start_ts)) * validation_fraction)
        validation_start = max(float(start_ts), float(end_ts) - validation_span)

        def _primary_occupancy_sensor(policy):
            meta = policy.selection_meta or {}
            return meta.get("primary_occupancy_sensor") or meta.get("primary_local_sensor") or next(iter(meta.get("primary_local_sensors") or []), None)

        def _fast_anchor(agent, policy, action_value, action_ts):
            if not is_fast_reactive_agent(agent):
                return float(action_ts), None
            positive = float(action_value) >= 0.5
            primary = _primary_occupancy_sensor(policy)
            local_ts = None
            if primary:
                window = float(OPTIONS.get("fast_precursor_on_seconds", 8) if positive else OPTIONS.get("fast_precursor_off_seconds", 120))
                local_ts = timeline.directional_transition_before(primary, action_ts, positive, window)
            upstream_ts = None
            if positive:
                for eid in (policy.selection_meta or {}).get("upstream_sensors") or []:
                    ts = timeline.directional_transition_before(eid, action_ts, True, float(OPTIONS.get("fast_precursor_on_seconds", 8)))
                    if ts is not None and (upstream_ts is None or ts > upstream_ts):
                        upstream_ts = ts
            return float(local_ts if local_ts is not None else action_ts), upstream_ts

        def _effective_fast_dwell_end(agent, policy, action_value, start_ts, end_ts):
            if not is_fast_reactive_agent(agent):
                return float(end_ts)
            primary = _primary_occupancy_sensor(policy)
            if not primary:
                return float(end_ts)
            # Stop reinforcing ON as soon as the dedicated local occupancy sensor becomes
            # vacant, and stop reinforcing OFF when it becomes occupied. This removes
            # inherited one-minute automation delays from desired-state learning.
            opposite_positive = float(action_value) < 0.5
            edge = timeline.first_directional_transition_after(primary, start_ts, end_ts, opposite_positive)
            return min(float(end_ts), float(edge)) if edge is not None else float(end_ts)

        def learn_or_validate(policy, horizon, action_idx, features, reward, sample_ts):
            head = policy.heads[int(horizon)]
            if float(sample_ts) >= validation_start:
                head.validate(action_idx, features, reward)
                heldout_updates.append((policy, int(horizon), int(action_idx), features, float(reward)))
            else:
                policy.update(horizon, action_idx, features, reward)

        def record_behavior_benchmark(agent, policy, old, reward):
            """Chronological held-out benchmark against the target's recorded behaviour.

            v0.7.7 required every scored transition to be attributable to a directly
            discovered Home Assistant automation. That can collapse a genuinely good
            light policy to 0% when the rule reaches the lamp through a group, device
            target, helper, script, or when Recorder no longer exposes enough provenance.

            The authoritative benchmark is therefore the *observed target behaviour*:
            what state the device actually entered under the historical context. Existing
            automations remain the strongest structural prior for feature selection and
            are counted for diagnostics, but they are no longer a gate for whether a
            held-out transition is scoreable. Explicit user changes are also legitimate
            preference evidence and are included rather than silently discarded.

            Binary targets use balanced per-action accuracy so an always-OFF policy still
            cannot qualify merely because OFF dominates wall-clock time.
            """
            if not benchmark or float(reward) <= 0.0:
                return
            if float(old.get("anchor_ts", old["ts"])) < validation_start:
                return
            h = min(old["features_by_horizon"])
            features = old["features_by_horizon"][h]
            head = policy.heads[int(h)]
            arms = head.evaluate(features)
            predicted = max(arms, key=lambda a: a["mean"])["index"]
            actual = int(old["action_idx"])
            predicted_value = float(policy.actions[predicted])
            actual_value = float(policy.actions[actual])
            if len(policy.actions) <= 2 or agent.get("target_property") == "power":
                correct = predicted == actual
            else:
                tolerance = max(float(agent.get("deadband") or 0.0), (float(agent["max_value"]) - float(agent["min_value"])) * 0.03)
                correct = abs(predicted_value - actual_value) <= tolerance
            stat = benchmark_stats[agent["id"]]
            stat["samples"] += 1
            stat["correct"] += 1 if correct else 0
            slot = stat["per_action"].setdefault(str(actual), {"samples": 0, "correct": 0})
            slot["samples"] += 1
            slot["correct"] += 1 if correct else 0
            infos = automation_infos_by_agent.get(agent["id"]) or []
            origin = "manual" if old.get("user_id") else ("automation_assisted" if infos else "anonymous_external")
            stat["origin_counts"][origin] = int(stat["origin_counts"].get(origin) or 0) + 1

        def replay_completed_dwell(agent, policy, old, end_time, next_user_id=None):
            """Train desired-state value, not merely the next transition.

            The transition onset remains useful for anticipation: context at t-H learns
            the state that becomes desirable at t. Stable dwells additionally contribute
            a few contexts from *inside* the dwell, so presence/lux/etc. while a light is
            ON reinforce ON rather than making ON look like a precursor to OFF.
            """
            nonlocal new_count
            effective_end = _effective_fast_dwell_end(agent, policy, old["action_value"], old["ts"], end_time)
            dwell = max(0.0, float(effective_end) - old["ts"])
            reward = historical_reward(agent, dwell, old.get("user_id"), next_user_id)
            primary_h = min(old["features_by_horizon"])
            inserted = STORE.add_historical_experience(
                agent["id"], old["history_id"], old["action_idx"], old["action_value"], reward,
                dwell, old["features_by_horizon"][primary_h], old.get("user_id")
            )
            if not inserted:
                return False

            # Score the held-out transition before it is folded into training. This is
            # the behavioural benchmark against the legacy HA automations.
            record_behavior_benchmark(agent, policy, old, reward)

            # 1) Onset samples. Context is taken at the action boundary. The runtime
            # is event-driven, so it can reach this same environmental context as soon
            # as a precursor state_changed event arrives, before a human acts.
            for h, features in old["features_by_horizon"].items():
                if old["ts"] < validation_start <= effective_end:
                    continue  # purge rewards whose outcomes cross the validation boundary
                learn_or_validate(policy, h, old["action_idx"], features, reward, old.get("anchor_ts", old["ts"]))

            # Optional upstream ON cue: neighbouring-room sensors may legitimately fire
            # a few seconds before the dedicated local sensor. Teach that cue weakly so
            # it can accelerate ON, but never let it define OFF/occupancy persistence.
            for h, features in (old.get("upstream_features_by_horizon") or {}).items():
                if old.get("upstream_anchor_ts", old["ts"]) >= validation_start:
                    heldout_updates.append((policy, int(h), old["action_idx"], features, float(reward) * 0.35))
                else:
                    policy.update(h, old["action_idx"], features, float(reward) * 0.35)

            # 2) Persistence samples: learn what should remain true while the state is
            # accepted. Limit to three samples/head so a six-hour dwell cannot dominate
            # the policy merely because it lasted longer. Only sample contexts that are
            # safely inside the dwell for that prediction horizon.
            settle = min(20.0, max(3.0, float(OPTIONS.get("temporal_short_seconds", 60)) * 0.10))
            for h in policy.horizons:
                h = int(h)
                earliest_target = old["ts"] + h + settle
                latest_target = float(effective_end) - settle
                if latest_target <= earliest_target:
                    continue
                span = latest_target - earliest_target
                target_times = [earliest_target]
                if span >= max(20.0, h * 0.5):
                    target_times.append(earliest_target + span * 0.5)
                if span >= max(60.0, h):
                    target_times.append(latest_target)
                tracker = trackers[h]
                seen = set()
                for target_time in target_times:
                    context_ts = target_time
                    key = int(context_ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    tracker.advance(context_ts)
                    features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=context_ts)
                    if target_time < validation_start <= effective_end:
                        continue
                    if target_time >= validation_start:
                        # Correlated samples within a dwell are training evidence,
                        # not independent validation trials. Validate onset only.
                        heldout_updates.append((policy, h, old["action_idx"], features, float(reward)))
                    else:
                        policy.update(h, old["action_idx"], features, reward)

            new_count += 1
            return True

        replay_started = now_ts()
        replay_last_report = replay_started
        replay_total = max(1, len(rows))
        replay_done = 0
        if progress_enabled:
            screening_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.20
            replay_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.90
        for row in rows:
            replay_done += 1
            now_report = now_ts()
            if progress_enabled and (replay_done == replay_total or replay_done % 5000 == 0 or now_report - replay_last_report >= 2.0):
                elapsed_replay = max(0.01, now_report - replay_started)
                rate = replay_done / elapsed_replay
                remaining = (replay_total - replay_done) / max(rate, 1e-9)
                frac = replay_done / replay_total
                p = screening_end + (replay_end - screening_end) * frac
                self.set_status(progress=p, message=f"{progress_label}: replay {replay_done:,}/{replay_total:,} archived state changes",
                                stage_eta_seconds=remaining, work_done=replay_done, work_total=replay_total,
                                work_unit="history rows", eta_source="measured replay throughput",
                                phase_detail=f"Chronological reward replay for {len(agents)} agent(s) · {rate:,.0f} rows/s")
                replay_last_report = now_report
            agents_for_target = target_map.get(row["entity_id"], [])
            if not agents_for_target:
                continue
            st = archived_state(row)
            for agent in agents_for_target:
                aid = agent["id"]
                policy = policies[aid]
                value = target_value(st, agent["target_property"])
                if value is None:
                    continue
                prev = last_value.get(aid)
                if prev is not None and abs(float(value) - float(prev)) <= max(0.01, float(agent["deadband"]) * 0.05):
                    continue

                if aid in pending:
                    old = pending[aid]
                    replay_completed_dwell(agent, policy, old, float(row["ts"]), row.get("context_user_id"))

                features_by_horizon = {}
                anchor_ts, upstream_anchor_ts = _fast_anchor(agent, policy, value, float(row["ts"]))
                for h in policy.horizons:
                    tracker = trackers[h]
                    tracker.advance(anchor_ts)
                    features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=anchor_ts)
                    features_by_horizon[h] = features
                upstream_features_by_horizon = {}
                if upstream_anchor_ts is not None and upstream_anchor_ts < anchor_ts and anchor_ts - upstream_anchor_ts <= float(OPTIONS.get("fast_upstream_lead_seconds", 4)):
                    for h in policy.horizons:
                        tracker = trackers[h]
                        tracker.advance(upstream_anchor_ts)
                        features, _, _ = policy.features(tracker.state_map, tracker.history, at_ts=upstream_anchor_ts)
                        upstream_features_by_horizon[h] = features

                actions = policy.actions
                action_idx = min(range(len(actions)), key=lambda i: abs(actions[i] - float(value)))
                pending[aid] = {
                    "history_id": row["id"], "ts": float(row["ts"]), "anchor_ts": anchor_ts,
                    "upstream_anchor_ts": upstream_anchor_ts, "features_by_horizon": features_by_horizon,
                    "upstream_features_by_horizon": upstream_features_by_horizon,
                    "action_idx": action_idx, "action_value": actions[action_idx], "user_id": row.get("context_user_id"),
                }
                last_value[aid] = float(value)

        for agent in agents:
            aid = agent["id"]
            policy = policies[aid]
            old = pending.get(aid)
            # The last open dwell is right-censored: wait for its actual end.
            # Otherwise its unique history id freezes a premature reward forever.
            if old:
                pass

        if progress_enabled:
            replay_end = float(progress_lo) + (float(progress_hi) - float(progress_lo)) * 0.90
            self.set_status(progress=replay_end, message=f"{progress_label}: finalizing held-out benchmark and policy models",
                            stage_eta_seconds=0, work_done=replay_total, work_total=replay_total,
                            work_unit="history rows", eta_source="benchmark finalization",
                            phase_detail=f"Replay complete · {new_count:,} new rewarded experiences")

        # The newest slice was held out while confidence was calibrated. Once its
        # out-of-sample score is recorded, fold it into the final policy so no history is
        # wasted. Calibration remains a genuine chronological backtest.
        for policy, horizon, action_idx, features, reward in heldout_updates:
            policy.update(horizon, action_idx, features, reward)

        for agent in agents:
            policy = policies[agent["id"]]
            STORE.save_model(agent["id"], policy.export())

        if benchmark:
            for agent in agents:
                STORE.set_partial_benchmark(agent["id"], benchmark_stats.get(agent["id"]) or {})

        qualification_summary = None
        if qualify:
            threshold = clamp(float(OPTIONS.get("candidate_benchmark_threshold", 0.78)), 0.0, 1.0)
            min_samples = max(1, int(OPTIONS.get("candidate_benchmark_min_samples", 12)))
            qualified_count = 0
            paused_count = 0
            for agent in agents:
                stat = benchmark_stats.get(agent["id"]) or {}
                samples = int(stat.get("samples") or 0)
                per_action = stat.get("per_action") or {}
                per_action_accuracy = {
                    key: (float(v.get("correct") or 0) / max(1, int(v.get("samples") or 0)))
                    for key, v in per_action.items() if int(v.get("samples") or 0) > 0
                }
                binary = len(policies[agent["id"]].actions) <= 2 or agent.get("target_property") == "power"
                class_coverage = (len(per_action_accuracy) >= 2) if binary else bool(per_action_accuracy)
                if binary and per_action_accuracy:
                    # Balanced transition accuracy: ON and OFF count equally. An agent
                    # that simply predicts the dominant OFF state cannot pass 78%.
                    score = sum(per_action_accuracy.values()) / len(per_action_accuracy)
                else:
                    score = float(stat.get("correct") or 0) / samples if samples else 0.0
                enough = samples >= min_samples and class_coverage
                passed = bool(enough and score > threshold)
                state = "qualified" if passed else "paused"
                origin_counts = {str(k): int(v or 0) for k, v in (stat.get("origin_counts") or {}).items()}
                automation_rules = int(stat.get("automation_rules") or 0)
                reason = (
                    "recorded-behaviour benchmark passed" if passed else
                    f"insufficient held-out behaviour samples ({samples}/{min_samples})" if not enough else
                    f"recorded-behaviour benchmark {score:.1%} below {threshold:.0%}"
                )
                detail = {
                    "threshold": threshold, "minimum_samples": min_samples,
                    "balanced": bool(binary), "class_coverage": bool(class_coverage),
                    "per_action_accuracy": per_action_accuracy,
                    "automation_rules": automation_rules,
                    "origin_counts": origin_counts,
                    "counts": {"samples": samples, "correct": int(stat.get("correct") or 0),
                               "per_action": stat.get("per_action") or {},
                               "automation_rules": automation_rules,
                               "origin_counts": origin_counts},
                    "reason": reason,
                }
                STORE.set_training_state(
                    agent["id"], state, score=score, samples=samples,
                    source="recorded-behaviour", detail=detail, demote_control=True,
                )
                STORE.set_training_progress(agent["id"], float(start_ts), float(end_ts), float(end_ts))
                if passed:
                    qualified_count += 1
                else:
                    paused_count += 1
                    # A failed full-history benchmark pauses all live inference/training CPU.
                    self.engine.runtime.pop(agent["id"], None)
                STORE.event(
                    agent["id"], "info" if passed else "warning",
                    "candidate_qualified" if passed else "candidate_paused",
                    f"Behaviour benchmark {score:.1%} over {samples} held-out transition(s): {reason}",
                    detail,
                )
            qualification_summary = {
                "qualified": qualified_count, "paused": paused_count,
                "threshold": threshold, "minimum_samples": min_samples,
            }
            if agent_ids is None:
                STORE.meta_set("candidate_qualification_complete", "1")

        if new_count or qualification_summary:
            STORE.event(
                None, "info", "offline_rl_training",
                f"Added {new_count} selective temporal RL experiences across {len(horizons)} prediction horizons",
                {"experiences": new_count, "prediction_horizons_seconds": horizons,
                 "feature_dimensions": int(OPTIONS.get("feature_dimensions", 128)),
                 "validation_fraction": validation_fraction, "heldout_updates": len(heldout_updates),
                 "qualification": qualification_summary},
            )
        return new_count


HISTORY = None

class HAEventStream(threading.Thread):
    """Near-real-time state_changed stream plus Entity Registry metadata.

    REST polling remains as a fallback/resync path, but inference is normally woken by
    the WebSocket event bus so control latency is not tied to the full /states poll.
    """
    daemon = True

    def __init__(self, engine):
        super().__init__(name="adaptive-ai-ha-events")
        self.engine = engine
        self.stop_event = threading.Event()

    def run(self):
        if ws_connect is None:
            self.engine.ws_error = "websockets package unavailable; REST fallback active"
            return
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                with ws_connect("ws://supervisor/core/websocket", open_timeout=10, close_timeout=5) as ws:
                    hello = json.loads(ws.recv())
                    if hello.get("type") == "auth_required":
                        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                        auth = json.loads(ws.recv())
                        if auth.get("type") != "auth_ok":
                            raise RuntimeError(auth.get("message") or "WebSocket auth failed")
                    elif hello.get("type") != "auth_ok":
                        raise RuntimeError(f"Unexpected WebSocket greeting: {hello.get('type')}")
                    self.engine.ws_connected = True
                    self.engine.ws_error = None
                    HA.last_ok = now_ts(); HA.last_error = None
                    # Registry metadata lets discovery exclude configuration/diagnostic entities.
                    ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                    ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
                    backoff = 2.0
                    while not self.stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if msg.get("type") == "result" and msg.get("id") == 1 and msg.get("success"):
                            self.engine.update_entity_registry(msg.get("result") or [])
                            continue
                        if msg.get("type") != "event" or msg.get("id") != 2:
                            continue
                        event = msg.get("event") or {}
                        data = event.get("data") or {}
                        self.engine.on_state_changed(data)
            except Exception as exc:
                self.engine.ws_connected = False
                self.engine.ws_error = f"{type(exc).__name__}: {exc}"
                if not self.stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(30.0, backoff * 1.7)
        self.engine.ws_connected = False


class Engine(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="adaptive-ai-engine")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.runtime = {}
        self.models = {}
        self.context_relevance = {}
        self.last_state_count = 0
        self.last_poll = None
        self.error = None
        self.state_map = {}
        self.entity_registry = {}
        self.archive_seen = {}
        self.archive_last_ts = {}
        self.pending_archive = []
        self.command_echoes = {}
        self.command_contexts = {}
        self.state_revision = 0
        self.entity_revisions = {}
        self.dirty_entities = set()
        self.last_trigger_entity = None
        self.control_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="device-control")
        self.poll_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-poll")
        self.in_flight = {}
        self.poll_future = None
        self.last_full_poll = 0.0
        self.last_ws_event = None
        self.ws_connected = False
        self.ws_error = None
        self.temporal_history = TemporalHistory(maxlen=24)
        self.lock = threading.RLock()

    def prime_temporal_from_archive(self, start_ts, end_ts):
        try:
            rows = STORE.archive_rows(start_ts=start_ts, end_ts=end_ts)
            for row in rows:
                self.temporal_history.add(row["entity_id"], float(row["ts"]), archived_state(row))
            return len(rows)
        except Exception as exc:
            print(f"[temporal] prime failed: {exc}", flush=True)
            return 0

    def status(self):
        # Never hold the engine lock while waiting on SQLite. This keeps Ingress/UI
        # responsive during large history imports and avoids lock-order stalls.
        with self.lock:
            runtime_conf = {aid: rt.get("last_confidence") for aid, rt in self.runtime.items()}
            engine_error = self.error
            last_poll = self.last_poll
            state_count = self.last_state_count
            ws_connected = self.ws_connected
            ws_error = self.ws_error
            registry_count = len(self.entity_registry)
            last_ws_event = self.last_ws_event
        agents = STORE.list_agents()
        confidences = [runtime_conf.get(a["id"]) for a in agents]
        confidences = [x for x in confidences if x is not None]
        history = HISTORY.status() if HISTORY is not None else {"phase": "starting", "archive": {"n": 0, "days": 0, "entities": 0}}
        return {
            "version": APP_VERSION,
            "ha_connected": HA.last_error is None and HA.last_ok is not None,
            "ha_last_ok": HA.last_ok,
            "ha_error": HA.last_error,
            "engine_error": engine_error,
            "last_poll": last_poll,
            "state_count": state_count,
            "options": OPTIONS,
            "ha_base_url": HA_BASE_URL,
            "agent_count": len(agents),
            "average_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
            "feedback_count": sum(int(a.get("feedback_count") or 0) for a in agents),
            "historical_experience_count": sum(int(a.get("historical_count") or 0) for a in agents),
            "realtime": {"connected": ws_connected, "error": ws_error, "registry_entries": registry_count, "last_event": last_ws_event},
            "automation_knowledge": AUTOMATION_KNOWLEDGE.status(),
            "history": history,
        }

    def update_entity_registry(self, entries):
        registry = {e.get("entity_id"): e for e in entries if isinstance(e, dict) and e.get("entity_id")}
        with self.lock:
            changed = registry != self.entity_registry
            self.entity_registry = registry
            if changed:
                # Context membership depends on device_id. Recreate in-memory policies so
                # a newly detected controllable device cannot leave sibling entities in
                # an old schema. Stored weights remain available for the history rebuild.
                self.models.clear()
        STORE.event(None, "info", "entity_registry", f"Loaded {len(registry)} Entity Registry entries for cleaner agent discovery", None)

    def registry_entry(self, entity_id):
        with self.lock:
            return self.entity_registry.get(entity_id)

    def on_state_changed(self, data):
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not entity_id:
            return
        with self.lock:
            old_state = self.state_map.get(entity_id)
            incoming_ts = parse_ts((new_state or {}).get("last_updated"))
            old_ts = parse_ts((old_state or {}).get("last_updated"))
            if incoming_ts and old_ts and incoming_ts < old_ts:
                return
            self.state_revision += 1
            self.entity_revisions[entity_id] = self.state_revision
            if new_state is None:
                self.state_map.pop(entity_id, None)
            else:
                self.state_map[entity_id] = new_state
            self.last_state_count = len(self.state_map)
            self.last_ws_event = now_ts()
            self.last_trigger_entity = entity_id
            self.dirty_entities.add(entity_id)
        HA.last_ok = now_ts(); HA.last_error = None
        if new_state is not None:
            ts = parse_ts(new_state.get("last_updated") or new_state.get("last_changed")) or now_ts()
            self.temporal_history.add(entity_id, ts, self._temporal_state(new_state))
            self._queue_archive_state(new_state)
        # A state transition can be the precursor to an action; wake inference now instead
        # of waiting for a whole-state REST poll.
        self.wake_event.set()

    def _compact_attrs(self, st):
        attrs = st.get("attributes") or {}
        compact = {}
        for k, v in attrs.items():
            if k in NUMERIC_ATTRS or k in (
                "device_class", "unit_of_measurement", "friendly_name", "supported_color_modes",
                "min", "max", "step", "options", "supported_features",
            ):
                compact[k] = v
        return compact

    def _temporal_state(self, st):
        if st is None:
            return None
        return {
            "entity_id": st.get("entity_id"), "state": st.get("state"),
            "attributes": self._compact_attrs(st),
            "last_changed": st.get("last_changed"), "last_updated": st.get("last_updated"),
        }

    def _queue_archive_state(self, st, force=False):
        entity_id = st.get("entity_id")
        if not entity_id:
            return
        now = now_ts()
        compact_attrs = self._compact_attrs(st)
        fingerprint = json.dumps([st.get("state"), compact_attrs], sort_keys=True, separators=(",", ":"), default=str)
        with self.lock:
            if fingerprint == self.archive_seen.get(entity_id):
                return
            controllable = bool(target_options_for_state(st))
            discrete = entity_id.split('.')[0] in ("binary_sensor", "person", "device_tracker", "input_boolean")
            if not force and not controllable and not discrete and now - self.archive_last_ts.get(entity_id, 0.0) < float(OPTIONS["archive_context_interval_seconds"]):
                return
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or now
            user_id = (st.get("context") or {}).get("user_id")
            self.pending_archive.append((entity_id, ts, st.get("state"), compact_attrs, user_id, "live"))
            self.archive_seen[entity_id] = fingerprint
            self.archive_last_ts[entity_id] = now

    def flush_archive(self):
        with self.lock:
            rows = self.pending_archive
            self.pending_archive = []
        if rows:
            STORE.archive_batch(rows)

    def refresh_states(self):
        with self.lock:
            poll_revision = self.state_revision
        states = HA.states()
        state_map = {s["entity_id"]: s for s in (states or [])}
        with self.lock:
            # A REST request may finish after newer websocket events. Never rewind them.
            for eid, old in self.state_map.items():
                new = state_map.get(eid)
                newer_event = self.entity_revisions.get(eid, 0) > poll_revision
                old_ts = parse_ts(old.get("last_updated")) or 0
                new_ts = parse_ts((new or {}).get("last_updated")) or 0
                if newer_event or (new is not None and old_ts > new_ts):
                    state_map[eid] = old
            for eid, revision in self.entity_revisions.items():
                if revision > poll_revision and eid not in self.state_map:
                    state_map.pop(eid, None)
            self.state_map = state_map
            self.last_state_count = len(state_map)
            self.last_poll = now_ts()
            self.last_full_poll = self.last_poll
            self.error = None
        for st in state_map.values():
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or now_ts()
            self.temporal_history.add(st.get("entity_id"), ts, self._temporal_state(st))
            self._queue_archive_state(st)
        self.flush_archive()
        return state_map

    def run(self):
        print(f"Adaptive AI {APP_VERSION} starting; HA={HA_BASE_URL}", flush=True)
        STORE.event(None, "info", "startup", f"Adaptive AI {APP_VERSION} started", None)
        try:
            self.refresh_states()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        tick = max(0.5, float(OPTIONS.get("proactive_tick_seconds", 1)))
        debounce = max(0.0, float(OPTIONS.get("realtime_inference_debounce_ms", 150)) / 1000.0)
        while not self.stop_event.is_set():
            event_wakeup = self.wake_event.wait(tick)
            self.wake_event.clear()
            if event_wakeup and debounce:
                # Coalesce bursts (motion + lux + light state etc.) into one inference pass.
                self.stop_event.wait(debounce)
            try:
                if now_ts() - self.last_full_poll >= float(OPTIONS["poll_seconds"]) and (self.poll_future is None or self.poll_future.done()):
                    self.last_full_poll = now_ts()
                    self.poll_future = self.poll_worker.submit(self.refresh_states)
                self.flush_archive()
                with self.lock:
                    state_map = dict(self.state_map)
                    changed_entities = set(self.dirty_entities) if event_wakeup else set()
                    if event_wakeup:
                        self.dirty_entities.clear()
                if state_map:
                    self.process(state_map, changed_entities if event_wakeup else None)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                with self.lock:
                    self.error = msg
                print(f"[engine] {msg}", flush=True)

    def archive_live_states(self, state_map):
        # Kept for compatibility with tests/older call sites; event-driven runtime queues
        # changes and commits them in batches.
        for st in state_map.values():
            self._queue_archive_state(st)
        self.flush_archive()

    def policy(self, agent):
        aid = agent["id"]
        actions = action_values(agent)
        cached = self.models.get(aid)
        if cached and cached.actions == actions and cached.dims == int(OPTIONS["feature_dimensions"]):
            return cached
        hint_entities, _ = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        with self.lock:
            state_map = dict(self.state_map)
            registry = dict(self.entity_registry)
        model = MultiHorizonPolicy(agent, state_map, registry, hint_entities, STORE.get_model(aid), self.context_relevance.get(aid))
        self.models[aid] = model
        return model

    def take_control(self, agent, refresh=False):
        """Disable matching automations and verify OFF before granting ownership."""
        if refresh:
            self.refresh_states()
            with self.lock:
                states, registry = dict(self.state_map), dict(self.entity_registry)
            AUTOMATION_KNOWLEDGE.scan(states, registry, force=True)
        _, infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        disabled = []
        for info in infos:
            eid = info.get("entity_id")
            if not eid or not eid.startswith("automation."):
                continue
            with self.lock:
                state = self.state_map.get(eid)
            if (state or {}).get("state") == "off":
                continue
            if state is None and not info.get("enabled"):
                continue
            HA.service("automation", "turn_off", {"entity_id": eid, "stop_actions": True})
            disabled.append(eid)
            STORE.event(agent["id"], "info", "automation_takeover_requested",
                        f"Control requested disabling {eid}", {"entity_id": eid, "target_entity": agent["target_entity"]})
        if disabled:
            self.refresh_states()
            unresolved = [eid for eid in disabled if self.state_map.get(eid, {}).get("state") != "off"]
            if unresolved:
                raise RuntimeError("Automation OFF not confirmed: " + ", ".join(unresolved))
            with AUTOMATION_KNOWLEDGE.lock:
                for info in AUTOMATION_KNOWLEDGE.automations:
                    if info.get("entity_id") in disabled:
                        info["enabled"] = False
            STORE.event(agent["id"], "info", "automation_takeover",
                        "Control disabled matching automations", {"disabled": disabled})
        return disabled

    def process(self, state_map, changed_entities=None):
        changed = set(changed_entities or ())
        groups = {}
        for agent in STORE.list_agents():
            if not agent["enabled"] or agent["mode"] == "paused" or agent.get("training_state") != "qualified":
                continue
            if changed:
                cached = self.models.get(agent["id"])
                if cached is not None and agent["target_entity"] not in changed and not (changed & set(cached.schema.entities)):
                    continue
            groups.setdefault(agent["target_entity"], []).append(agent)
        for target, agents in groups.items():
            active = self.in_flight.get(target)
            if active is not None and not active.done():
                continue
            self.in_flight[target] = self.control_workers.submit(self.process_target, agents, changed)
        for target in list(self.in_flight):
            if target not in groups and self.in_flight[target].done():
                del self.in_flight[target]

    def process_target(self, agents, changed_entities=None):
        with self.lock:
            revision = self.state_revision
            states = dict(self.state_map)
        for agent in agents:
            if self.stop_event.is_set():
                return
            try:
                latest = STORE.get_agent(agent["id"])
                if latest and latest["enabled"] and latest["mode"] != "paused" and latest.get("training_state") == "qualified":
                    if changed_entities:
                        self.process_agent(latest, states, changed_entities)
                    else:
                        self.process_agent(latest, states)
            except Exception as exc:
                STORE.event(agent["id"], "error", "agent_error", str(exc), {"trace": traceback.format_exc(limit=4)})
        if self.state_revision != revision:
            self.wake_event.set()

    def release_manual_hold(self, agent_id):
        STORE.meta_set("manual_hold:" + agent_id, "0")
        STORE.meta_set("manual_hold_source:" + agent_id, "")
        rt = self.runtime.get(agent_id)
        if rt:
            rt["manual_override_until"] = 0.0
        self.wake_event.set()

    def _reward_pending(self, agent, rt, reward, reason, user_id=None):
        pending = rt.get("pending")
        if not pending:
            return
        policy = self.policy(agent)
        policy.update(int(pending.get("horizon") or min(policy.horizons)), pending["action_index"], pending["features"], reward)
        STORE.save_model(agent["id"], policy.export())
        STORE.add_feedback(
            agent["id"], pending["action_index"], pending["action_value"], reward, reason,
            pending["features"], user_id,
        )
        rt["last_reward"] = reward
        rt["last_reward_reason"] = reason
        rt["pending"] = None
        STORE.event(
            agent["id"], "info" if reward >= 0 else "warning", "rl_reward",
            f"RL reward {reward:+.2f}: {reason}",
            {"reward": reward, "reason": reason, "action_value": pending["action_value"], "user_id": user_id},
        )

    def record_command(self, agent, value, response=None):
        """Remember intent and returned contexts; REST HA calls can have a user_id."""
        timestamp = now_ts()
        expiry = timestamp + max(30.0, timing_for(agent).acknowledgement * 2)
        with self.lock:
            self.command_contexts = {k: v for k, v in self.command_contexts.items() if v > timestamp}
            eid = agent["target_entity"]
            records = self.command_echoes.setdefault(eid, [])
            records[:] = [r for r in records if r["expires"] > timestamp][-7:]
            records.append({"property": agent["target_property"], "value": value, "expires": expiry})
            for state in response if isinstance(response, list) else []:
                if not isinstance(state, dict):
                    continue
                context_id = (state.get("context") or {}).get("id")
                if context_id:
                    self.command_contexts[context_id] = expiry

    def own_command_echo(self, agent, state, current):
        timestamp = now_ts()
        context = (state or {}).get("context") or {}
        with self.lock:
            if any(self.command_contexts.get(context.get(k), 0) > timestamp for k in ("id", "parent_id")):
                return True
            return any(r["expires"] > timestamp and r["property"] == agent["target_property"]
                       and same_value(current, r["value"], agent["deadband"])
                       for r in self.command_echoes.get(agent["target_entity"], [])[-1:])

    def set_manual_hold(self, agent, rt, timestamp):
        rt["manual_override_until"] = timestamp + timing_for(agent).manual_hold
        STORE.meta_set("manual_hold:" + agent["id"], str(rt["manual_override_until"]))
        STORE.meta_set("manual_hold_source:" + agent["id"], "explicit_user_v8")

    def process_agent(self, agent, state_map, changed_entities=None):
        aid = agent["id"]
        defaults = {
            "previous_target": None, "last_ai_ts": 0.0, "last_ai_value": None,
            "last_inference_ts": 0.0, "manual_override_until": 0.0,
            "last_prediction": None, "last_confidence": 0.0, "pending": None,
            "last_reward": None, "last_reward_reason": None, "context_meta": {},
            "decision_state": "idle", "decision_reason": "Waiting for inference",
            "last_service_ts": None, "last_service": None, "last_service_ok": None,
            "last_service_error": None, "last_service_data": None,
        }
        with self.lock:
            rt = self.runtime.setdefault(aid, {})
        for key, value in defaults.items():
            rt.setdefault(key, value)
        timing = timing_for(agent)
        if not rt.get("restored"):
            # Old versions persisted holds for anonymous changes and even their own echoes.
            trusted = STORE.meta_get("manual_hold_source:" + aid, "") == "explicit_user_v8"
            rt["manual_override_until"] = float(STORE.meta_get("manual_hold:" + aid, "0")) if trusted else 0.0
            rt["restored"] = True
        target_state = state_map.get(agent["target_entity"])
        current = target_value(target_state, agent["target_property"])
        if current is None or not math.isfinite(current):
            rt["pending"] = None  # missing outcome is not acceptance
            rt["previous_target"] = None
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = "Target state/value unavailable"
            return
        if agent["target_property"] == "position" and target_state.get("state") in ("opening", "closing"):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Cover is moving; wait for its final position"
            return

        # Resolve acknowledgement separately from delayed preference feedback.
        timestamp = now_ts()
        pending = rt.get("pending")
        previous = rt.get("previous_target")
        changed = previous is not None and abs(current - previous) > max(.01, agent["deadband"] * .05)
        context = (target_state or {}).get("context") or {}
        user_id = context.get("user_id") if not context.get("parent_id") else None
        own_echo = self.own_command_echo(agent, target_state, current)
        expected_ack = pending and same_value(current, pending["action_value"], agent["deadband"])
        if changed:
            rt["last_change_origin"] = "own_command" if own_echo or expected_ack else "manual_user" if user_id else "external"
        if changed and user_id and not own_echo and not expected_ack:
            if pending:
                if not same_value(current, pending["action_value"], agent["deadband"]):
                    self._reward_pending(agent, rt, -1.0, "manual correction", user_id)
                else:
                    rt["pending"] = None
            # A demonstrated preference is useful even in Shadow and without an AI action.
            policy = self.policy(agent)
            features, _, _ = policy.features(state_map, self.temporal_history, at_ts=timestamp)
            idx = min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i] - current))
            for h in policy.horizons:
                policy.update(h, idx, features, 1.0)
            STORE.save_model(aid, policy.export())
            STORE.add_feedback(aid, idx, policy.actions[idx], 1.0, "manual demonstration", features, user_id)
            self.set_manual_hold(agent, rt, timestamp)
        elif pending:
            matches = same_value(current, pending["action_value"], agent["deadband"])
            age = timestamp - pending["started_ts"]
            if matches and pending.get("acknowledged_ts") is None:
                pending["acknowledged_ts"] = timestamp
                rt["ack_latency_seconds"] = age
            elif pending.get("acknowledged_ts") is None and age >= timing.acknowledgement:
                rt["pending"] = None
                rt["retry_after"] = timestamp + max(30.0, timing.settling)
                STORE.event(aid, "warning", "ack_timeout", "Device did not confirm the requested value", {"seconds": age})
            elif changed and pending.get("acknowledged_ts") is not None and not matches:
                # Unknown hardware changes and automations are ambiguous, not human labels.
                rt["pending"] = None
                # Unknown/linked actuator changes are not human instructions.
                rt["last_reward_reason"] = "external target change (not a manual override)"
            elif matches and pending.get("acknowledged_ts") is not None:
                window = max(timing.settling, float(OPTIONS["reward_window_seconds"]))
                if timestamp - pending["acknowledged_ts"] >= window:
                    self._reward_pending(agent, rt, .15, "weak acceptance after settling")
        elif changed and not own_echo:
            rt["last_reward_reason"] = "external target change (not a manual override)"
        rt["previous_target"] = current

        if agent["mode"] not in ("shadow", "control"):
            rt["decision_state"] = "paused"
            rt["decision_reason"] = "Agent is paused"
            return
        min_inference_gap = max(0.05, float(OPTIONS.get("realtime_inference_debounce_ms", 75)) / 1000.0)
        if now_ts() - rt["last_inference_ts"] < min_inference_gap:
            return
        rt["last_inference_ts"] = now_ts()

        hint_entities, automation_infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        policy = self.policy(agent)
        features, labels, context_meta = policy.features(state_map, self.temporal_history, at_ts=now_ts())
        context_meta.update(policy.selection_meta or {})
        context_meta["automation_hint_entities"] = len(hint_entities)
        context_meta["whole_home_entities"] = len(state_map)
        context_meta["trigger_entities"] = sorted(set(changed_entities or ()))[:8]
        context_meta["primary_local_sensors"] = list((policy.selection_meta or {}).get("primary_local_sensors") or [])
        context_meta["primary_local_sensor"] = (policy.selection_meta or {}).get("primary_local_sensor")
        context_meta["primary_occupancy_sensor"] = (policy.selection_meta or {}).get("primary_occupancy_sensor")
        context_meta["causal_presence_scores"] = dict((policy.selection_meta or {}).get("causal_presence_scores") or {})
        context_meta["upstream_sensors"] = list((policy.selection_meta or {}).get("upstream_sensors") or [])
        rt["context_meta"] = context_meta
        rt["automation_priors"] = [
            {"entity_id": x.get("entity_id"), "name": x.get("name"), "enabled": bool(x.get("enabled")),
             "context_count": len(x.get("context_entities") or [])}
            for x in automation_infos[:8]
        ]

        chosen, confidence, arms, horizon, support, novelty = policy.choose(features, explore=False)
        micro_explore = False
        if agent["mode"] == "control" and agent.get("micro_exploration") and (confidence < float(agent["confidence_threshold"]) or support < float(OPTIONS.get("min_historical_support", 0.20))):
            if now_ts() - rt.get("last_exploration_ts", 0.0) >= float(agent.get("exploration_interval") or 21600):
                max_step = max(float(agent.get("exploration_step") or 0), float(agent["deadband"]))
                allowed = [i for i, v in enumerate(policy.actions) if abs(v - current) <= max_step + 1e-9]
                nearest = min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i] - current))
                if nearest not in allowed:
                    allowed.append(nearest)
                candidate, candidate_conf, _, candidate_horizon, candidate_support, candidate_novelty = policy.choose(features, explore=True, allowed_indices=allowed)
                # Targeted exploration only fills a real support gap and stays close to the greedy policy.
                if candidate["mean"] >= chosen["mean"] - 0.12 and candidate_novelty <= max(0.95, novelty + 0.10):
                    chosen, confidence, horizon, support, novelty = candidate, candidate_conf, candidate_horizon, candidate_support, candidate_novelty
                    micro_explore = True

        rt["last_prediction"] = chosen["value"]
        rt["last_confidence"] = confidence
        rt["structural_confidence"] = chosen.get("structural_confidence", confidence)
        rt["validation_accuracy"] = chosen.get("validation_accuracy", 0.0)
        rt["validation_lower_bound"] = chosen.get("validation_lower_bound", 0.0)
        rt["validation_samples"] = chosen.get("validation_samples", 0)
        rt["last_uncertainty"] = chosen["uncertainty"]
        rt["last_expected_reward"] = chosen["mean"]
        rt["historical_support"] = support
        rt["context_novelty"] = novelty
        rt["prediction_horizon"] = horizon
        rt["micro_exploration_candidate"] = micro_explore
        rt["top_context"] = self.top_context(policy, horizon, chosen["index"], features, labels)

        if agent["mode"] == "shadow":
            rt["decision_state"] = "shadow"
            rt["decision_reason"] = (f"Desired state {chosen['value']:.2f}; reactive inference ~{int(OPTIONS.get('realtime_inference_debounce_ms', 75))} ms, "
                                     f"calibrated confidence {confidence:.0%}, support {support:.0%}, novelty {novelty:.0%}")
            old = rt.get("last_shadow_logged")
            if old is None or abs(chosen["value"] - old) >= max(agent["deadband"], 0.01):
                rt["last_shadow_logged"] = chosen["value"]
                STORE.event(
                    aid, "info", "shadow_prediction",
                    f"RL policy desires {chosen['value']:.2f} after current context event (confidence {confidence:.0%})",
                    {"prediction": chosen["value"], "confidence": confidence, "expected_reward": chosen["mean"],
                     "prediction_horizon": horizon, "historical_support": support, "context_novelty": novelty},
                )
            return

        if now_ts() < rt.get("takeover_retry_after", 0):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Retrying automation takeover after HA error"
            return
        try:
            disabled = self.take_control(agent)
        except Exception as exc:
            rt["takeover_retry_after"] = now_ts() + 30
            rt["decision_state"] = "error"
            rt["decision_reason"] = f"Automation takeover failed: {exc}"
            STORE.event(aid, "error", "automation_takeover_failed", str(exc))
            return
        if disabled:
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Automations disabled; evaluating fresh device state"
            return
        # If the desired state already matches reality there is nothing to send. Do this
        # before feedback/cooldown gates so a pending reward never makes a correct state
        # look artificially "stuck".
        pending = rt.get("pending")
        supersede = bool(pending and pending.get("acknowledged_ts") is None
                         and agent["target_entity"].split('.')[0] in ("light", "input_boolean")
                         and not same_value(chosen["value"], pending["action_value"], agent["deadband"]))
        if abs(chosen["value"] - current) < float(agent["deadband"]) and not supersede:
            rt["decision_state"] = "hold"
            rt["decision_reason"] = f"Holding current value {current:.2f}; desired {chosen['value']:.2f} already matches"
            return

        pending = rt.get("pending")
        if pending:
            acknowledged = pending.get("acknowledged_ts")
            if not supersede and (acknowledged is None or now_ts() - acknowledged < timing.settling):
                rt["decision_state"] = "waiting"
                rt["decision_reason"] = "Waiting for device acknowledgement / physical settling"
                return
            # Do not teach the policy that its own changed prediction proves acceptance.
            # Retain unresolved intent until a replacement is actually sent. Cooldown
            # and manual gates may still reject this inference pass.
            if not supersede:
                rt["pending"] = None

        if now_ts() < rt.get("retry_after", 0):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Device retry backoff"
            return
        if now_ts() < rt.get("manual_override_until", 0.0):
            remain = max(0, int(rt.get("manual_override_until", 0.0) - now_ts()))
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = f"Manual override active for ~{remain}s"
            return
        elapsed = now_ts() - rt["last_ai_ts"]
        if elapsed < max(float(agent["action_interval"]), timing.settling):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = f"Action cooldown: {max(0, int(float(agent['action_interval'])-elapsed))}s remaining"
            return
        if agent["mode"] == "control" and confidence < float(agent["confidence_threshold"]) and not micro_explore:
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = f"Confidence {confidence:.0%} below threshold {float(agent['confidence_threshold']):.0%}"
            return
        if agent["mode"] == "control" and support < float(OPTIONS.get("min_historical_support", 0.20)) and not micro_explore:
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = f"Historical support {support:.0%} below safety floor {float(OPTIONS.get('min_historical_support', .20)):.0%}"
            return
        if agent["mode"] == "control" and novelty > float(OPTIONS.get("max_context_novelty", 0.85)) and not micro_explore:
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = f"Context novelty {novelty:.0%} above safety ceiling {float(OPTIONS.get('max_context_novelty', .85)):.0%}"
            return
        conflicts = [a for a in STORE.list_agents() if a["id"] != aid and a["enabled"] and a["mode"] == "control" and a["target_entity"] == agent["target_entity"]]
        if conflicts:
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = "Multiple Control agents own this entity"
            return
        # Recheck the latest state before dispatch; events may arrive during inference.
        with self.lock:
            latest = self.state_map.get(agent["target_entity"])
        if latest != target_state:
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Target changed during inference; retry on fresh state"
            return
        value = legal_value(agent, target_state, chosen["value"])
        if same_value(value, current, agent["deadband"]) and not supersede:
            rt["decision_state"] = "hold"
            rt["decision_reason"] = "Nearest legal device value already set"
            return
        domain, service, data = target_call(agent["target_entity"], agent["target_property"], value)
        rt["last_service_ts"] = now_ts()
        rt["last_service"] = f"{domain}.{service}"
        rt["last_service_data"] = data
        started = now_ts()
        # Record intent before the blocking HTTP call: websocket echoes can arrive first.
        self.record_command(agent, value)
        try:
            response = HA.service(domain, service, data)
            self.record_command(agent, value, response)
            rt["last_service_latency_ms"] = (now_ts() - started) * 1000.0
            rt["last_service_ok"] = True
            rt["last_service_error"] = None
        except Exception as exc:
            rt["retry_after"] = now_ts() + max(30.0, timing.settling)
            rt["last_service_ok"] = False
            rt["last_service_error"] = f"{type(exc).__name__}: {exc}"
            rt["decision_state"] = "error"
            rt["decision_reason"] = f"Home Assistant service call failed: {exc}"
            STORE.event(
                aid, "error", "service_call_failed",
                f"{domain}.{service} failed: {exc}",
                {"service": f"{domain}.{service}", "service_data": data},
            )
            return
        rt["last_ai_ts"] = started
        rt["last_ai_value"] = value
        rt["decision_state"] = "acted"
        rt["decision_reason"] = f"Sent {domain}.{service} → {value:.2f}; waiting for device acknowledgement"
        rt["pending"] = {
            "action_index": chosen["index"], "action_value": value, "horizon": horizon,
            "acknowledged_ts": None,
            "features": features, "started_ts": started, "no_service": False,
        }
        if micro_explore:
            rt["last_exploration_ts"] = now_ts()
        STORE.event(
            aid, "info", "micro_exploration" if micro_explore else "ai_action",
            f"{'Micro exploration' if micro_explore else 'Control'} → {value:.2f} (policy confidence {confidence:.0%})",
            {"prediction": value, "confidence": confidence, "expected_reward": chosen["mean"],
             "uncertainty": chosen["uncertainty"], "prediction_horizon": horizon,
             "historical_support": support, "context_novelty": novelty,
             "service": f"{domain}.{service}", "service_data": data},
        )

    def top_context(self, policy, horizon, action_idx, features, labels):
        head = policy.heads[int(horizon)]
        aa, bb = head.a[action_idx], head.b[action_idx]
        ranked = []
        for idx, x in features.items():
            if idx >= policy.dims:
                continue
            theta = bb[idx] / max(aa[idx], 1e-9)
            contribution = theta * x
            if abs(contribution) < 1e-4:
                continue
            ranked.append((abs(contribution), contribution, labels.get(idx, [f"feature:{idx}"])[:2]))
        ranked.sort(reverse=True)
        return [{"feature": " / ".join(lbls), "contribution": contrib} for _, contrib, lbls in ranked[:7]]

    def runtime_for(self, agent):
        rt = self.runtime.get(agent["id"]) or {}
        confidence = float(rt.get("last_confidence") or 0.0)
        policy = self.models.get(agent["id"])
        selected_entities = list(policy.schema.entities) if policy else None
        with self.lock:
            recs, present = sensor_recommendations(agent, self.state_map, confidence, selected_entities)
            target_state = self.state_map.get(agent["target_entity"])
        prediction_label = None
        if agent["target_property"] == "option_index" and rt.get("last_prediction") is not None:
            options = list(((target_state or {}).get("attributes") or {}).get("options") or [])
            idx = int(clamp(round(float(rt["last_prediction"])), 0, max(0, len(options) - 1))) if options else 0
            prediction_label = options[idx] if options else None
        return {
            "last_prediction": rt.get("last_prediction"),
            "current_value": target_value(target_state, agent["target_property"]) if target_state else None,
            "last_prediction_label": prediction_label,
            "last_confidence": confidence,
            "structural_confidence": float(rt.get("structural_confidence") or 0.0),
            "validation_accuracy": float(rt.get("validation_accuracy") or 0.0),
            "validation_lower_bound": float(rt.get("validation_lower_bound") or 0.0),
            "validation_samples": int(rt.get("validation_samples") or 0),
            "last_uncertainty": rt.get("last_uncertainty"),
            "last_expected_reward": rt.get("last_expected_reward"),
            "last_ai_ts": rt.get("last_ai_ts"),
            "manual_override_until": rt.get("manual_override_until", 0.0),
            "last_change_origin": rt.get("last_change_origin"),
            "last_service_latency_ms": rt.get("last_service_latency_ms"),
            "timing": vars(timing_for(agent)),
            "ack_latency_seconds": rt.get("ack_latency_seconds"),
            "pending_feedback": bool(rt.get("pending")),
            "last_reward": rt.get("last_reward"),
            "last_reward_reason": rt.get("last_reward_reason"),
            "context_meta": rt.get("context_meta") or (dict(policy.selection_meta) if policy else {}),
            "top_context": rt.get("top_context") or [],
            "sensor_recommendations": recs,
            "present_capabilities": present,
            "automation_priors": rt.get("automation_priors") or [],
            "enabled_automation_conflicts": sum(1 for x in (rt.get("automation_priors") or []) if x.get("enabled")),
            "decision_state": rt.get("decision_state") or "idle",
            "decision_reason": rt.get("decision_reason") or "Waiting for inference",
            "last_service_ts": rt.get("last_service_ts"),
            "last_service": rt.get("last_service"),
            "last_service_ok": rt.get("last_service_ok"),
            "last_service_error": rt.get("last_service_error"),
            "last_service_data": rt.get("last_service_data"),
            "prediction_lead_seconds": int(rt.get("prediction_horizon") or 0),
            "prediction_horizon": int(rt.get("prediction_horizon") or 0),
            "historical_support": float(rt.get("historical_support") or 0.0),
            "context_novelty": float(rt.get("context_novelty") if rt.get("context_novelty") is not None else 1.0),
            "realtime_connected": bool(self.ws_connected),
            "policy_updates": int(policy.total_updates) if policy else int(agent.get("feedback_count") or 0) + int(agent.get("historical_count") or 0),
            "historical_experiences": int(agent.get("historical_count") or 0),
            "micro_exploration": bool(agent.get("micro_exploration")),
            "selected_context_entities": list(policy.schema.entities) if policy else [],
            "prediction_horizons": list(policy.horizons) if policy else parse_horizons(agent),
            "training_state": agent.get("training_state") or "training",
            "benchmark_score": agent.get("benchmark_score"),
            "benchmark_samples": int(agent.get("benchmark_samples") or 0),
            "benchmark_source": agent.get("benchmark_source"),
            "benchmark_detail": agent.get("benchmark_detail") or {},
            "model": "Automation-benchmarked short-series behavioural-driver LinUCB RL",
        }


ENGINE = Engine()


def entity_summary(state):
    entity_id = state["entity_id"]
    domain = entity_id.split(".", 1)[0]
    attrs = state.get("attributes") or {}
    return {
        "entity_id": entity_id, "domain": domain, "name": attrs.get("friendly_name") or entity_id,
        "state": state.get("state"), "unit": attrs.get("unit_of_measurement"),
        "target_options": target_options_for_state(state),
    }


def validate_agent(p):
    if not isinstance(p, dict):
        return "expected an object"
    for key in ("name", "target_entity", "target_property", "min_value", "max_value"):
        if key not in p:
            return f"missing field: {key}"
    if not isinstance(p["target_entity"], str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", p["target_entity"]):
        return "invalid target entity ID"
    domain = p["target_entity"].split(".", 1)[0]
    allowed = {x["property"] for x in SUPPORTED_TARGETS.get(domain, [])}
    if p["target_property"] not in allowed:
        return f"unsupported target property for {domain}"
    try:
        inputs = p.get("input_entities", ["*"])
        if not isinstance(inputs, list) or not inputs or any(not isinstance(x, str) or (x != "*" and not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x)) for x in inputs):
            return "input_entities must be a non-empty list of entity IDs or *"
        for key in ("min_value", "max_value", "deadband", "action_interval", "exploration_step", "exploration_interval", "confidence_threshold", "ack_timeout", "settling_seconds", "manual_hold_seconds"):
            if key in p and not math.isfinite(float(p[key])):
                return f"{key} must be finite"
        for key in ("deadband", "action_interval", "exploration_interval"):
            if key in p and float(p[key]) <= 0:
                return f"{key} must be positive"
        for key in ("ack_timeout", "settling_seconds", "manual_hold_seconds"):
            if key in p and not 0 <= float(p[key]) <= 86400:
                return f"{key} must be between 0 (automatic) and 86400 seconds"
        if not 0 <= float(p.get("confidence_threshold", .75)) <= 1:
            return "confidence_threshold must be between 0 and 1"
        if p.get("mode", "shadow") not in ("shadow", "control", "paused"):
            return "invalid mode"
        if float(p["min_value"]) >= float(p["max_value"]):
            return "minimum must be smaller than maximum"
        if float(p.get("exploration_step", 0)) <= 0:
            return "exploration_step must be positive"
    except Exception:
        return "invalid numeric limits"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AdaptiveAI/0.4"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def send_bytes(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def static(self, name, content_type):
        path = STATIC_DIR / name
        if not path.exists():
            return self.send_json(404, {"error": "not_found"})
        return self.send_bytes(200, path.read_bytes(), content_type)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", ""):
                return self.static("index.html", "text/html; charset=utf-8")
            if path == "/style.css":
                return self.static("style.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self.static("app.js", "application/javascript; charset=utf-8")
            if path == "/settings.js":
                return self.static("settings.js", "application/javascript; charset=utf-8")
            if path == "/api/status":
                return self.send_json(200, ENGINE.status())
            if path == "/api/entities":
                with ENGINE.lock:
                    states = list(ENGINE.state_map.values())
                return self.send_json(200, sorted([entity_summary(s) for s in states], key=lambda x: (x["domain"], x["name"].lower())))
            if path == "/api/agents":
                agents = STORE.list_agents()
                for a in agents:
                    a["runtime"] = ENGINE.runtime_for(a)
                return self.send_json(200, agents)
            if path.startswith("/api/agents/") and path.endswith("/feedback"):
                agent_id = path.split("/")[3]
                return self.send_json(200, STORE.list_feedback(agent_id, 100))
            if path == "/api/events":
                limit = 100
                if "limit=" in query:
                    try:
                        limit = int(clamp(int(query.split("limit=", 1)[1].split("&", 1)[0]), 1, 500))
                    except Exception:
                        pass
                return self.send_json(200, STORE.list_events(limit))
            if path == "/health":
                return self.send_json(200, {"ok": True, "version": APP_VERSION})
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        try:
            if path == "/api/discovery/rescan":
                with ENGINE.lock:
                    current = dict(ENGINE.state_map)
                    registry = dict(ENGINE.entity_registry)
                if not current or HISTORY is None:
                    return self.send_json(409, {"error": "Home Assistant state/history engine not ready"})
                AUTOMATION_KNOWLEDGE.scan(current, registry)
                start_ts = now_ts() - float(OPTIONS["history_bootstrap_days"]) * 86400.0
                created = HISTORY.auto_discover_agents(current, start_ts, threshold_override=1)
                return self.send_json(200, {"ok": True, "created": created, "training_started": 0, "manual_training": True, "history": HISTORY.status(), "automation_knowledge": AUTOMATION_KNOWLEDGE.status()})
            if path.startswith("/api/agents/") and path.endswith("/train"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") == "training":
                    return self.send_json(409, {"error": "this agent is already training"})
                partial = agent.get("training_cursor_ts") is not None and float(agent.get("training_progress") or 0.0) < 0.999
                started = HISTORY.request_agent_resume(agent_id) if partial else HISTORY.request_agent_rebuild(agent_id)
                if not started:
                    return self.send_json(409, {"error": "another training job is already active; low-memory mode allows one at a time"})
                return self.send_json(202, {"ok": True, "state": "training", "resumed": bool(partial), "message": "Per-agent training started in low-memory mode"})
            if path.startswith("/api/agents/") and path.endswith("/resume"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent or HISTORY is None:
                    return self.send_json(404, {"error": "agent/history engine not found"})
                if agent.get("training_state") != "paused":
                    return self.send_json(409, {"error": "Resume is available only for PAUSED agents"})
                started = HISTORY.request_agent_resume(agent_id)
                if not started:
                    return self.send_json(409, {"error": "training job is already active"})
                return self.send_json(202, {"ok": True, "state": "training", "message": "Resume scheduled from saved cursor"})
            if path.startswith("/api/agents/") and path.endswith("/verify-control"):
                agent_id = path.split("/")[3]
                agent = STORE.get_agent(agent_id)
                if not agent:
                    return self.send_json(404, {"error": "agent not found"})
                with ENGINE.lock:
                    state = ENGINE.state_map.get(agent["target_entity"])
                current = target_value(state, agent["target_property"])
                if current is None:
                    return self.send_json(409, {"error": "target value unavailable"})
                domain, service, data = target_call(agent["target_entity"], agent["target_property"], current)
                try:
                    result = HA.service(domain, service, data)
                    ENGINE.record_command(agent, current, result)
                    rt = ENGINE.runtime.setdefault(agent_id, {})
                    rt["last_service_ts"] = now_ts(); rt["last_service"] = f"{domain}.{service}"
                    rt["last_service_ok"] = True; rt["last_service_error"] = None; rt["last_service_data"] = data
                    STORE.event(agent_id, "info", "control_verified", f"Verified HA service access via {domain}.{service}", {"service_data": data})
                    return self.send_json(200, {"ok": True, "service": f"{domain}.{service}", "service_data": data, "current_value": current, "ha_response": result})
                except Exception as exc:
                    rt = ENGINE.runtime.setdefault(agent_id, {})
                    rt["last_service_ts"] = now_ts(); rt["last_service"] = f"{domain}.{service}"
                    rt["last_service_ok"] = False; rt["last_service_error"] = f"{type(exc).__name__}: {exc}"; rt["last_service_data"] = data
                    STORE.event(agent_id, "error", "control_verify_failed", f"HA service access failed: {exc}", {"service": f"{domain}.{service}", "service_data": data})
                    return self.send_json(502, {"error": str(exc), "service": f"{domain}.{service}", "service_data": data})
            if path == "/api/agents":
                payload = self.read_json()
                error = validate_agent(payload)
                if error:
                    return self.send_json(400, {"error": error})
                requested_control = payload.get("mode") == "control"
                agent = STORE.create_agent({**payload, "mode": "paused"} if requested_control else payload)
                if requested_control:
                    return self.send_json(409, {"error": "New candidates must pass the historical behaviour benchmark before Control can be enabled", "agent_id": agent["id"], "mode": "paused"})
                ENGINE.models.pop(agent["id"], None)
                return self.send_json(201, agent)
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_PATCH(self):
        path, _, _ = self.path.partition("?")
        try:
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                payload = self.read_json()
                if "mode" in payload and payload["mode"] not in ("shadow", "control", "paused"):
                    return self.send_json(400, {"error": "invalid mode"})
                existing = STORE.get_agent(agent_id)
                if not existing:
                    return self.send_json(404, {"error": "agent not found"})
                error = validate_agent({**existing, **payload})
                if error:
                    return self.send_json(400, {"error": error})
                if payload.get("mode") == "control":
                    if existing.get("training_state") != "qualified":
                        score = existing.get("benchmark_score")
                        score_text = f"{score:.1%}" if score is not None else "not benchmarked"
                        return self.send_json(409, {"error": f"Control requires behaviour benchmark > {float(OPTIONS.get('candidate_benchmark_threshold', .78)):.0%}; candidate is {score_text}. Use Resume to continue from the saved cursor, or Rebuild after changing sensors/context."})
                    try:
                        ENGINE.take_control(existing, refresh=True)
                    except Exception as exc:
                        STORE.event(agent_id, "error", "automation_takeover_failed", str(exc))
                        return self.send_json(502, {"error": f"Control transition failed: {exc}. Successfully disabled automations remain off."})
                agent = STORE.update_agent(agent_id, payload)
                if payload.get("mode") == "control":
                    ENGINE.release_manual_hold(agent_id)
                if not agent:
                    return self.send_json(404, {"error": "agent not found"})
                if any(k in payload for k in ("min_value", "max_value", "input_entities")):
                    ENGINE.models.pop(agent_id, None)
                    ENGINE.runtime.pop(agent_id, None)
                return self.send_json(200, agent)
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def do_DELETE(self):
        path, _, _ = self.path.partition("?")
        try:
            if path.startswith("/api/agents/") and path.endswith("/learning"):
                agent_id = path.split("/")[3]
                if HISTORY is None or not STORE.get_agent(agent_id):
                    return self.send_json(404, {"error": "agent/history engine not found"})
                HISTORY.request_agent_rebuild(agent_id)
                return self.send_json(202, {"ok": True, "state": "training", "message": "Full rebuild scheduled from the beginning of local history"})
            if path.startswith("/api/agents/"):
                agent_id = path.split("/")[3]
                STORE.delete_agent(agent_id)
                ENGINE.models.pop(agent_id, None)
                ENGINE.runtime.pop(agent_id, None)
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "not_found"})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})


def main():
    global HISTORY
    try:
        nice_by = int(OPTIONS.get("process_nice", 10))
        if nice_by > 0 and hasattr(os, "nice"):
            os.nice(nice_by)
            print(f"Adaptive AI process niceness increased by {nice_by}; Home Assistant keeps CPU priority", flush=True)
    except Exception as exc:
        print(f"[startup] Could not adjust process niceness: {exc}", flush=True)
    ENGINE.start()
    event_stream = HAEventStream(ENGINE)
    event_stream.start()
    HISTORY = HistoryManager(ENGINE)
    HISTORY.start()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    print("Adaptive AI UI listening on :8099", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ENGINE.stop_event.set()
        ENGINE.control_workers.shutdown(wait=False, cancel_futures=True)
        ENGINE.poll_worker.shutdown(wait=False, cancel_futures=True)
        if HISTORY is not None:
            HISTORY.stop_event.set()
        event_stream.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
