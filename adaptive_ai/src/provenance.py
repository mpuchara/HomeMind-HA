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
import uuid

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
        self._migrate()

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
        with self.store.lock, self.store.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO provenance_events
                   (event_id,contract_version,event_time,received_time,entity_id,context_id,
                    context_parent_id,user_id,source,origin,state_json,processed_time)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (event_id, CONTRACT_VERSION, event_time, received_time, str(entity_id),
                 context_id, parent_id, user_id, str(source), origin,
                 _json(payload) if payload is not None else None),
            )
            c.execute(
                """INSERT INTO provenance_history_links(entity_id,event_time,event_id)
                   VALUES(?,?,?) ON CONFLICT(entity_id,event_time) DO UPDATE SET event_id=excluded.event_id""",
                (str(entity_id), event_time, event_id),
            )
        return event_id, bool(cur.rowcount)

    def event(self, event_id):
        if not event_id:
            return None
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM provenance_events WHERE event_id=?", (str(event_id),)).fetchone()
        return dict(row) if row else None

    def event_processed(self, event_id):
        row = self.event(event_id)
        return bool(row and row.get("processed_time") is not None)

    def mark_event_processed(self, event_id, processed_time=None):
        if not event_id:
            return
        processed_time = float(self.clock() if processed_time is None else processed_time)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE provenance_events SET processed_time=COALESCE(processed_time,?) WHERE event_id=?",
                (processed_time, str(event_id)),
            )

    def history_provenance(self, entity_id, event_time):
        with self.store.conn() as c:
            row = c.execute(
                """SELECT e.* FROM provenance_history_links l
                   JOIN provenance_events e ON e.event_id=l.event_id
                   WHERE l.entity_id=? AND l.event_time=?""",
                (str(entity_id), float(event_time)),
            ).fetchone()
        return dict(row) if row else {"origin": UNKNOWN, "source": UNKNOWN, "event_id": None}

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

    def decision(self, decision_id):
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
        command_id = command_id or decision_id or str(uuid.uuid4())
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
                (str(command_id), decision_id, agent["target_entity"], agent["target_property"],
                 float(value), float(agent.get("deadband") or 0.0), created_time, None, expires,
                 "pending", command_origin),
            )
        return str(command_id)

    def dispatch_command(self, command_id, response=None, dispatched_time=None):
        dispatched_time = float(self.clock() if dispatched_time is None else dispatched_time)
        contexts = []
        for state in response if isinstance(response, list) else []:
            if not isinstance(state, dict):
                continue
            context = state.get("context") or {}
            for key in ("id", "parent_id"):
                if context.get(key):
                    contexts.append(str(context[key]))
        with self.store.lock, self.store.conn() as c:
            row = c.execute("SELECT expires_time FROM provenance_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if not row:
                return
            expires = float(row[0])
            c.execute(
                "UPDATE provenance_commands SET dispatched_time=?,status='dispatched' WHERE command_id=?",
                (dispatched_time, str(command_id)),
            )
            for context_id in set(contexts):
                c.execute(
                    """INSERT INTO provenance_command_contexts(context_id,command_id,expires_time)
                       VALUES(?,?,?) ON CONFLICT(context_id) DO UPDATE SET
                       command_id=excluded.command_id,expires_time=excluded.expires_time""",
                    (context_id, str(command_id), expires),
                )

    def fail_command(self, command_id):
        if not command_id:
            return
        now = float(self.clock())
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE provenance_commands SET status='failed',expires_time=? WHERE command_id=?",
                      (now, str(command_id)))
            c.execute("DELETE FROM provenance_command_contexts WHERE command_id=?", (str(command_id),))

    def match_command_state(self, state):
        if not state:
            return None
        now = float(self.clock())
        entity_id = state.get("entity_id")
        context = state.get("context") or {}
        context_ids = [str(context[k]) for k in ("id", "parent_id") if context.get(k)]
        with self.store.conn() as c:
            for context_id in context_ids:
                row = c.execute(
                    """SELECT p.* FROM provenance_command_contexts x
                       JOIN provenance_commands p ON p.command_id=x.command_id
                       WHERE x.context_id=? AND x.expires_time>? AND p.status IN ('pending','dispatched')""",
                    (context_id, now),
                ).fetchone()
                if row:
                    return dict(row)
            rows = c.execute(
                """SELECT * FROM provenance_commands WHERE entity_id=? AND expires_time>?
                   AND status IN ('pending','dispatched') ORDER BY created_time DESC LIMIT 8""",
                (str(entity_id), now),
            ).fetchall()
        for row in rows:
            row = dict(row)
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
                 _json({str(k): v for k, v in (features or {}).items()) if features is not None else None,
                 _json(metadata or {}) if metadata is not None else None),
            )
        return bool(cur.rowcount)

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
