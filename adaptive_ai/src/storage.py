from itertools import islice
from contextlib import contextmanager
from collections import deque
import json
import os
import sqlite3
import sys
from telemetry import RUNTIME_DEBUG
import threading
import time
import uuid
from settings import (DATA_DIR, DB_PATH, clamp, iso_now, now_ts)

class Store:
    def __init__(self, path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self._meta_lock = threading.RLock()
        self._connection_session = threading.local()
        self._wal_keeper_lock = threading.RLock()
        self._wal_keeper = None
        self._wal_keeper_starts = 0
        self._wal_keeper_stops = 0
        self._wal_checkpoint_worker = None
        # Diagnostic events are operational telemetry, not Store transaction state.
        # Keep their tiny RAM ring independently accessible while a large microSD write
        # owns self.lock. This prevents /api/events and diagnostic producers from being
        # serialized behind history/model persistence.
        self._event_lock = threading.RLock()
        self._agent_index_revision = 0
        # Small, frequently-read metadata and diagnostic events are RAM-first. The SD
        # card remains the durable backing store, but unchanged metadata and one-row event
        # commits must not sit on realtime paths.
        self._meta_cache = {}
        self._event_buffer = deque(maxlen=4096)
        self._event_recent = deque(maxlen=5000)
        self._event_buffer_dropped = 0
        self._event_buffer_first_at = None
        self._event_buffer_seq = 0
        self._event_prune_batches = 0
        # Process-isolated historical workers may install a publication guard and
        # request keyset-paged archive reads. Both are process-local switches: the
        # realtime process keeps the ordinary Store contract unchanged.
        self.training_publish_guard = None
        self.checkpointed_archive_reads = False
        # Isolated historical workers attach to an already-initialized parent database.
        # Re-running schema DDL/migrations for every 6 h replay chunk competes with the
        # realtime writer across processes and can turn normal WAL contention into
        # sqlite3.OperationalError("database is locked"). Worker bootstrap is therefore
        # read-only: the authoritative realtime parent owns schema/migration startup.
        self.training_worker_process = (
            str(os.environ.get("ADAPTIVE_AI_TRAINING_WORKER", "")).strip() == "1"
        )
        if self.training_worker_process:
            self._load_meta_cache()
        else:
            self._init()
            self._load_meta_cache()
            self._load_recent_events()
            self.migrate_models()

    def touch_agent_index(self):
        # Lightweight in-process invalidation for the realtime routing cache.
        # Durable safety still lives in Executor; this only avoids polling all agent rows.
        with self.lock:
            self._agent_index_revision = int(self._agent_index_revision) + 1
            return self._agent_index_revision

    def start_wal_keeper(self):
        """Keep WAL attached during runtime without a transaction or shared SQL use.

        Ordinary short-lived connections still own their independent transactions.
        The idle, query-only connection prevents last-close checkpoint/cleanup churn.
        Start explicitly at runtime composition, not in every component/test Store.
        """
        if self.training_worker_process:
            return False
        with self._wal_keeper_lock:
            if self._wal_keeper is not None:
                return False
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                         check_same_thread=False)
            try:
                connection.execute("PRAGMA query_only=ON")
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise RuntimeError("WAL keeper requires an initialized WAL database")
                # sqlite3.connect alone is lazy. Read and exhaust a database cursor to
                # attach the WAL index, leaving no reader snapshot to starve checkpoints.
                cursor = connection.execute("SELECT count(*) FROM sqlite_master")
                cursor.fetchall()
                cursor.close()
            except BaseException:
                connection.close()
                raise
            self._wal_keeper = connection
            self._wal_keeper_starts += 1
            return True

    def stop_wal_keeper(self):
        """Release after final durability drains; idempotent across shutdown paths."""
        with self._wal_keeper_lock:
            connection = self._wal_keeper
            if connection is None:
                return False
            connection.close()
            self._wal_keeper = None
            self._wal_keeper_stops += 1
            return True

    def sqlite_snapshot(self):
        """RAM-only diagnostics, never run SQL from the debug export."""
        with self._wal_keeper_lock:
            return dict(version=sqlite3.sqlite_version,
                        wal_keeper_active=self._wal_keeper is not None,
                        wal_keeper_starts=self._wal_keeper_starts,
                        wal_keeper_stops=self._wal_keeper_stops,
                        wal_checkpoint=(self._wal_checkpoint_worker.snapshot()
                                        if self._wal_checkpoint_worker is not None else None))

    def start_wal_checkpoint(self):
        if self.training_worker_process:
            return False
        from wal_checkpoint import WALCheckpointWorker
        with self._wal_keeper_lock:
            if self._wal_checkpoint_worker is None:
                self._wal_checkpoint_worker = WALCheckpointWorker(self)
            worker = self._wal_checkpoint_worker
        return worker.start()

    def stop_wal_checkpoint(self):
        if self._wal_checkpoint_worker is not None:
            self._wal_checkpoint_worker.stop()

    @contextmanager
    def background_sqlite(self):
        """Bound busy waits for retryable background batches, per thread only.

        This does not alter explicit durability barriers or training worker writes.
        It limits SQLite contention, not Store lock wait or slow physical I/O.
        """
        previous = getattr(self._connection_session, "busy_timeout_ms", None)
        self._connection_session.busy_timeout_ms = 250
        try:
            yield
        finally:
            if previous is None:
                del self._connection_session.busy_timeout_ms
            else:
                self._connection_session.busy_timeout_ms = previous

    @contextmanager
    def conn(self):
        borrowed = getattr(self._connection_session, "connection", None)
        reuse = borrowed is not None and not borrowed.in_transaction
        busy_timeout_ms = getattr(self._connection_session, "busy_timeout_ms", None)
        if busy_timeout_ms is None:
            busy_timeout_ms = 60000 if self.training_worker_process else 30000
        trace = None
        if RUNTIME_DEBUG.enabled and not getattr(self._connection_session, "opening_session", False):
            frame = sys._getframe(2)
            trace = RUNTIME_DEBUG.begin(
                "sqlite_transaction", caller=f"{frame.f_globals.get('__name__')}.{frame.f_code.co_name}:{frame.f_lineno}",
                busy_timeout_ms=busy_timeout_ms, borrowed=reuse,
                sqlite_version=sqlite3.sqlite_version,
            )
        c = borrowed if reuse else None
        previous_timeout = None
        status, error_fields = "ok", {}
        phase = "connect"
        timings = {}
        checkpoint_pages = None
        started = time.perf_counter() if trace else 0.0
        body_started = commit_started = started
        try:
            if reuse and busy_timeout_ms != getattr(self._connection_session, "connection_busy_timeout_ms", None):
                previous_timeout = int(c.execute("PRAGMA busy_timeout").fetchone()[0])
            elif not reuse:
                c = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000.0)
                c.row_factory = sqlite3.Row
            phase = "configure"
            checkpoint_worker = self._wal_checkpoint_worker
            checkpoint_pages = (0 if checkpoint_worker is not None and checkpoint_worker.active else 1000)
            if not reuse and checkpoint_pages == 0:
                c.execute("PRAGMA wal_autocheckpoint=0")
            elif reuse and checkpoint_worker is not None:
                if getattr(self._connection_session, "checkpoint_pages", None) != checkpoint_pages:
                    c.execute(f"PRAGMA wal_autocheckpoint={checkpoint_pages}")
                    self._connection_session.checkpoint_pages = checkpoint_pages
            if not reuse or previous_timeout is not None:
                c.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            if not reuse:
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("PRAGMA temp_store=MEMORY")
                c.execute("PRAGMA cache_size=-16384")
                try:
                    c.execute("PRAGMA mmap_size=67108864")
                except sqlite3.DatabaseError:
                    pass
            with c:
                if trace:
                    body_started = time.perf_counter()
                    timings["prepare_ms"] = round((body_started - started) * 1000, 3)
                phase = "body"
                yield c
                if trace:
                    commit_started = time.perf_counter()
                    timings["body_ms"] = round((commit_started - body_started) * 1000, 3)
                phase = "commit"
            if trace:
                timings["commit_ms"] = round((time.perf_counter() - commit_started) * 1000, 3)
        except BaseException as exc:
            status = "error"
            if trace:
                metric, phase_start = (
                    ("body_ms", body_started) if phase == "body" else
                    ("commit_ms", commit_started) if phase == "commit" else
                    ("prepare_ms", started)
                )
                timings[metric] = round((time.perf_counter() - phase_start) * 1000, 3)
            error_fields = dict(error_type=type(exc).__name__, phase=phase,
                                sqlite_errorcode=getattr(exc, "sqlite_errorcode", None),
                                sqlite_errorname=getattr(exc, "sqlite_errorname", None))
            raise
        finally:
            close_started = time.perf_counter() if trace else 0.0
            try:
                if reuse and previous_timeout is not None:
                    c.execute(f"PRAGMA busy_timeout={previous_timeout}")
                elif c is not None and not reuse:
                    c.close()
            except BaseException as exc:
                status = "error"
                error_fields = dict(error_type=type(exc).__name__, phase="close",
                                    sqlite_errorcode=getattr(exc, "sqlite_errorcode", None),
                                    sqlite_errorname=getattr(exc, "sqlite_errorname", None))
                raise
            finally:
                if trace:
                    timings["close_ms"] = round((time.perf_counter() - close_started) * 1000, 3)
                RUNTIME_DEBUG.end(trace, status=status, autocheckpoint_pages=checkpoint_pages,
                                  **error_fields, **timings)

    @contextmanager
    def connection_session(self):
        """Reuse one thread-owned connection without merging transactions.

        conn() blocks still commit/rollback independently. A call nested inside
        an active write transaction gets its own connection as before, so it
        cannot commit or read another caller's uncommitted changes. The session owns
        only connection lifetime, and acquires no long-lived Store or writer lock.
        """
        existing = getattr(self._connection_session, "connection", None)
        if existing is not None:
            yield existing
            return
        trace = None
        if RUNTIME_DEBUG.enabled:
            frame = sys._getframe(2)
            busy_timeout_ms = getattr(self._connection_session, "busy_timeout_ms", None)
            if busy_timeout_ms is None:
                busy_timeout_ms = 60000 if self.training_worker_process else 30000
            trace = RUNTIME_DEBUG.begin(
                "sqlite_session_open",
                caller=f"{frame.f_globals.get('__name__')}.{frame.f_code.co_name}:{frame.f_lineno}",
                busy_timeout_ms=busy_timeout_ms, sqlite_version=sqlite3.sqlite_version,
            )
        self._connection_session.opening_session = True
        try:
            with self.conn() as connection:
                self._connection_session.opening_session = False
                RUNTIME_DEBUG.end(trace)
                trace = None
                self._connection_session.connection = connection
                self._connection_session.connection_busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
                self._connection_session.checkpoint_pages = int(connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0])
                try:
                    yield connection
                finally:
                    del self._connection_session.connection
                    del self._connection_session.connection_busy_timeout_ms
                    del self._connection_session.checkpoint_pages
        except BaseException as exc:
            RUNTIME_DEBUG.end(trace, status="error", error_type=type(exc).__name__,
                              phase="session_open",
                              sqlite_errorcode=getattr(exc, "sqlite_errorcode", None),
                              sqlite_errorname=getattr(exc, "sqlite_errorname", None))
            raise
        finally:
            self._connection_session.opening_session = False

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
                CREATE INDEX IF NOT EXISTS idx_agents_target_created ON agents(target_entity, created_at);
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
                CREATE TABLE IF NOT EXISTS entity_history_revision (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    revision INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO entity_history_revision(id,revision) VALUES(1,0);
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
            self._ensure_column(c, "agents", "additional_signal", "TEXT NOT NULL DEFAULT 'null'")
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
            # Stage 08 causal replay distinguishes HA event time from local receive time.
            # Legacy/Recorder-imported rows keep NULL here: their receive time is unknown
            # and replay falls back to event time without fabricating provenance.
            self._ensure_column(c, "entity_history", "received_ts", "REAL")
            # 0.14.123: each sanctioned history mutation receives one durable,
            # cross-process revision. Existing rows remain revision 0 until touched.
            self._ensure_column(
                c, "entity_history", "mutation_revision",
                "INTEGER NOT NULL DEFAULT 0",
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_entity_history_received_ts ON entity_history(received_ts)")
            # Defensive triggers cover any legacy/raw SQL writer that bypasses the Store
            # helpers. Normal archive_upsert/archive_batch supply a non-zero revision, so
            # these triggers stay cold on the performance-sensitive normal path.
            c.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_entity_history_insert_revision
                AFTER INSERT ON entity_history
                WHEN NEW.mutation_revision=0
                BEGIN
                  UPDATE entity_history_revision
                     SET revision=revision+1 WHERE id=1;
                  UPDATE entity_history
                     SET mutation_revision=(
                       SELECT revision FROM entity_history_revision WHERE id=1
                     )
                   WHERE id=NEW.id;
                END;
                CREATE TRIGGER IF NOT EXISTS trg_entity_history_update_revision
                AFTER UPDATE OF received_ts,state,attributes_json,context_user_id,source
                ON entity_history
                WHEN NEW.mutation_revision=OLD.mutation_revision
                BEGIN
                  UPDATE entity_history_revision
                     SET revision=revision+1 WHERE id=1;
                  UPDATE entity_history
                     SET mutation_revision=(
                       SELECT revision FROM entity_history_revision WHERE id=1
                     )
                   WHERE id=NEW.id;
                END;
                """
            )
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

    def _load_meta_cache(self):
        with self.conn() as c:
            rows = c.execute("SELECT key,value FROM app_meta").fetchall()
        with self._meta_lock:
            self._meta_cache = {str(row[0]): str(row[1]) for row in rows}

    def _load_recent_events(self):
        # One startup read warms the UI event feed. Periodic /api/events polls then stay
        # entirely in RAM until process restart.
        with self.conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 5000").fetchall()
        recent = []
        for row in reversed(rows):
            item = dict(row)
            raw = item.pop("data_json", None)
            item["data"] = json.loads(raw) if raw else None
            recent.append(item)
        with self._event_lock:
            self._event_recent.extend(recent)

    @staticmethod
    def _ensure_column(c, table, column, ddl):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def list_agents(self):
        """UI/detail path: aggregate feedback/history tables once, not once per agent."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT a.*,
                       COALESCE(f.feedback_count, 0) AS feedback_count,
                       COALESCE(f.positive_count, 0) AS positive_count,
                       COALESCE(f.negative_count, 0) AS negative_count,
                       f.average_reward AS average_reward,
                       COALESCE(h.historical_count, 0) AS historical_count,
                       h.historical_average_reward AS historical_average_reward
                FROM agents a
                LEFT JOIN (
                    SELECT agent_id,
                           COUNT(*) AS feedback_count,
                           SUM(CASE WHEN reward > 0 THEN 1 ELSE 0 END) AS positive_count,
                           SUM(CASE WHEN reward < 0 THEN 1 ELSE 0 END) AS negative_count,
                           AVG(reward) AS average_reward
                    FROM rl_feedback
                    GROUP BY agent_id
                ) f ON f.agent_id=a.id
                LEFT JOIN (
                    SELECT agent_id,
                           COUNT(*) AS historical_count,
                           AVG(reward) AS historical_average_reward
                    FROM historical_experiences
                    GROUP BY agent_id
                ) h ON h.agent_id=a.id
                ORDER BY a.created_at
                """
            ).fetchall()
        return [self._agent_dict(r) for r in rows]

    def list_agent_configs(self):
        """Hot path: no COUNT/AVG scans over history on every motion event."""
        with self.conn() as c:
            return [self._agent_dict(r) for r in c.execute('SELECT * FROM agents ORDER BY created_at')]

    def list_agent_configs_for_target(self, target_entity):
        """Fresh configs for every property of one entity, without decoding other agents."""
        with self.conn() as c:
            rows = c.execute(
                'SELECT * FROM agents WHERE target_entity=? ORDER BY created_at',
                (target_entity,),
            ).fetchall()
        return [self._agent_dict(row) for row in rows]

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
        from additional_signal import normalize
        d["additional_signal"] = normalize(json.loads(d.get("additional_signal") or "null"))
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
        from additional_signal import normalize
        signal = normalize(payload.get("additional_signal"))
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
            c.execute("UPDATE agents SET additional_signal=? WHERE id=?", (json.dumps(signal), agent_id))
        timing = {k: payload[k] for k in ("ack_timeout", "settling_seconds", "manual_hold_seconds") if k in payload}
        if timing:
            self.update_agent(agent_id, timing)
        self.touch_agent_index()
        self.event(agent_id, "info", "agent_created", f"Created RL agent {payload['name']}", payload)
        return self.get_agent(agent_id)

    def update_agent(self, agent_id, payload):
        if "additional_signal" in payload:
            from additional_signal import normalize
            payload = {**payload, "additional_signal": normalize(payload["additional_signal"])}
        allowed = {
            "name": str, "mode": str, "min_value": float, "max_value": float,
            "confidence_threshold": float, "deadband": float, "action_interval": float,
            "exploration_step": float, "exploration_interval": float,
            "ack_timeout": float, "settling_seconds": float, "manual_hold_seconds": float,
            "micro_exploration": int, "enabled": int,
            "input_entities": json.dumps,
            "additional_signal": json.dumps,
        }
        updates, values = [], []
        reset_model = any(k in payload for k in ("min_value", "max_value", "input_entities", "additional_signal"))
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
            # Stage 8 makes the structural cause explicit so the next Train/Rebuild
            # cannot silently collapse all incompatibilities into one generic replay.
            action_space_changed = any(k in payload for k in ("min_value", "max_value"))
            feature_mask_changed = "input_entities" in payload
            rebuild_reason = (
                "action_space_change"
                if action_space_changed
                else "feature_mask_change"
            )
            detail = {
                "reason": "Context or action range changed; press Train",
                "rebuild_reason": rebuild_reason,
                "feature_mask_changed": bool(feature_mask_changed),
                "action_space_changed": bool(action_space_changed),
            }
            self.set_training_state(
                agent_id, "needs_retrain", detail=detail
            )
            with self.conn() as c:
                c.execute(
                    'UPDATE agents SET training_cursor_ts=NULL,training_progress=0 WHERE id=?',
                    (agent_id,),
                )
        if updates:
            self.touch_agent_index()
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
        self.touch_agent_index()
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

    def save_models_batch(self, rows):
        """Persist latest model snapshots for multiple agents in one transaction."""
        latest = {}
        for agent_id, model in list(rows or []):
            latest[str(agent_id)] = dict(model)
        if not latest:
            return 0
        ids = list(latest)
        placeholders = ",".join("?" for _ in ids)
        with self.lock, self.conn() as c:
            guard = getattr(self, "training_publish_guard", None)
            if callable(guard):
                # Validate immediately before the atomic model upsert. A clean worker
                # therefore cannot publish a model for an agent whose configuration
                # changed after the versioned training job was created.
                for agent_id in ids:
                    guard(str(agent_id), latest[agent_id])
            watermarks = {
                str(row["agent_id"]): int(row["watermark"] or 0)
                for row in c.execute(
                    f"""SELECT agent_id,COALESCE(MAX(id),0) AS watermark
                        FROM historical_experiences WHERE agent_id IN ({placeholders})
                        GROUP BY agent_id""",
                    ids,
                ).fetchall()
            }
            previous = {
                str(row["agent_id"]): row["model_json"]
                for row in c.execute(
                    f"SELECT agent_id,model_json FROM rl_models WHERE agent_id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            stamp = iso_now()
            packed = []
            for agent_id in ids:
                model = dict(latest[agent_id])
                model["_history_watermark"] = int(watermarks.get(agent_id, 0))
                if agent_id in previous and "_benchmark_counts" not in model:
                    try:
                        model["_benchmark_counts"] = json.loads(previous[agent_id] or "{}").get("_benchmark_counts", {})
                    except Exception:
                        model["_benchmark_counts"] = {}
                packed.append((agent_id, json.dumps(model, separators=(",", ":")), stamp))
            c.executemany(
                """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json, updated_at=excluded.updated_at""",
                packed,
            )
        return len(packed)

    def save_model(self, agent_id, model):
        self.save_models_batch([(agent_id, model)])

    def restore_model_snapshot(self, agent_id, model):
        """Restore an exact pre-job model without recalculating its history watermark."""
        with self.lock, self.conn() as c:
            if model is None:
                c.execute("DELETE FROM rl_models WHERE agent_id=?", (str(agent_id),))
            else:
                c.execute(
                    """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                       model_json=excluded.model_json,updated_at=excluded.updated_at""",
                    (
                        str(agent_id),
                        json.dumps(dict(model), separators=(",", ":")),
                        iso_now(),
                    ),
                )
        self.touch_agent_index()

    def restore_training_chunk_snapshot(self, agent_id, agent, model):
        """Rollback a rejected/aborted isolated chunk to its exact pre-chunk lifecycle."""
        agent = dict(agent or {})
        with self.lock, self.conn() as c:
            if model is None:
                c.execute("DELETE FROM rl_models WHERE agent_id=?", (str(agent_id),))
            else:
                c.execute(
                    """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                       model_json=excluded.model_json,updated_at=excluded.updated_at""",
                    (
                        str(agent_id),
                        json.dumps(dict(model), separators=(",", ":")),
                        iso_now(),
                    ),
                )
            c.execute(
                """UPDATE agents SET training_state=?,mode=?,benchmark_score=?,
                   benchmark_samples=?,benchmark_source=?,benchmark_detail_json=?,
                   benchmark_updated_at=?,training_window_start_ts=?,training_cursor_ts=?,
                   training_window_end_ts=?,training_progress=?,training_updated_at=?
                   WHERE id=?""",
                (
                    str(agent.get("training_state") or "training"),
                    str(agent.get("mode") or "paused"),
                    agent.get("benchmark_score"),
                    int(agent.get("benchmark_samples") or 0),
                    agent.get("benchmark_source"),
                    json.dumps(
                        agent.get("benchmark_detail") or {},
                        separators=(",", ":"), ensure_ascii=False,
                    ),
                    agent.get("benchmark_updated_at"),
                    agent.get("training_window_start_ts"),
                    agent.get("training_cursor_ts"),
                    agent.get("training_window_end_ts"),
                    float(agent.get("training_progress") or 0.0),
                    agent.get("training_updated_at") or iso_now(),
                    str(agent_id),
                ),
            )
        self.touch_agent_index()

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
        self.touch_agent_index()
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
        self.touch_agent_index()

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
            changed = int(cur.rowcount or 0)
        if changed:
            self.touch_agent_index()
        return changed

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
        self.touch_agent_index()
        self.meta_set("candidate_qualification_complete", "0")
        self.event(None, "info", "training_revision", "Rebuilding offline RL policies for the new predictive feature revision", None)

    def event(self, agent_id, level, kind, message, data=None):
        """Queue diagnostic activity in RAM and persist it in coarse batches.

        Event rows are operational diagnostics, not control state. Keeping the newest
        rows visible from RAM lets the UI remain realtime without forcing a WAL commit on
        every informational message. Warnings/errors still trigger prompt durability.
        """
        try:
            now_mono = time.monotonic()
            with self._event_lock:
                self._event_buffer_seq += 1
                row = {
                    "id": None,
                    "_ram_seq": self._event_buffer_seq,
                    "created_at": iso_now(),
                    "agent_id": agent_id,
                    "level": str(level),
                    "kind": str(kind),
                    "message": str(message),
                    "data": data,
                }
                if self._event_buffer.maxlen and len(self._event_buffer) >= self._event_buffer.maxlen:
                    self._event_buffer_dropped += 1
                self._event_buffer.append(row)
                if self._event_buffer_first_at is None:
                    self._event_buffer_first_at = now_mono
                pending = len(self._event_buffer)
                age = now_mono - float(self._event_buffer_first_at or now_mono)
            # Warnings are frequent operational diagnostics (for example negative RL
            # reward) and must not force one WAL transaction each. Only errors bypass
            # coalescing; warning/info rows batch for up to 5 seconds or 64 records.
            if str(level).lower() == "error" or pending >= 64 or age >= 5.0:
                # Diagnostic durability may lag briefly, but realtime inference must
                # never wait behind an unrelated Store transaction.
                # Also bound an external SQLite writer: acquiring the in-process
                # Store lock without waiting alone does not make disk access fast.
                with self.background_sqlite():
                    self.flush_events(nonblocking=True)
        except Exception as exc:
            print(f"[event] {exc}", flush=True)

    def flush_events(self, nonblocking=False):
        # Serialize the SQLite batch with other durable Store writers, but let realtime
        # callers skip the flush rather than wait. Shutdown/explicit boundaries retain
        # the default blocking behavior.
        acquired = self.lock.acquire(blocking=not bool(nonblocking))
        if not acquired:
            return 0
        batch = []
        try:
            with self._event_lock:
                if not self._event_buffer:
                    return 0
                batch = list(self._event_buffer)
                self._event_buffer.clear()
                self._event_buffer_first_at = None
            packed = [
                (row["created_at"], row.get("agent_id"), row["level"], row["kind"],
                 row["message"], json.dumps(row.get("data")) if row.get("data") is not None else None)
                for row in batch
            ]
            with self.conn() as c:
                c.executemany(
                    "INSERT INTO events(created_at,agent_id,level,kind,message,data_json) VALUES(?,?,?,?,?,?)",
                    packed,
                )
                self._event_prune_batches += 1
                if self._event_prune_batches >= 4:
                    c.execute(
                        "DELETE FROM events WHERE id < COALESCE((SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 4999),-1)"
                    )
                    self._event_prune_batches = 0
            with self._event_lock:
                for row in batch:
                    cached = dict(row)
                    cached.pop("_ram_seq", None)
                    self._event_recent.append(cached)
            return len(batch)
        except Exception:
            with self._event_lock:
                for row in reversed(batch):
                    if self._event_buffer.maxlen and len(self._event_buffer) >= self._event_buffer.maxlen:
                        self._event_buffer_dropped += 1
                    self._event_buffer.appendleft(row)
                if self._event_buffer_first_at is None:
                    self._event_buffer_first_at = time.monotonic()
            raise
        finally:
            self.lock.release()

    def list_events(self, limit=100):
        """Return the live diagnostic feed without polling SQLite or Store transaction locks."""
        limit = max(1, int(limit))
        with self._event_lock:
            pending = [dict(row) for row in list(self._event_buffer)[-limit:]]
            remaining = max(0, limit - len(pending))
            durable = [dict(row) for row in list(self._event_recent)[-remaining:]] if remaining else []
        out = []
        for row in reversed(pending):
            row.pop("_ram_seq", None)
            out.append(row)
        out.extend(reversed(durable))
        return out[:limit]


    def find_agent_by_target(self, entity_id, property_name):
        with self.lock, self.conn() as c:
            row = c.execute("SELECT id FROM agents WHERE target_entity=? AND target_property=?", (entity_id, property_name)).fetchone()
        return self.get_agent(row[0]) if row else None

    @staticmethod
    def _next_entity_history_revision(c):
        c.execute(
            "UPDATE entity_history_revision SET revision=revision+1 WHERE id=1"
        )
        row = c.execute(
            "SELECT revision FROM entity_history_revision WHERE id=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("entity_history revision row is missing")
        return int(row[0])

    def archive_window_fingerprint(self, entity_ids, start_ts, end_ts):
        """Cheap mutation guard for one exact target-history overlap.

        COUNT detects deletion. MAX(mutation_revision) detects every sanctioned INSERT
        or UPDATE without invalidating the cache for newer rows outside this window.
        """
        ids = sorted({str(entity_id) for entity_id in (entity_ids or ())})
        start_ts, end_ts = float(start_ts), float(end_ts)
        if not ids or end_ts <= start_ts:
            return (0, 0)
        placeholders = ",".join("?" for _ in ids)
        with self.conn() as c:
            row = c.execute(
                f"""SELECT COUNT(*) AS n,
                           COALESCE(MAX(mutation_revision),0) AS revision
                      FROM entity_history
                     WHERE entity_id IN ({placeholders})
                       AND ts>=? AND ts<?""",
                [*ids, start_ts, end_ts],
            ).fetchone()
        return (int(row["n"] or 0), int(row["revision"] or 0))

    def archive_upsert(self, entity_id, ts, state, attributes=None, user_id=None, source="live"):
        attrs = json.dumps(attributes or {}, separators=(",", ":"), ensure_ascii=False)
        with self.lock, self.conn() as c:
            revision = self._next_entity_history_revision(c)
            c.execute(
                """INSERT INTO entity_history(
                       entity_id,ts,state,attributes_json,context_user_id,source,
                       mutation_revision
                   )
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id,ts) DO UPDATE SET
                     state=excluded.state,
                     attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE entity_history.attributes_json END,
                     context_user_id=COALESCE(excluded.context_user_id, entity_history.context_user_id),
                     source=CASE WHEN excluded.source='ha_history_full' THEN excluded.source ELSE entity_history.source END,
                     mutation_revision=excluded.mutation_revision""",
                (
                    entity_id, float(ts), None if state is None else str(state),
                    attrs, user_id, source, revision,
                ),
            )

    def archive_batch(self, rows):
        if not rows:
            return 0

        def pack(row):
            if len(row) == 6:
                entity_id, ts, state, attrs, user_id, source = row
                received_ts = None
            elif len(row) == 7:
                entity_id, ts, state, attrs, user_id, source, received_ts = row
            else:
                raise ValueError("archive row must contain 6 legacy fields or 7 fields with received_ts")
            received_ts = float(received_ts) if received_ts is not None else None
            return (
                entity_id, float(ts), received_ts, None if state is None else str(state),
                json.dumps(attrs or {}, separators=(",", ":"), ensure_ascii=False),
                user_id, source,
            )

        packed = [pack(row) for row in rows]
        with self.lock, self.conn() as c:
            revision = self._next_entity_history_revision(c)
            versioned = [(*item, revision) for item in packed]
            c.executemany(
                """INSERT INTO entity_history
                   (entity_id,ts,received_ts,state,attributes_json,context_user_id,source,
                    mutation_revision)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(entity_id,ts) DO UPDATE SET
                     received_ts=CASE
                       WHEN entity_history.source='live' THEN entity_history.received_ts
                       WHEN excluded.source='live' THEN excluded.received_ts
                       ELSE COALESCE(entity_history.received_ts, excluded.received_ts)
                     END,
                     state=excluded.state,
                     attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE entity_history.attributes_json END,
                     context_user_id=COALESCE(excluded.context_user_id, entity_history.context_user_id),
                     source=CASE WHEN excluded.source='ha_history_full' THEN excluded.source ELSE entity_history.source END,
                     mutation_revision=excluded.mutation_revision""",
                versioned,
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
        """Stream history rows without materializing the archive.

        Normal runtime keeps one reader for throughput. Isolated training workers set
        checkpointed_archive_reads so each batch is a short SQLite snapshot and cannot
        pin WAL checkpoints for an entire multi-minute history scan.
        """
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        predicate = ((" WHERE " + " AND ".join(where)) if where else "")
        size = max(100, int(chunk_size))

        if not bool(getattr(self, "checkpointed_archive_reads", False)):
            sql = "SELECT * FROM entity_history" + predicate + " ORDER BY ts,id"
            with self.conn() as c:
                cursor = c.execute(sql, vals)
                while True:
                    batch = cursor.fetchmany(size)
                    if not batch:
                        break
                    for row in batch:
                        yield dict(row)
            return

        last_ts = None
        last_id = None
        base_where = list(where)
        base_vals = list(vals)
        while True:
            page_where = list(base_where)
            page_vals = list(base_vals)
            if last_ts is not None:
                page_where.append("(ts>? OR (ts=? AND id>?))")
                page_vals.extend([float(last_ts), float(last_ts), int(last_id)])
            page_sql = (
                "SELECT * FROM entity_history"
                + ((" WHERE " + " AND ".join(page_where)) if page_where else "")
                + " ORDER BY ts,id LIMIT ?"
            )
            page_vals.append(size)
            with self.conn() as c:
                batch = c.execute(page_sql, page_vals).fetchall()
            if not batch:
                break
            for row in batch:
                item = dict(row)
                last_ts, last_id = float(item["ts"]), int(item["id"])
                yield item
            if len(batch) < size:
                break

    def archive_change_iter(self, start_ts=None, end_ts=None, entity_ids=None, chunk_size=2000):
        """Stream only effective per-entity state/attribute changes.

        Feature screening previously pulled every Recorder row into Python and discarded
        consecutive duplicates there. SQLite can perform the identical per-entity LAG
        comparison in C, materially reducing Python/GIL work for chatty sensors while
        preserving the first row in the requested interval as a change.
        """
        where, vals = [], []
        if start_ts is not None:
            where.append("ts>=?"); vals.append(float(start_ts))
        if end_ts is not None:
            where.append("ts<=?"); vals.append(float(end_ts))
        ids = sorted(set(entity_ids or []))
        if ids:
            where.append("entity_id IN (%s)" % ",".join("?" for _ in ids)); vals.extend(ids)
        predicate = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source
            FROM (
                SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source,
                       LAG(id) OVER (PARTITION BY entity_id ORDER BY ts,id) AS previous_id,
                       LAG(state) OVER (PARTITION BY entity_id ORDER BY ts,id) AS previous_state,
                       LAG(attributes_json) OVER (PARTITION BY entity_id ORDER BY ts,id) AS previous_attributes_json
                FROM entity_history{predicate}
            )
            WHERE previous_id IS NULL
               OR state IS NOT previous_state
               OR attributes_json IS NOT previous_attributes_json
            ORDER BY ts,id
        """
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

    @staticmethod
    def _pack_historical_experience(agent_id, target_history_id, action_index, action_value,
                                    reward, dwell_seconds, features, user_id=None, created_at=None):
        raw = json.dumps({str(k): v for k, v in features.items()}, separators=(",", ":"))
        return (
            str(agent_id), int(target_history_id), created_at or iso_now(), int(action_index),
            float(action_value), float(reward), float(dwell_seconds), raw, user_id,
        )

    def historical_experience_target_ids(self, agent_id):
        """Small dedup index used by one offline replay job.

        Reading only the unique target-history keys once is much cheaper than opening and
        committing one SQLite transaction for every dwell during replay.
        """
        with self.conn() as c:
            return {
                int(row[0]) for row in c.execute(
                    "SELECT target_history_id FROM historical_experiences WHERE agent_id=?",
                    (str(agent_id),),
                ).fetchall()
            }

    def add_historical_experiences_batch(self, rows):
        """Persist many replay experiences in one WAL transaction.

        The caller still owns policy-ordering semantics; this method changes persistence
        granularity only. INSERT OR IGNORE keeps the historical uniqueness contract.
        """
        rows = list(rows or [])
        if not rows:
            return 0
        created_at = iso_now()
        packed = [
            self._pack_historical_experience(
                row["agent_id"], row["target_history_id"], row["action_index"],
                row["action_value"], row["reward"], row["dwell_seconds"],
                row["features"], row.get("user_id"), created_at=created_at,
            )
            for row in rows
        ]
        with self.lock, self.conn() as c:
            before = int(c.total_changes)
            c.executemany(
                """INSERT OR IGNORE INTO historical_experiences
                   (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                packed,
            )
            return max(0, int(c.total_changes) - before)

    def add_historical_experience(self, agent_id, target_history_id, action_index, action_value, reward, dwell_seconds, features, user_id=None):
        packed = self._pack_historical_experience(
            agent_id, target_history_id, action_index, action_value,
            reward, dwell_seconds, features, user_id,
        )
        with self.lock, self.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO historical_experiences
                   (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                packed,
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
        key = str(key)
        with self._meta_lock:
            return self._meta_cache.get(key, default)

    def migrate_models(self):
        """Storage-level legacy guard; current feature contracts migrate themselves later.

        This layer runs before runtime composition, so it must not hard-code the newest
        policy/schema pair. Doing that would misclassify a newer valid model on every
        restart before the observation contract has a chance to inspect it.
        """
        with self.lock, self.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS model_backups (agent_id TEXT, model_json TEXT, saved_at TEXT, PRIMARY KEY(agent_id,saved_at))')
            for row in c.execute('SELECT agent_id,model_json FROM rl_models'):
                try:
                    raw = json.loads(row['model_json'])
                    policy_version = int(raw.get('version') or 0)
                    schema_version = int((raw.get('schema') or {}).get('version') or 0)
                    # v10/schema11 was the first explicit modern contract. Newer
                    # contracts are intentionally left to observation_contract.py,
                    # which owns exact compatibility after composition is installed.
                    modern = policy_version >= 10 and schema_version >= 11
                except (ValueError, TypeError, AttributeError):
                    modern = False
                if not modern:
                    c.execute('INSERT OR IGNORE INTO model_backups VALUES (?,?,?)',
                              (row['agent_id'], row['model_json'], 'migration-0.9.0'))
                    c.execute("UPDATE agents SET training_state='needs_retrain',mode='paused',training_cursor_ts=NULL,training_progress=0 WHERE id=?", (row['agent_id'],))
            c.execute("UPDATE agents SET training_state='waiting' WHERE training_state='paused' AND benchmark_score IS NULL AND training_cursor_ts IS NULL AND id NOT IN (SELECT agent_id FROM rl_models)")

    def meta_set(self, key, value):
        key, value = str(key), str(value)
        with self.lock:
            with self._meta_lock:
                if self._meta_cache.get(key) == value:
                    return False
            with self.conn() as c:
                c.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            with self._meta_lock:
                self._meta_cache[key] = value
        return True


STORE = Store(DB_PATH)
