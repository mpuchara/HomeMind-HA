"""Durable provenance for HA events, policy decisions and learning experiences.

Contract v1 is additive. Missing provenance is represented as ``unknown``; migrations
never infer a historical user/automation origin from incomplete old rows. This module
also owns the persistent command/context evidence used to recognise HomeMind echoes
across a process restart.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections import OrderedDict

from context import target_value
from control import same_value
from settings import iso_now, now_ts

CONTRACT_VERSION = 1
UNKNOWN = "unknown"


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def stable_event_id(entity_id, event_time, context_id=None, parent_id=None, state=None):
    """Stable key for an HA state event when HA does not provide an event UUID."""
    payload = [str(entity_id), float(event_time), context_id or "", parent_id or "",
               None if state is None else state.get("state")]
    digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:32]
    return "ha:" + digest


class ProvenanceJournal:
    def __init__(self, store, clock=now_ts):
        self.store = store
        self.clock = clock
        self._command_cache_lock = threading.RLock()
        self._command_cache = {}
        self._command_context_cache = {}
        self._event_cache_lock = threading.RLock()
        self._event_cache = OrderedDict()
        self._history_event_cache = OrderedDict()
        self._pending_events = OrderedDict()
        self._event_cache_limit = 8192
        self._pending_event_limit = 8192
        self._dropped_pending_events = 0
        self._migrate()
        self._load_recent_event_cache()
        self._load_active_command_cache()

    @staticmethod
    def _columns(c, table):
        return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self):
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS provenance_events (
                    event_id TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    event_time REAL NOT NULL,
                    received_time REAL NOT NULL,
                    entity_id TEXT NOT NULL,
                    context_id TEXT,
                    context_parent_id TEXT,
                    user_id TEXT,
                    source TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'unknown',
                    state_json TEXT,
                    processed_time REAL
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_events_entity_time
                    ON provenance_events(entity_id,event_time);
                CREATE INDEX IF NOT EXISTS idx_provenance_events_context
                    ON provenance_events(context_id,context_parent_id);

                CREATE TABLE IF NOT EXISTS provenance_history_links (
                    entity_id TEXT NOT NULL,
                    event_time REAL NOT NULL,
                    event_id TEXT NOT NULL,
                    PRIMARY KEY(entity_id,event_time)
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_history_event
                    ON provenance_history_links(event_id);

                CREATE TABLE IF NOT EXISTS provenance_decisions (
                    decision_id TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    created_time REAL NOT NULL,
                    agent_id TEXT NOT NULL,
                    generation_id TEXT,
                    trigger_event_id TEXT,
                    model_version INTEGER,
                    model_revision TEXT,
                    schema_version INTEGER,
                    schema_revision TEXT,
                    reward_version INTEGER,
                    feature_manifest_json TEXT NOT NULL,
                    allowed_actions_json TEXT NOT NULL,
                    chosen_action REAL,
                    model_desired REAL,
                    teaching_id INTEGER,
                    teaching_desired REAL,
                    experiment_id TEXT,
                    episode_id TEXT,
                    action_probability REAL,
                    dispatch_status TEXT,
                    dispatch_reason TEXT,
                    dispatch_time REAL,
                    dispatch_value REAL,
                    ack_event_id TEXT,
                    ack_time REAL,
                    outcome_reward REAL,
                    outcome_reason TEXT,
                    outcome_time REAL
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_decisions_agent_time
                    ON provenance_decisions(agent_id,created_time);
                CREATE INDEX IF NOT EXISTS idx_provenance_decisions_event
                    ON provenance_decisions(trigger_event_id);

                CREATE TABLE IF NOT EXISTS provenance_commands (
                    command_id TEXT PRIMARY KEY,
                    decision_id TEXT,
                    entity_id TEXT NOT NULL,
                    target_property TEXT NOT NULL,
                    desired_value REAL NOT NULL,
                    deadband REAL NOT NULL,
                    created_time REAL NOT NULL,
                    dispatched_time REAL,
                    expires_time REAL NOT NULL,
                    status TEXT NOT NULL,
                    command_origin TEXT NOT NULL DEFAULT 'own_command'
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_commands_entity
                    ON provenance_commands(entity_id,expires_time);
                CREATE INDEX IF NOT EXISTS idx_provenance_commands_decision
                    ON provenance_commands(decision_id);

                CREATE TABLE IF NOT EXISTS provenance_command_contexts (
                    context_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    expires_time REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_command_contexts_command
                    ON provenance_command_contexts(command_id);

                CREATE TABLE IF NOT EXISTS provenance_experiences (
                    experience_key TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    created_time REAL NOT NULL,
                    agent_id TEXT NOT NULL,
                    decision_id TEXT,
                    source_event_id TEXT,
                    experiment_id TEXT,
                    episode_id TEXT,
                    source TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'unknown',
                    action_index INTEGER,
                    action_value REAL,
                    reward REAL,
                    features_json TEXT,
                    metadata_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_provenance_experiences_agent_time
                    ON provenance_experiences(agent_id,created_time);
                CREATE INDEX IF NOT EXISTS idx_provenance_experiences_decision
                    ON provenance_experiences(decision_id);
                """
            )
            # Additive forward migration for databases that briefly saw an earlier v1
            # draft. Existing rows stay unknown/own_command; no historical attribution is
            # fabricated from context_user_id or automation heuristics.
            event_cols = self._columns(c, "provenance_events")
            if "processed_time" not in event_cols:
                c.execute("ALTER TABLE provenance_events ADD COLUMN processed_time REAL")
            command_cols = self._columns(c, "provenance_commands")
            if "command_origin" not in command_cols:
                c.execute(
                    "ALTER TABLE provenance_commands ADD COLUMN command_origin TEXT NOT NULL DEFAULT 'own_command'"
                )

    def _remember_event(self, row):
        event_id = str(row["event_id"])
        key = (str(row["entity_id"]), float(row["event_time"]))
        with self._event_cache_lock:
            self._event_cache[event_id] = dict(row)
            self._event_cache.move_to_end(event_id)
            self._history_event_cache[key] = event_id
            self._history_event_cache.move_to_end(key)
            while len(self._event_cache) > self._event_cache_limit:
                old_id, old = self._event_cache.popitem(last=False)
                old_key = (str(old.get("entity_id") or ""), float(old.get("event_time") or 0.0))
                if self._history_event_cache.get(old_key) == old_id:
                    self._history_event_cache.pop(old_key, None)
            while len(self._history_event_cache) > self._event_cache_limit:
                self._history_event_cache.popitem(last=False)

    def _load_recent_event_cache(self):
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT * FROM provenance_events ORDER BY received_time DESC LIMIT ?",
                (self._event_cache_limit,),
            ).fetchall()
        for row in reversed(rows):
            self._remember_event(dict(row))

    def pending_event_count(self):
        with self._event_cache_lock:
            return len(self._pending_events)

    def flush_events_batch(self, limit=512):
        limit = max(1, int(limit))
        with self._event_cache_lock:
            keys = list(self._pending_events.keys())[:limit]
            rows = [dict(self._pending_events.pop(key)) for key in keys]
        if not rows:
            return 0
        packed = [
            (
                row["event_id"], CONTRACT_VERSION, float(row["event_time"]),
                float(row["received_time"]), str(row["entity_id"]),
                row.get("context_id"), row.get("context_parent_id"), row.get("user_id"),
                str(row.get("source") or UNKNOWN), str(row.get("origin") or UNKNOWN),
                row.get("state_json"), row.get("processed_time"),
            )
            for row in rows
        ]
        links = [(str(row["entity_id"]), float(row["event_time"]), str(row["event_id"])) for row in rows]
        try:
            with self.store.lock, self.store.conn() as c:
                c.executemany(
                    """INSERT INTO provenance_events
                       (event_id,contract_version,event_time,received_time,entity_id,context_id,
                        context_parent_id,user_id,source,origin,state_json,processed_time)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET
                         received_time=MIN(provenance_events.received_time,excluded.received_time),
                         origin=CASE WHEN provenance_events.origin='unknown' AND excluded.origin!='unknown'
                                     THEN excluded.origin ELSE provenance_events.origin END,
                         state_json=COALESCE(provenance_events.state_json,excluded.state_json),
                         processed_time=COALESCE(provenance_events.processed_time,excluded.processed_time)""",
                    packed,
                )
                c.executemany(
                    """INSERT INTO provenance_history_links(entity_id,event_time,event_id)
                       VALUES(?,?,?) ON CONFLICT(entity_id,event_time) DO UPDATE SET event_id=excluded.event_id""",
                    links,
                )
            return len(rows)
        except Exception:
            with self._event_cache_lock:
                for row in reversed(rows):
                    self._pending_events[str(row["event_id"])] = row
                    self._pending_events.move_to_end(str(row["event_id"]), last=False)
            raise

    def _load_active_command_cache(self):
        """Hydrate restart-surviving command evidence once, outside event hot paths."""
        now = float(self.clock())
        with self.store.conn() as c:
            commands = [dict(row) for row in c.execute(
                """SELECT * FROM provenance_commands
                   WHERE expires_time>? AND status IN ('pending','dispatched')""",
                (now,),
            ).fetchall()]
            contexts = [dict(row) for row in c.execute(
                """SELECT context_id,command_id,expires_time
                   FROM provenance_command_contexts WHERE expires_time>?""",
                (now,),
            ).fetchall()]
        with self._command_cache_lock:
            self._command_cache = {str(row["command_id"]): row for row in commands}
            active = set(self._command_cache)
            self._command_context_cache = {
                str(row["context_id"]): (str(row["command_id"]), float(row["expires_time"]))
                for row in contexts if str(row["command_id"]) in active
            }

    def _purge_command_cache(self, now):
        expired = {
            command_id for command_id, row in self._command_cache.items()
            if float(row.get("expires_time") or 0.0) <= float(now)
            or str(row.get("status") or "") not in {"pending", "dispatched"}
        }
        for command_id in expired:
            self._command_cache.pop(command_id, None)
        for context_id, (command_id, expires) in list(self._command_context_cache.items()):
            if command_id not in self._command_cache or float(expires) <= float(now):
                self._command_context_cache.pop(context_id, None)

    def record_event(self, entity_id, state, *, event_time, received_time=None,
                     source="ha_state_changed", origin=UNKNOWN, event_id=None):
        event_time = float(event_time)
        received_time = float(self.clock() if received_time is None else received_time)
        context = (state or {}).get("context") or {}
        context_id = context.get("id")
        parent_id = context.get("parent_id")
        user_id = context.get("user_id")
        event_id = event_id or stable_event_id(entity_id, event_time, context_id, parent_id, state)
        origin = str(origin or UNKNOWN)
        payload = None if state is None else {
            "state": state.get("state"),
            "last_changed": state.get("last_changed"),
            "last_updated": state.get("last_updated"),
        }
        with self._event_cache_lock:
            existing = self._event_cache.get(str(event_id)) or self._pending_events.get(str(event_id))
            if existing is not None:
                return str(event_id), False
        row = {
            "event_id": str(event_id), "contract_version": CONTRACT_VERSION,
            "event_time": event_time, "received_time": received_time,
            "entity_id": str(entity_id), "context_id": context_id,
            "context_parent_id": parent_id, "user_id": user_id,
            "source": str(source), "origin": origin,
            "state_json": _json(payload) if payload is not None else None,
            "processed_time": None,
        }
        self._remember_event(row)
        with self._event_cache_lock:
            if len(self._pending_events) >= self._pending_event_limit:
                self._pending_events.popitem(last=False)
                self._dropped_pending_events += 1
            self._pending_events[str(event_id)] = dict(row)
        return str(event_id), True

    def event(self, event_id):
        if not event_id:
            return None
        event_id = str(event_id)
        with self._event_cache_lock:
            cached = self._event_cache.get(event_id)
            if cached is not None:
                self._event_cache.move_to_end(event_id)
                return dict(cached)
            pending = self._pending_events.get(event_id)
            if pending is not None:
                return dict(pending)
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM provenance_events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        self._remember_event(out)
        return out

    def event_processed(self, event_id):
        row = self.event(event_id)
        return bool(row and row.get("processed_time") is not None)

    def mark_event_processed(self, event_id, processed_time=None):
        if not event_id:
            return
        event_id = str(event_id)
        processed_time = float(self.clock() if processed_time is None else processed_time)
        with self._event_cache_lock:
            cached = self._event_cache.get(event_id)
            pending = self._pending_events.get(event_id)
            if cached is not None or pending is not None:
                if cached is not None and cached.get("processed_time") is None:
                    cached["processed_time"] = processed_time
                if pending is not None and pending.get("processed_time") is None:
                    pending["processed_time"] = processed_time
                return
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE provenance_events SET processed_time=COALESCE(processed_time,?) WHERE event_id=?",
                (processed_time, event_id),
            )

    def history_provenance(self, entity_id, event_time):
        key = (str(entity_id), float(event_time))
        with self._event_cache_lock:
            event_id = self._history_event_cache.get(key)
            if event_id is not None:
                row = self._event_cache.get(event_id) or self._pending_events.get(event_id)
                if row is not None:
                    self._history_event_cache.move_to_end(key)
                    if event_id in self._event_cache:
                        self._event_cache.move_to_end(event_id)
                    return dict(row)
        with self.store.conn() as c:
            row = c.execute(
                """SELECT e.* FROM provenance_history_links l
                   JOIN provenance_events e ON e.event_id=l.event_id
                   WHERE l.entity_id=? AND l.event_time=?""",
                key,
            ).fetchone()
        if not row:
            return {"origin": UNKNOWN, "source": UNKNOWN, "event_id": None}
        out = dict(row)
        self._remember_event(out)
        return out

    def generation_for_agent(self, agent_id):
        with self.store.conn() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_candidate_generations'"
            ).fetchone()
            if not exists:
                return None
            row = c.execute(
                """SELECT generation_id,generation_number,generation_type,root_agent_id
                   FROM agent_candidate_generations WHERE agent_id=?
                   ORDER BY created_ts DESC LIMIT 1""",
                (str(agent_id),),
            ).fetchone()
        return dict(row) if row else None

    def record_decision(self, *, decision_id, created_time, agent_id, generation_id=None,
                        trigger_event_id=None, model_version=None, model_revision=None,
                        schema_version=None, schema_revision=None, reward_version=None,
                        feature_manifest=None, allowed_actions=None, chosen_action=None,
                        model_desired=None, teaching_id=None, teaching_desired=None,
                        experiment_id=None, episode_id=None, action_probability=None):
        probability = _finite(action_probability)
        if probability is not None and not 0.0 <= probability <= 1.0:
            probability = None
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO provenance_decisions
                   (decision_id,contract_version,created_time,agent_id,generation_id,trigger_event_id,
                    model_version,model_revision,schema_version,schema_revision,reward_version,
                    feature_manifest_json,allowed_actions_json,chosen_action,model_desired,
                    teaching_id,teaching_desired,experiment_id,episode_id,action_probability)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(decision_id), CONTRACT_VERSION, float(created_time), str(agent_id),
                 generation_id, trigger_event_id, model_version, model_revision,
                 schema_version, schema_revision, reward_version,
                 _json(feature_manifest or {}), _json(list(allowed_actions or [])),
                 _finite(chosen_action), _finite(model_desired),
                 int(teaching_id) if teaching_id else None, _finite(teaching_desired),
                 experiment_id, episode_id, probability),
            )
        return str(decision_id)

    def record_decisions_batch(self, rows):
        """Persist observation-only decisions in one transaction.

        Rows already contain their final dispatch_status/reason, so Shadow provenance
        avoids the old INSERT + UPDATE pair for every inference.
        """
        prepared = []
        for raw in rows or ():
            row = dict(raw or {})
            probability = _finite(row.get("action_probability"))
            if probability is not None and not 0.0 <= probability <= 1.0:
                probability = None
            prepared.append((
                str(row["decision_id"]), CONTRACT_VERSION, float(row["created_time"]),
                str(row["agent_id"]), row.get("generation_id"), row.get("trigger_event_id"),
                row.get("model_version"), row.get("model_revision"),
                row.get("schema_version"), row.get("schema_revision"), row.get("reward_version"),
                _json(row.get("feature_manifest") or {}),
                _json(list(row.get("allowed_actions") or [])),
                _finite(row.get("chosen_action")), _finite(row.get("model_desired")),
                int(row["teaching_id"]) if row.get("teaching_id") else None,
                _finite(row.get("teaching_desired")), row.get("experiment_id"),
                row.get("episode_id"), probability,
                None if row.get("dispatch_status") is None else str(row.get("dispatch_status")),
                None if row.get("dispatch_reason") is None else str(row.get("dispatch_reason")),
            ))
        if not prepared:
            return 0
        with self.store.lock, self.store.conn() as c:
            before = c.total_changes
            c.executemany(
                """INSERT OR IGNORE INTO provenance_decisions
                   (decision_id,contract_version,created_time,agent_id,generation_id,trigger_event_id,
                    model_version,model_revision,schema_version,schema_revision,reward_version,
                    feature_manifest_json,allowed_actions_json,chosen_action,model_desired,
                    teaching_id,teaching_desired,experiment_id,episode_id,action_probability,
                    dispatch_status,dispatch_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                prepared,
            )
            return int(c.total_changes - before)

    def decision(self, decision_id):
        flush = getattr(self.store, "_flush_provenance_decisions", None)
        if callable(flush):
            flush()
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM provenance_decisions WHERE decision_id=?", (str(decision_id),)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["feature_manifest"] = json.loads(out.pop("feature_manifest_json") or "{}")
        out["allowed_actions"] = json.loads(out.pop("allowed_actions_json") or "[]")
        return out

    def mark_decision_status(self, decision_id, status, reason=None):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE provenance_decisions SET dispatch_status=?,dispatch_reason=? WHERE decision_id=?",
                (str(status), None if reason is None else str(reason), str(decision_id)),
            )

    def mark_dispatch(self, decision_id, value, dispatched_time=None, episode_id=None):
        dispatched_time = float(self.clock() if dispatched_time is None else dispatched_time)
        episode_id = episode_id or str(decision_id)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE provenance_decisions SET dispatch_status='ACCEPTED',dispatch_time=?,
                   dispatch_value=?,episode_id=COALESCE(episode_id,?) WHERE decision_id=?""",
                (dispatched_time, _finite(value), episode_id, str(decision_id)),
            )
        return episode_id

    def mark_ack(self, decision_id, event_id=None, ack_time=None):
        if not decision_id:
            return
        ack_time = float(self.clock() if ack_time is None else ack_time)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE provenance_decisions SET ack_event_id=COALESCE(?,ack_event_id),
                   ack_time=COALESCE(ack_time,?) WHERE decision_id=?""",
                (event_id, ack_time, str(decision_id)),
            )

    def mark_outcome(self, decision_id, reward, reason, outcome_time=None):
        if not decision_id:
            return
        outcome_time = float(self.clock() if outcome_time is None else outcome_time)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE provenance_decisions SET outcome_reward=?,outcome_reason=?,outcome_time=?
                   WHERE decision_id=?""",
                (_finite(reward), None if reason is None else str(reason), outcome_time, str(decision_id)),
            )

    def reserve_command(self, agent, value, *, decision_id=None, command_id=None,
                        created_time=None, command_origin="own_command"):
        created_time = float(self.clock() if created_time is None else created_time)
        expires = created_time + max(30.0, float(agent.get("ack_timeout") or 0.0) * 2.0, 10.0)
        command_id = str(command_id or decision_id or uuid.uuid4())
        command_origin = str(command_origin or "own_command")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO provenance_commands
                   (command_id,decision_id,entity_id,target_property,desired_value,deadband,
                    created_time,dispatched_time,expires_time,status,command_origin)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(command_id) DO UPDATE SET
                     decision_id=COALESCE(excluded.decision_id,provenance_commands.decision_id),
                     desired_value=excluded.desired_value,deadband=excluded.deadband,
                     expires_time=MAX(provenance_commands.expires_time,excluded.expires_time),
                     command_origin=CASE
                       WHEN provenance_commands.command_origin='user_intent' THEN provenance_commands.command_origin
                       ELSE excluded.command_origin END,
                     status=CASE WHEN provenance_commands.status='dispatched' THEN provenance_commands.status ELSE excluded.status END""",
                (command_id, decision_id, agent["target_entity"], agent["target_property"],
                 float(value), float(agent.get("deadband") or 0.0), created_time, None, expires,
                 "pending", command_origin),
            )
        with self._command_cache_lock:
            self._purge_command_cache(created_time)
            previous = dict(self._command_cache.get(command_id) or {})
            self._command_cache[command_id] = {
                "command_id": command_id,
                "decision_id": decision_id or previous.get("decision_id"),
                "entity_id": previous.get("entity_id") or str(agent["target_entity"]),
                "target_property": previous.get("target_property") or str(agent["target_property"]),
                "desired_value": float(value),
                "deadband": float(agent.get("deadband") or 0.0),
                "created_time": float(previous.get("created_time") or created_time),
                "dispatched_time": previous.get("dispatched_time"),
                "expires_time": max(float(previous.get("expires_time") or 0.0), expires),
                "status": "dispatched" if previous.get("status") == "dispatched" else "pending",
                "command_origin": (
                    "user_intent" if previous.get("command_origin") == "user_intent"
                    else command_origin
                ),
            }
        return command_id

    def dispatch_command(self, command_id, response=None, dispatched_time=None):
        command_id = str(command_id)
        dispatched_time = float(self.clock() if dispatched_time is None else dispatched_time)
        contexts = []
        for state in response if isinstance(response, list) else []:
            if not isinstance(state, dict):
                continue
            context = state.get("context") or {}
            for key in ("id", "parent_id"):
                if context.get(key):
                    contexts.append(str(context[key]))
        with self._command_cache_lock:
            self._purge_command_cache(dispatched_time)
            cached = dict(self._command_cache.get(command_id) or {})
        expires = cached.get("expires_time")
        if expires is None:
            # Compatibility fallback for externally inserted command rows. Normal runtime
            # commands and restart-surviving commands are already memory-cached.
            with self.store.conn() as c:
                row = c.execute(
                    "SELECT * FROM provenance_commands WHERE command_id=?", (command_id,)
                ).fetchone()
            if not row:
                return
            cached = dict(row)
            expires = float(cached["expires_time"])
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE provenance_commands SET dispatched_time=?,status='dispatched' WHERE command_id=?",
                (dispatched_time, command_id),
            )
            for context_id in set(contexts):
                c.execute(
                    """INSERT INTO provenance_command_contexts(context_id,command_id,expires_time)
                       VALUES(?,?,?) ON CONFLICT(context_id) DO UPDATE SET
                       command_id=excluded.command_id,expires_time=excluded.expires_time""",
                    (context_id, command_id, float(expires)),
                )
        with self._command_cache_lock:
            cached["dispatched_time"] = dispatched_time
            cached["status"] = "dispatched"
            self._command_cache[command_id] = cached
            for context_id in set(contexts):
                self._command_context_cache[context_id] = (command_id, float(expires))

    def fail_command(self, command_id):
        if not command_id:
            return
        command_id = str(command_id)
        now = float(self.clock())
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE provenance_commands SET status='failed',expires_time=? WHERE command_id=?",
                      (now, command_id))
            c.execute("DELETE FROM provenance_command_contexts WHERE command_id=?", (command_id,))
        with self._command_cache_lock:
            self._command_cache.pop(command_id, None)
            for context_id, (cached_id, _expires) in list(self._command_context_cache.items()):
                if cached_id == command_id:
                    self._command_context_cache.pop(context_id, None)

    def match_command_state(self, state):
        """Match a live HA state against the restart-safe active-command cache."""
        if not state:
            return None
        now = float(self.clock())
        entity_id = str(state.get("entity_id") or "")
        context = state.get("context") or {}
        context_ids = [str(context[k]) for k in ("id", "parent_id") if context.get(k)]
        with self._command_cache_lock:
            self._purge_command_cache(now)
            for context_id in context_ids:
                match = self._command_context_cache.get(context_id)
                if not match:
                    continue
                command_id, expires = match
                row = self._command_cache.get(command_id)
                if row and float(expires) > now:
                    return dict(row)
            rows = sorted(
                (
                    dict(row) for row in self._command_cache.values()
                    if str(row.get("entity_id") or "") == entity_id
                ),
                key=lambda row: float(row.get("created_time") or 0.0),
                reverse=True,
            )[:8]
        for row in rows:
            current = target_value(state, row["target_property"])
            if current is not None and same_value(current, row["desired_value"], row["deadband"]):
                return row
        return None

    def experience_exists(self, experience_key):
        if not experience_key:
            return False
        with self.store.conn() as c:
            return bool(c.execute("SELECT 1 FROM provenance_experiences WHERE experience_key=?",
                                  (str(experience_key),)).fetchone())

    def record_experience(self, *, experience_key, agent_id, source, origin=UNKNOWN,
                          decision_id=None, source_event_id=None, experiment_id=None,
                          episode_id=None, action_index=None, action_value=None, reward=None,
                          features=None, metadata=None, created_time=None):
        created_time = float(self.clock() if created_time is None else created_time)
        with self.store.lock, self.store.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO provenance_experiences
                   (experience_key,contract_version,created_time,agent_id,decision_id,source_event_id,
                    experiment_id,episode_id,source,origin,action_index,action_value,reward,
                    features_json,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(experience_key), CONTRACT_VERSION, created_time, str(agent_id), decision_id,
                 source_event_id, experiment_id, episode_id, str(source), str(origin or UNKNOWN),
                 None if action_index is None else int(action_index), _finite(action_value), _finite(reward),
                 _json({str(k): v for k, v in (features or {}).items()}) if features is not None else None,
                 _json(metadata or {}) if metadata is not None else None),
            )
        return bool(cur.rowcount)

    def record_experiences_batch(self, records):
        """Persist a bounded group of provenance facts in one transaction.

        This is the replay counterpart of record_experience().  It preserves the exact
        idempotency-key contract while avoiding one WAL commit per historical dwell.
        """
        records = list(records or [])
        if not records:
            return 0
        packed = []
        default_created = float(self.clock())
        for row in records:
            packed.append((
                str(row["experience_key"]),
                CONTRACT_VERSION,
                float(row.get("created_time") if row.get("created_time") is not None else default_created),
                str(row["agent_id"]),
                row.get("decision_id"),
                row.get("source_event_id"),
                row.get("experiment_id"),
                row.get("episode_id"),
                str(row["source"]),
                str(row.get("origin") or UNKNOWN),
                None if row.get("action_index") is None else int(row["action_index"]),
                _finite(row.get("action_value")),
                _finite(row.get("reward")),
                (_json({str(k): v for k, v in (row.get("features") or {}).items()})
                 if row.get("features") is not None else None),
                (_json(row.get("metadata") or {}) if row.get("metadata") is not None else None),
            ))
        with self.store.lock, self.store.conn() as c:
            before = int(c.total_changes)
            c.executemany(
                """INSERT OR IGNORE INTO provenance_experiences
                   (experience_key,contract_version,created_time,agent_id,decision_id,source_event_id,
                    experiment_id,episode_id,source,origin,action_index,action_value,reward,
                    features_json,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                packed,
            )
            return max(0, int(c.total_changes) - before)

    def commit_feedback_model(self, *, experience_key, agent_id, model, action_index,
                              action_value, reward, reason, features, user_id=None,
                              decision_id=None, source_event_id=None, episode_id=None,
                              experiment_id=None, source="live", origin=UNKNOWN,
                              metadata=None):
        """Atomically persist one learned feedback update and its idempotency key.

        Caller holds ``store.lock`` while checking ``experience_exists``, mutating the
        in-memory policy and entering this method. Store.lock is re-entrant. The model,
        feedback row and experience key commit in one SQLite transaction, so retry after
        restart cannot apply the same update twice.
        """
        model = dict(model)
        with self.store.lock, self.store.conn() as c:
            if c.execute("SELECT 1 FROM provenance_experiences WHERE experience_key=?",
                         (str(experience_key),)).fetchone():
                return False
            model["_history_watermark"] = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM historical_experiences WHERE agent_id=?",
                (str(agent_id),),
            ).fetchone()[0]
            previous = c.execute("SELECT model_json FROM rl_models WHERE agent_id=?", (str(agent_id),)).fetchone()
            if previous and "_benchmark_counts" not in model:
                try:
                    model["_benchmark_counts"] = json.loads(previous[0]).get("_benchmark_counts", {})
                except (TypeError, ValueError):
                    model["_benchmark_counts"] = {}
            c.execute(
                """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json,updated_at=excluded.updated_at""",
                (str(agent_id), _json(model), iso_now()),
            )
            c.execute(
                """INSERT INTO rl_feedback(agent_id,created_at,action_index,action_value,reward,reason,
                   features_json,user_id,source) VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(agent_id), iso_now(), int(action_index), float(action_value), float(reward), str(reason),
                 _json({str(k): v for k, v in features.items()}), user_id, str(source)),
            )
            c.execute(
                """INSERT INTO provenance_experiences
                   (experience_key,contract_version,created_time,agent_id,decision_id,source_event_id,
                    experiment_id,episode_id,source,origin,action_index,action_value,reward,
                    features_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(experience_key), CONTRACT_VERSION, float(self.clock()), str(agent_id), decision_id,
                 source_event_id, experiment_id, episode_id, str(source), str(origin or UNKNOWN),
                 int(action_index), float(action_value), float(reward),
                 _json({str(k): v for k, v in features.items()}), _json(metadata or {})),
            )
        return True

    def list_experiences(self, agent_id=None):
        with self.store.conn() as c:
            if agent_id is None:
                rows = c.execute("SELECT * FROM provenance_experiences ORDER BY created_time,experience_key").fetchall()
            else:
                rows = c.execute("SELECT * FROM provenance_experiences WHERE agent_id=? ORDER BY created_time,experience_key",
                                 (str(agent_id),)).fetchall()
        return [dict(r) for r in rows]
