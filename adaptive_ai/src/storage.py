from itertools import islice
from contextlib import contextmanager
import json
import sqlite3
import threading
import uuid
from settings import (DATA_DIR, DB_PATH, clamp, iso_now, now_ts)

class Store:
    def __init__(self, path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self._init()
        self.migrate_models()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=FILE")
        c.execute("PRAGMA cache_size=-2048")
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

    def list_agent_configs(self):
        """Hot path: no COUNT/AVG scans over history on every motion event."""
        with self.conn() as c:
            return [self._agent_dict(r) for r in c.execute('SELECT * FROM agents ORDER BY created_at')]

    def get_agent_config(self, agent_id):
        with self.conn() as c:
            row = c.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
        return self._agent_dict(row) if row else None

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
            c.execute("UPDATE agents SET training_state='waiting', mode='paused', training_progress=0, training_updated_at=? WHERE id=?", (iso_now(), agent_id))
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
            # Configuration edits invalidate inference but do not start a phantom job.
            self.set_training_state(agent_id, 'needs_retrain', detail={'reason': 'Context or action range changed; press Train'})
            with self.conn() as c:
                c.execute('UPDATE agents SET training_cursor_ts=NULL,training_progress=0 WHERE id=?', (agent_id,))
        self.event(agent_id, "info", "agent_updated", "Agent settings updated", payload)
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id):
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM rl_feedback WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM historical_experiences WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM rl_models WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            for table in ('teaching_labels', 'decision_history'):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f"DELETE FROM {table} WHERE agent_id=?", (agent_id,))
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
        with self.lock, self.conn() as c:
            model = dict(model)
            model['_history_watermark'] = c.execute('SELECT COALESCE(MAX(id),0) FROM historical_experiences WHERE agent_id=?', (agent_id,)).fetchone()[0]
            previous = c.execute('SELECT model_json FROM rl_models WHERE agent_id=?', (agent_id,)).fetchone()
            if previous and '_benchmark_counts' not in model:
                model['_benchmark_counts'] = json.loads(previous[0]).get('_benchmark_counts', {})
            raw = json.dumps(model, separators=(",", ":"))
            c.execute(
                """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json, updated_at=excluded.updated_at""",
                (agent_id, raw, iso_now()),
            )

    def discard_uncommitted_experiences(self, agent_id):
        model = self.get_model(agent_id) or {}
        with self.lock, self.conn() as c:
            c.execute('DELETE FROM historical_experiences WHERE agent_id=? AND id>?',
                      (agent_id, int(model.get('_history_watermark', 0))))

    def clear_learning(self, agent_id):
        """Full rebuild reset. Raw entity_history is intentionally retained."""
        with self.lock, self.conn() as c:
            # User feedback is an audit/preference record, retained across rebuilds.
            c.execute("DELETE FROM historical_experiences WHERE agent_id=?", (agent_id,))
            c.execute("DELETE FROM rl_models WHERE agent_id=?", (agent_id,))
            c.execute("""UPDATE agents SET training_state='training', mode='paused', benchmark_score=NULL, benchmark_samples=0,
                       benchmark_source=NULL, benchmark_detail_json='{}', benchmark_updated_at=NULL,
                       training_cursor_ts=NULL, training_window_start_ts=NULL, training_window_end_ts=NULL,
                       training_progress=0, training_updated_at=? WHERE id=?""", (iso_now(), agent_id))
        self.event(agent_id, "warning", "learning_reset",
                   "Full rebuild reset: policy/benchmark/cursor cleared; local raw history retained", None)

    def set_training_state(self, agent_id, state, score=None, samples=0, source=None, detail=None,
                           demote_control=False, shadow_after_completion=False):
        state = str(state or "training")
        if state not in ("training", "qualified", "paused", "needs_retrain", "waiting"):
            raise ValueError("invalid training state")
        raw = json.dumps(detail or {}, separators=(",", ":"), ensure_ascii=False)
        # Training is offline-only. Control qualification and Shadow inference are
        # intentionally separate. A completed pass with a persisted model may remain in
        # Shadow even when it did not clear the Control benchmark; interrupted/error
        # PAUSED states stay fully paused unless the caller explicitly marks completion.
        with self.lock, self.conn() as c:
            has_model = c.execute("SELECT 1 FROM rl_models WHERE agent_id=? LIMIT 1", (agent_id,)).fetchone() is not None
            completed_shadow = bool(shadow_after_completion and has_model and state in ("qualified", "paused"))
            mode = "shadow" if state == "qualified" or completed_shadow else "paused"
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
        # Finalization only needs the agent row. Avoid COUNT/AVG subqueries over the
        # historical experience table exactly when replay has just finished.
        agent = self.get_agent_config(agent_id)
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

    def archive_rows(self, start_ts=None, end_ts=None, entity_id=None, limit=20000):
        rows = list(islice(self.archive_iter(start_ts,end_ts,[entity_id] if entity_id else None), limit+1))
        if len(rows) > limit:
            raise ValueError('Use archive_iter for large histories')
        return rows

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
        rows = list(islice(self.archive_iter(start_ts, end_ts, entity_ids, chunk_size=256), 20001))
        if len(rows)>20000:
            raise ValueError('Use archive_iter for large histories')
        return rows

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

    def list_historical_experiences(self, agent_id, limit=1000):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM historical_experiences WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, min(10000, max(1, int(limit))))).fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["features"]={int(k):v for k,v in json.loads(d.pop("features_json")).items()}; out.append(d)
        return out

    def meta_get(self, key, default=None):
        with self.conn() as c:
            r=c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def migrate_models(self):
        """Additive, idempotent migration; never replay or erase historical data."""
        with self.lock, self.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS model_backups (agent_id TEXT, model_json TEXT, saved_at TEXT, PRIMARY KEY(agent_id,saved_at))')
            for row in c.execute('SELECT agent_id,model_json FROM rl_models'):
                try:
                    raw = json.loads(row['model_json'])
                    valid = raw.get('version') == 10 and raw.get('schema', {}).get('version') == 11
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    c.execute('INSERT OR IGNORE INTO model_backups VALUES (?,?,?)',
                              (row['agent_id'], row['model_json'], 'migration-0.9.0'))
                    c.execute("UPDATE agents SET training_state='needs_retrain',mode='paused',training_cursor_ts=NULL,training_progress=0 WHERE id=?", (row['agent_id'],))
            c.execute("UPDATE agents SET training_state='waiting' WHERE training_state='paused' AND benchmark_score IS NULL AND training_cursor_ts IS NULL AND id NOT IN (SELECT agent_id FROM rl_models)")

    def meta_set(self, key, value):
        with self.lock, self.conn() as c:
            c.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


STORE = Store(DB_PATH)
