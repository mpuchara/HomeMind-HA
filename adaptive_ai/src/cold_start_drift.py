"""Stage 14: explicit cold-start evidence and controlled adaptation to drift.

This observer is deliberately non-controlling. It reports cold-start evidence, monitors
immutable episode outcomes / corrections / sensor health / topology, and asks the existing
Candidate workflow for an isolated child after sustained drift. It never lowers safety
gates, creates ActionIntent, or dispatches Home Assistant services. Promotion remains owned
by the existing Candidate + Stage-13 fixed-future-evidence contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter

from confidence_contract import DEFAULT_HALF_LIFE_EPISODES
from settings import OPTIONS, iso_now


CONTRACT_VERSION = 1
BASELINE_EPISODES = 12
RECENT_EPISODES = 6
ENV_STABLE_SNAPSHOTS = 3
QUALITY_DROP_THRESHOLD = 0.20
ROLLBACK_DROP_THRESHOLD = 0.20
RECOVERY_MARGIN = 0.05
HOUR_SHIFT_THRESHOLD = 2.5
CONTEXT_TV_THRESHOLD = 0.35
SENSOR_HEALTH_BAD = 0.65
SENSOR_HEALTH_DROP = 0.20
MAX_OPTIONAL_QUESTIONS = 2
OBSERVE_THROTTLE_SECONDS = 30.0
POST_PROMOTION_EPISODES = 6
ACTIVE_PREFERENCE_STATUSES = {"recorded", "applied", "learning_queued", "rebuild_queued"}


def _finite(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return {} if default is None else default


def _table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone())


def _hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _circular_hour_distance(a, b):
    if a is None or b is None:
        return 0.0
    delta = abs(float(a) - float(b)) % 24.0
    return min(delta, 24.0 - delta)


def _circular_mean(hours):
    values = [float(x) % 24.0 for x in hours if _finite(x) is not None]
    if not values:
        return None
    xs = sum(math.cos(v / 24.0 * 2.0 * math.pi) for v in values)
    ys = sum(math.sin(v / 24.0 * 2.0 * math.pi) for v in values)
    if abs(xs) < 1e-12 and abs(ys) < 1e-12:
        return values[-1]
    angle = math.atan2(ys, xs)
    if angle < 0:
        angle += 2.0 * math.pi
    return angle / (2.0 * math.pi) * 24.0


def _tv_distance(left, right):
    a = Counter(str(x) for x in left if x is not None)
    b = Counter(str(x) for x in right if x is not None)
    if not a or not b:
        return 0.0
    na, nb = float(sum(a.values())), float(sum(b.values()))
    return 0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in set(a) | set(b))


def decay_contract():
    """Report the meaning of decay instead of conflating instructions with statistics."""
    return {
        "version": CONTRACT_VERSION,
        "policy_learning": {
            "basis": "wall_clock",
            "half_life_days": float(OPTIONS.get("policy_half_life_days", 30)),
            "meaning": "old statistical policy evidence gradually loses training influence",
        },
        "future_evaluation": {
            "basis": "episode_order",
            "half_life_episodes": float(DEFAULT_HALF_LIFE_EPISODES),
            "meaning": "old evaluation evidence decays outside the locked Stage-13 future test",
        },
        "persistent_instruction": {
            "decays": False,
            "meaning": "explicit persistent preference is an instruction, not historical statistics",
        },
        "regression_anchor": {
            "decays_for_retention": False,
            "training_weight": 0.0,
            "meaning": "durable regression reference only; never multiplied into training weight",
        },
    }


def ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS adaptation_episode_observations (
                agent_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                ts REAL NOT NULL,
                quality REAL,
                cost REAL,
                harmful INTEGER NOT NULL DEFAULT 0,
                correction_count INTEGER NOT NULL DEFAULT 0,
                activity_hour REAL,
                context_bucket TEXT,
                source TEXT NOT NULL,
                created_ts REAL NOT NULL,
                PRIMARY KEY(agent_id,episode_id)
            );
            CREATE INDEX IF NOT EXISTS idx_adaptation_episode_agent_ts
                ON adaptation_episode_observations(agent_id,ts);

            CREATE TABLE IF NOT EXISTS adaptation_environment_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                ts REAL NOT NULL,
                sensor_health REAL,
                unavailable_fraction REAL,
                sensor_count INTEGER NOT NULL DEFAULT 0,
                topology_signature TEXT,
                topology_json TEXT NOT NULL DEFAULT '{}',
                registry_revision INTEGER,
                preference_revision INTEGER NOT NULL DEFAULT 0,
                preference_latest_ts REAL,
                context_bucket TEXT,
                created_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_adaptation_env_agent_ts
                ON adaptation_environment_snapshots(agent_id,ts);

            CREATE TABLE IF NOT EXISTS adaptation_state (
                agent_id TEXT PRIMARY KEY,
                contract_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                drift_kind TEXT,
                drift_reason TEXT,
                drift_score REAL,
                detected_ts REAL,
                baseline_quality REAL,
                baseline_episode_count INTEGER NOT NULL DEFAULT 0,
                candidate_generation_id TEXT,
                candidate_agent_id TEXT,
                promoted_ts REAL,
                rollback_backup_id INTEGER,
                recovered_ts REAL,
                episodes_to_recover INTEGER,
                preference_revision_seen INTEGER NOT NULL DEFAULT 0,
                last_episode_ts REAL,
                last_observe_ts REAL,
                updated_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS adaptation_regression_anchors (
                agent_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                retained_ts REAL NOT NULL,
                training_weight REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(agent_id,episode_id)
            );
            """
        )


def _quality_from_metrics(metrics):
    metrics = dict(metrics or {})
    corrections = int(metrics.get("manual_correction_count") or 0)
    if not bool(metrics.get("meaningful")) and corrections <= 0:
        return None, None
    cost = 0.0
    cost += min(1.0, corrections * 0.50)
    cost += min(1.0, max(0.0, float(metrics.get("off_while_needed_seconds") or 0.0)) / 30.0)
    cost += min(1.0, max(0.0, float(metrics.get("unnecessary_on_seconds") or 0.0)) / 120.0)
    cost += min(1.0, float(metrics.get("false_arrival_prediction") or 0.0))
    cost += min(1.0, float(metrics.get("retrigger_count") or 0.0) * 0.25)
    cost += min(1.0, float(metrics.get("chatter_count") or 0.0) * 0.20)
    cost = max(0.0, min(1.0, cost))
    if bool(metrics.get("harmful")):
        cost = max(cost, 0.5)
    return 1.0 - cost, cost


def _context_bucket(context, ts):
    context = dict(context or {})
    for key in ("context_bucket", "household_pattern", "occupancy_pattern", "activity_pattern"):
        value = context.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    hour = (float(ts) % 86400.0) / 3600.0
    return f"hour3:{int(hour // 3)}"


class AdaptationService:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.engine = manager.engine
        self._in_observe = set()
        # Drift detection is deliberately off the realtime inference worker. The old
        # implementation ran SQLite episode ingestion + drift scans synchronously after
        # every live inference; as evidence accumulated this became an O(history) hot path
        # and could starve the HA websocket/UI on Raspberry Pi.
        self._observer_lock = threading.RLock()
        self._observer_event = threading.Event()
        self._observer_pending = {}
        self._observer_last_run = {}
        self._observer_thread = None
        self._observer_stats = {
            "scheduled": 0, "coalesced": 0, "runs": 0, "errors": 0,
            "pending_high_water": 0, "last_run_ms": 0.0, "max_run_ms": 0.0,
        }
        ensure_tables(self.store)

    def _state(self, agent_id):
        aid = str(agent_id)
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM adaptation_state WHERE agent_id=?", (aid,)).fetchone()
        if row:
            return dict(row)
        revision, _ = self._preference_state(aid)
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO adaptation_state
                   (agent_id,contract_version,status,preference_revision_seen,updated_ts)
                   VALUES(?,?,?,?,?)""",
                (aid, CONTRACT_VERSION, "monitoring", int(revision), now),
            )
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM adaptation_state WHERE agent_id=?", (aid,)).fetchone()
        return dict(row)

    def _update_state(self, agent_id, **fields):
        if not fields:
            return self._state(agent_id)
        self._state(agent_id)
        fields = dict(fields)
        fields["updated_ts"] = time.time()
        names = list(fields)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE adaptation_state SET " + ",".join(f"{name}=?" for name in names) + " WHERE agent_id=?",
                [fields[name] for name in names] + [str(agent_id)],
            )
        return self._state(agent_id)

    def _preference_state(self, agent_id):
        with self.store.conn() as c:
            if not _table_exists(c, "manual_feedback_journal"):
                return 0, None
            rows = c.execute(
                """SELECT created_ts,scope,application_status,undone_ts FROM manual_feedback_journal
                   WHERE (agent_id=? OR root_agent_id=?) ORDER BY created_ts""",
                (str(agent_id), str(agent_id)),
            ).fetchall()
        persistent = [
            dict(row) for row in rows
            if row["undone_ts"] is None
            and str(row["scope"] or "") == "persistent_preference"
            and str(row["application_status"] or "") in ACTIVE_PREFERENCE_STATUSES
        ]
        return len(persistent), (float(persistent[-1]["created_ts"]) if persistent else None)

    def _demonstration_counts(self, agent_id):
        values = []
        with self.store.conn() as c:
            for table in ("teaching_rl_labels", "teaching_labels"):
                if _table_exists(c, table):
                    rows = c.execute(
                        f"SELECT desired FROM {table} WHERE agent_id=? AND undone_ts IS NULL",
                        (str(agent_id),),
                    ).fetchall()
                    values.extend(_finite(row[0]) for row in rows)
            if _table_exists(c, "manual_feedback_journal"):
                rows = c.execute(
                    """SELECT correct_action FROM manual_feedback_journal
                       WHERE (agent_id=? OR root_agent_id=?) AND undone_ts IS NULL
                         AND correct_action IS NOT NULL""",
                    (str(agent_id), str(agent_id)),
                ).fetchall()
                values.extend(_finite(row[0]) for row in rows)
        values = [value for value in values if value is not None]
        return {
            "total": len(values),
            "on": sum(value >= 0.5 for value in values),
            "off": sum(value < 0.5 for value in values),
        }

    def _recognized_sensors(self, agent, state_map=None):
        states = dict(state_map or getattr(self.engine, "state_map", {}) or {})
        model = self.store.get_model(agent["id"]) or {}
        schema = dict(model.get("schema") or {})
        entities = list(schema.get("entities") or [])
        if not entities:
            entities = [x for x in list(agent.get("input_entities") or []) if x != "*"]
        context = getattr(self.engine, "context", None)
        if not entities and context is not None and callable(getattr(context, "relevant_entities", None)):
            try:
                entities = list(context.relevant_entities())[:32]
            except Exception:
                entities = []
        entities = [str(x) for x in entities if str(x) != str(agent.get("target_entity"))]
        result = []
        for eid in sorted(set(entities)):
            state = states.get(eid) or {}
            available = str(state.get("state") or "").lower() not in ("", "unknown", "unavailable", "none")
            area = role = None
            if context is not None:
                try:
                    area = context.area_for(eid)
                except Exception:
                    pass
                try:
                    role = (context.evidence_metadata(eid) or {}).get("role")
                except Exception:
                    pass
            result.append({"entity_id": eid, "available": available, "area_id": area, "role": role})
        return result

    def _automation_fallback(self, agent):
        executor = getattr(self.engine, "executor", None)
        handoff = getattr(executor, "handoff", None)
        knowledge = getattr(handoff, "knowledge", None)
        if knowledge is None or not callable(getattr(knowledge, "hints_for_target", None)):
            return []
        try:
            _, infos = knowledge.hints_for_target(agent["target_entity"])
            return [str(row.get("entity_id")) for row in (infos or []) if row.get("entity_id")]
        except Exception:
            return []

    def cold_start(self, agent, state_map=None):
        aid = str(agent["id"])
        sensors = self._recognized_sensors(agent, state_map)
        demonstrations = self._demonstration_counts(aid)
        with self.store.conn() as c:
            target_history = int(c.execute(
                "SELECT COUNT(*) FROM entity_history WHERE entity_id=?", (str(agent["target_entity"]),)
            ).fetchone()[0])
        automations = self._automation_fallback(agent)
        missing = []
        if target_history <= 0:
            missing.append("target_history")
        if not sensors:
            missing.append("recognized_context_sensors")
        if demonstrations["on"] <= 0:
            missing.append("independent_on_demonstration")
        if demonstrations["off"] <= 0:
            missing.append("independent_off_demonstration")
        questions = []
        if demonstrations["on"] <= 0:
            questions.append({
                "id": "cold_start_on_preference", "optional": True,
                "question": "W jednej niepewnej sytuacji: czy urządzenie powinno wtedy przejść do ON?",
                "creates": "explicit_demonstration", "auto_dispatch": False,
            })
        if demonstrations["off"] <= 0:
            questions.append({
                "id": "cold_start_off_preference", "optional": True,
                "question": "W jednej niepewnej sytuacji: czy urządzenie powinno wtedy pozostać/przejść do OFF?",
                "creates": "explicit_demonstration", "auto_dispatch": False,
            })
        model = self.store.get_model(aid)
        fallback = "existing_home_assistant_automation" if automations else "manual_or_existing_device_behavior"
        if model is not None:
            recommended = "shadow"
        elif target_history > 0 or automations:
            recommended = "fallback_plus_shadow_learning"
        else:
            recommended = "fallback_collect_evidence"
        return {
            "contract_version": CONTRACT_VERSION,
            "agent_id": aid,
            "status": "ready" if not missing else "missing_evidence",
            "recommended_mode": recommended,
            "fallback": fallback,
            "fallback_automations": automations,
            "recognized_sensors": sensors,
            "history_samples": target_history,
            "demonstrations": demonstrations,
            "missing_evidence": missing,
            "optional_questions": questions[:MAX_OPTIONAL_QUESTIONS],
            "question_budget": {"max_outstanding": MAX_OPTIONAL_QUESTIONS, "automatic_questions": False},
            "safety": {
                "thresholds_relaxed": False,
                "control_without_history": False,
                "insufficient_evidence_means": "fallback_or_shadow",
            },
            "decay": decay_contract(),
        }

    def record_episode(self, agent_id, episode_id, ts, *, quality=None, cost=None,
                       harmful=False, correction_count=0, activity_hour=None,
                       context_bucket=None, source="runtime"):
        if not episode_id:
            raise ValueError("episode_id is required")
        ts = float(ts)
        hour = (ts % 86400.0) / 3600.0 if activity_hour is None else float(activity_hour) % 24.0
        with self.store.lock, self.store.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO adaptation_episode_observations
                   (agent_id,episode_id,ts,quality,cost,harmful,correction_count,activity_hour,
                    context_bucket,source,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(agent_id), str(episode_id), ts, _finite(quality), _finite(cost),
                    int(bool(harmful)), int(correction_count or 0), hour,
                    None if context_bucket is None else str(context_bucket), str(source), time.time(),
                ),
            )
        return bool(cur.rowcount)

    def ingest_episode_evaluator(self, agent_id, limit=128):
        """Import only episode rows not already seen, in one bounded transaction.

        Previously every inference selected the complete episode history and attempted one
        INSERT OR IGNORE transaction per historical row. Runtime cost therefore increased
        monotonically with uptime. The observer now consumes only the missing suffix.
        """
        aid = str(agent_id)
        limit = max(1, min(512, int(limit or 128)))
        with self.store.conn() as c:
            if not (_table_exists(c, "episode_evaluator_episodes")
                    and _table_exists(c, "episode_evaluator_policy_results")):
                return 0
            rows = c.execute(
                """SELECT e.episode_id,e.end_ts,e.context_json,r.metrics_json
                   FROM episode_evaluator_episodes e
                   JOIN episode_evaluator_policy_results r ON r.episode_id=e.episode_id
                   LEFT JOIN adaptation_episode_observations a
                     ON a.agent_id=e.agent_id AND a.episode_id=e.episode_id
                   WHERE e.agent_id=? AND r.role='live' AND r.executed=1
                     AND a.episode_id IS NULL
                   ORDER BY e.end_ts,e.episode_id
                   LIMIT ?""",
                (aid, limit),
            ).fetchall()
        if not rows:
            return 0

        packed = []
        last_ts = None
        created = time.time()
        for raw in rows:
            row = dict(raw)
            metrics = _json(row.get("metrics_json"), {})
            quality, cost = _quality_from_metrics(metrics)
            context = _json(row.get("context_json"), {})
            ts = float(row["end_ts"])
            hour = (ts % 86400.0) / 3600.0
            packed.append((
                aid, str(row["episode_id"]), ts, _finite(quality), _finite(cost),
                int(bool(metrics.get("harmful"))),
                int(metrics.get("manual_correction_count") or 0),
                hour, _context_bucket(context, ts), "episode_evaluator_live", created,
            ))
            last_ts = ts

        with self.store.lock, self.store.conn() as c:
            before = c.total_changes
            c.executemany(
                """INSERT OR IGNORE INTO adaptation_episode_observations
                   (agent_id,episode_id,ts,quality,cost,harmful,correction_count,activity_hour,
                    context_bucket,source,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                packed,
            )
            added = int(c.total_changes - before)
        if added and last_ts is not None:
            self._update_state(aid, last_episode_ts=last_ts)
        return added

    def record_environment(self, agent_id, ts, *, sensor_health, unavailable_fraction=0.0,
                           topology_signature=None, topology=None, registry_revision=None,
                           preference_revision=0, preference_latest_ts=None, context_bucket=None):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO adaptation_environment_snapshots
                   (agent_id,ts,sensor_health,unavailable_fraction,sensor_count,topology_signature,
                    topology_json,registry_revision,preference_revision,preference_latest_ts,
                    context_bucket,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(agent_id), float(ts), _finite(sensor_health), _finite(unavailable_fraction, 0.0),
                    len(topology or []), topology_signature,
                    json.dumps(topology or [], separators=(",", ":"), default=str),
                    registry_revision, int(preference_revision or 0), _finite(preference_latest_ts),
                    context_bucket, time.time(),
                ),
            )

    def _environment_from_runtime(self, agent, state_map=None):
        sensors = self._recognized_sensors(agent, state_map)
        context = getattr(self.engine, "context", None)
        health_values, topology = [], []
        unavailable = 0
        for row in sensors:
            eid = row["entity_id"]
            value = 1.0 if row["available"] else 0.0
            source = None
            if context is not None:
                source = dict(getattr(getattr(context, "home", None), "sources", {}).get(eid) or {})
            if source:
                value *= max(0.0, min(1.0, float(source.get("communication_reliability") or 0.0)))
            if not row["available"]:
                unavailable += 1
            health_values.append(value)
            topology.append({"entity_id": eid, "area_id": row.get("area_id"), "role": row.get("role")})
        revision, latest = self._preference_state(agent["id"])
        return {
            "sensor_health": (sum(health_values) / len(health_values) if health_values else None),
            "unavailable_fraction": (unavailable / len(sensors) if sensors else 0.0),
            "topology_signature": (_hash(topology) if topology else None),
            "topology": topology,
            "registry_revision": getattr(context, "registry_revision", None) if context is not None else None,
            "preference_revision": revision,
            "preference_latest_ts": latest,
        }

    def _episode_rows(self, agent_id, *, limit=None, after_ts=None, quality_only=False):
        where = ["agent_id=?"]
        values = [str(agent_id)]
        if after_ts is not None:
            where.append("ts>?")
            values.append(float(after_ts))
        if quality_only:
            where.append("quality IS NOT NULL")
        sql = (
            "SELECT * FROM adaptation_episode_observations WHERE "
            + " AND ".join(where)
            + " ORDER BY ts DESC,episode_id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            values.append(max(1, int(limit)))
        with self.store.conn() as c:
            rows = [dict(row) for row in c.execute(sql, values).fetchall()]
        rows.reverse()
        return rows

    def _env_rows(self, agent_id, *, limit=None):
        sql = (
            "SELECT * FROM adaptation_environment_snapshots "
            "WHERE agent_id=? ORDER BY ts DESC,id DESC"
        )
        values = [str(agent_id)]
        if limit is not None:
            sql += " LIMIT ?"
            values.append(max(1, int(limit)))
        with self.store.conn() as c:
            rows = [dict(row) for row in c.execute(sql, values).fetchall()]
        rows.reverse()
        return rows

    def _episode_shift(self, agent_id):
        rows = self._episode_rows(
            agent_id, limit=BASELINE_EPISODES + RECENT_EPISODES, quality_only=True
        )
        if len(rows) < BASELINE_EPISODES + RECENT_EPISODES:
            return {"ready": False, "episodes": len(rows), "required": BASELINE_EPISODES + RECENT_EPISODES}
        baseline = rows[-(BASELINE_EPISODES + RECENT_EPISODES):-RECENT_EPISODES]
        recent = rows[-RECENT_EPISODES:]
        bq = sum(float(row["quality"]) for row in baseline) / len(baseline)
        rq = sum(float(row["quality"]) for row in recent) / len(recent)
        bh = _circular_mean(row.get("activity_hour") for row in baseline)
        rh = _circular_mean(row.get("activity_hour") for row in recent)
        return {
            "ready": True,
            "baseline_quality": bq,
            "recent_quality": rq,
            "quality_drop": bq - rq,
            "baseline_hour": bh,
            "recent_hour": rh,
            "hour_shift": _circular_hour_distance(bh, rh),
            "context_tv": _tv_distance(
                [row.get("context_bucket") for row in baseline],
                [row.get("context_bucket") for row in recent],
            ),
            "recent_corrections": sum(int(row.get("correction_count") or 0) for row in recent),
            "baseline_episode_ids": [row["episode_id"] for row in baseline],
            "recent_episode_ids": [row["episode_id"] for row in recent],
        }

    def _environment_shift(self, agent_id):
        rows = self._env_rows(agent_id, limit=ENV_STABLE_SNAPSHOTS * 2)
        if len(rows) < ENV_STABLE_SNAPSHOTS * 2:
            return {"ready": False, "snapshots": len(rows)}
        previous = rows[-ENV_STABLE_SNAPSHOTS * 2:-ENV_STABLE_SNAPSHOTS]
        recent = rows[-ENV_STABLE_SNAPSHOTS:]

        def mean(name, values):
            clean = [_finite(row.get(name)) for row in values]
            clean = [value for value in clean if value is not None]
            return sum(clean) / len(clean) if clean else None

        old_health, new_health = mean("sensor_health", previous), mean("sensor_health", recent)
        old_signatures = [row.get("topology_signature") for row in previous]
        new_signatures = [row.get("topology_signature") for row in recent]
        stable_old = len(set(old_signatures)) == 1 and old_signatures[0] is not None
        stable_new = len(set(new_signatures)) == 1 and new_signatures[0] is not None
        return {
            "ready": True,
            "old_sensor_health": old_health,
            "new_sensor_health": new_health,
            "health_drop": (old_health - new_health) if old_health is not None and new_health is not None else 0.0,
            "new_unavailable_fraction": mean("unavailable_fraction", recent),
            "new_sensor_count": int(recent[-1].get("sensor_count") or 0),
            "topology_changed": bool(stable_old and stable_new and old_signatures[0] != new_signatures[0]),
            "old_topology": old_signatures[0] if stable_old else None,
            "new_topology": new_signatures[0] if stable_new else None,
            "latest_preference_revision": int(recent[-1].get("preference_revision") or 0),
            "latest_preference_ts": _finite(recent[-1].get("preference_latest_ts")),
        }

    def retain_regression_anchors(self, agent_id, episode_ids, reason="pre_drift_baseline"):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            for episode_id in list(episode_ids or [])[:8]:
                c.execute(
                    """INSERT OR IGNORE INTO adaptation_regression_anchors
                       (agent_id,episode_id,reason,retained_ts,training_weight) VALUES(?,?,?,?,0)""",
                    (str(agent_id), str(episode_id), str(reason), now),
                )

    def regression_anchors(self, agent_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM adaptation_regression_anchors WHERE agent_id=? ORDER BY retained_ts,episode_id",
                (str(agent_id),),
            ).fetchall()]

    def detect(self, agent_id):
        aid = str(agent_id)
        state = self._state(aid)
        episode = self._episode_shift(aid)
        env = self._environment_shift(aid)
        kind = reason = None
        score = 0.0
        if env.get("ready"):
            sensor_count = int(env.get("new_sensor_count") or 0)
            new_health = env.get("new_sensor_health")
            persistent_bad = sensor_count > 0 and (
                (
                    new_health is not None and new_health < SENSOR_HEALTH_BAD
                    and float(env.get("health_drop") or 0.0) >= SENSOR_HEALTH_DROP
                )
                or float(env.get("new_unavailable_fraction") or 0.0) >= 0.40
            )
            if persistent_bad:
                kind = "sensor_failure"
                reason = "sensor health degraded persistently across independent runtime snapshots"
                score = max(float(env.get("health_drop") or 0.0), float(env.get("new_unavailable_fraction") or 0.0))
            elif env.get("topology_changed"):
                kind = "topology_change"
                reason = "stable sensor-to-area topology signature changed"
                score = 1.0
            elif int(env.get("latest_preference_revision") or 0) > int(state.get("preference_revision_seen") or 0):
                kind = "new_preference"
                reason = "new durable persistent preference was recorded; instructions do not decay"
                score = 1.0
        if kind is None and episode.get("ready"):
            quality_drop = float(episode.get("quality_drop") or 0.0)
            shifted = (
                float(episode.get("hour_shift") or 0.0) >= HOUR_SHIFT_THRESHOLD
                or float(episode.get("context_tv") or 0.0) >= CONTEXT_TV_THRESHOLD
            )
            corrected = int(episode.get("recent_corrections") or 0) >= 2
            if quality_drop >= QUALITY_DROP_THRESHOLD and (shifted or corrected):
                kind = "new_habit"
                reason = "episode quality dropped together with a sustained context/time/correction shift"
                score = max(
                    quality_drop,
                    float(episode.get("context_tv") or 0.0),
                    min(1.0, float(episode.get("hour_shift") or 0.0) / 6.0),
                )
        return {
            "contract_version": CONTRACT_VERSION,
            "agent_id": aid,
            "detected": bool(kind),
            "kind": kind,
            "reason": reason,
            "score": score,
            "episode_shift": episode,
            "environment_shift": env,
            "decay": decay_contract(),
        }

    def _active_candidate(self, root_id):
        with self.store.conn() as c:
            if not _table_exists(c, "agent_candidate_generations"):
                return None
            row = c.execute(
                """SELECT * FROM agent_candidate_generations
                   WHERE root_agent_id=? AND generation_type='candidate'
                     AND lifecycle_state NOT IN ('discarded','pruned','promoted','rolled_back')
                     AND agent_id IS NOT NULL
                   ORDER BY generation_number DESC,created_ts DESC LIMIT 1""",
                (str(root_id),),
            ).fetchone()
        return dict(row) if row else None

    def _ensure_candidate(self, agent_id, detection):
        existing = self._active_candidate(agent_id)
        if existing:
            return existing
        self.manager.enqueue(str(agent_id), reason=f"drift:{detection['kind']}")
        return self._active_candidate(agent_id)

    def _start_adaptation(self, agent_id, detection):
        aid = str(agent_id)
        state = self._state(aid)
        if state.get("status") in ("candidate_active", "promoted_monitoring"):
            return self.status(aid)
        episode = detection.get("episode_shift") or {}
        candidate = self._ensure_candidate(aid, detection)
        if not candidate:
            return self.status(aid)
        baseline_ids = episode.get("baseline_episode_ids") or []
        self.retain_regression_anchors(aid, baseline_ids, reason=f"pre_{detection['kind']}_baseline")
        env = detection.get("environment_shift") or {}
        self._update_state(
            aid,
            status="candidate_active",
            drift_kind=detection.get("kind"),
            drift_reason=detection.get("reason"),
            drift_score=float(detection.get("score") or 0.0),
            detected_ts=time.time(),
            baseline_quality=_finite(episode.get("baseline_quality"), _finite(state.get("baseline_quality"))),
            baseline_episode_count=len(baseline_ids),
            candidate_generation_id=candidate.get("generation_id"),
            candidate_agent_id=candidate.get("agent_id"),
            preference_revision_seen=max(
                int(state.get("preference_revision_seen") or 0),
                int(env.get("latest_preference_revision") or 0),
            ),
            promoted_ts=None,
            rollback_backup_id=None,
            recovered_ts=None,
            episodes_to_recover=None,
        )
        self.store.event(
            aid, "warning", "controlled_drift_adaptation_started",
            "Sustained drift created an isolated Candidate; Live policy was not reset",
            {
                "kind": detection.get("kind"),
                "score": detection.get("score"),
                "candidate_generation_id": candidate.get("generation_id"),
                "candidate_can_dispatch": False,
                "promotion": "existing Stage-13 fixed future evidence remains mandatory",
            },
        )
        return self.status(aid)

    def mark_promoted(self, agent_id, generation_id):
        aid = str(agent_id)
        state = self._state(aid)
        if str(state.get("candidate_generation_id") or "") != str(generation_id or ""):
            return False
        now = time.time()
        with self.store.conn() as c:
            backup = c.execute(
                """SELECT id FROM agent_generation_backups
                   WHERE agent_id=? AND expires_ts>? ORDER BY id DESC LIMIT 1""",
                (aid, now),
            ).fetchone()
        self._update_state(
            aid,
            status="promoted_monitoring",
            promoted_ts=now,
            rollback_backup_id=(int(backup[0]) if backup else None),
        )
        self.store.event(
            aid, "info", "controlled_drift_candidate_promoted",
            "Adaptation Candidate promoted; post-promotion recovery/rollback monitoring started",
            {"generation_id": generation_id, "rollback_backup_id": int(backup[0]) if backup else None},
        )
        return True

    def _post_promotion_rows(self, agent_id, promoted_ts):
        return self._episode_rows(
            agent_id,
            limit=POST_PROMOTION_EPISODES,
            after_ts=float(promoted_ts or 0.0),
            quality_only=True,
        )

    def _restore_backup(self, agent_id, backup_id):
        aid = str(agent_id)
        now = time.time()
        with self.store.conn() as c:
            backup = c.execute(
                "SELECT * FROM agent_generation_backups WHERE id=? AND agent_id=? AND expires_ts>?",
                (int(backup_id), aid, now),
            ).fetchone()
        if not backup:
            raise RuntimeError("Adaptation rollback backup is unavailable or expired")
        backup = dict(backup)
        old_agent = _json(backup.get("agent_json"), {})
        old_model_json = backup.get("model_json")
        current = self.store.get_agent_config(aid)
        if not current:
            raise RuntimeError("Root Live agent unavailable during adaptation rollback")
        executor = self.engine.executor
        old_mode = str(old_agent.get("mode") or "shadow")
        current_mode = str(current.get("mode") or "shadow")
        with executor.target_lock(current["target_entity"]):
            if current_mode == "control" and old_mode != "control":
                executor.release_control(current, reason="adaptation_quality_rollback")
            stamp = iso_now()
            now = time.time()
            with self.store.lock, self.store.conn() as c:
                if old_model_json:
                    c.execute(
                        """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                           ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json,updated_at=excluded.updated_at""",
                        (aid, str(old_model_json), stamp),
                    )
                else:
                    c.execute("DELETE FROM rl_models WHERE agent_id=?", (aid,))
                c.execute(
                    """UPDATE agents SET mode=?,training_state=?,benchmark_score=?,benchmark_samples=?,
                       benchmark_source=?,benchmark_detail_json=?,benchmark_updated_at=?,training_cursor_ts=?,
                       training_window_start_ts=?,training_window_end_ts=?,training_progress=?,training_updated_at=?
                       WHERE id=?""",
                    (
                        old_mode if old_mode in ("shadow", "control") else "shadow",
                        old_agent.get("training_state") or "qualified",
                        old_agent.get("benchmark_score"), int(old_agent.get("benchmark_samples") or 0),
                        old_agent.get("benchmark_source"), old_agent.get("benchmark_detail_json") or "{}",
                        old_agent.get("benchmark_updated_at"), old_agent.get("training_cursor_ts"),
                        old_agent.get("training_window_start_ts"), old_agent.get("training_window_end_ts"),
                        float(old_agent.get("training_progress") or 0.0), stamp, aid,
                    ),
                )
                c.execute(
                    """INSERT INTO agent_generation_state(agent_id,generation,updated_ts) VALUES(?,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET generation=excluded.generation,updated_ts=excluded.updated_ts""",
                    (aid, int(backup.get("generation") or 0), now),
                )
                if _table_exists(c, "agent_candidate_generations"):
                    current_live = c.execute(
                        """SELECT * FROM agent_candidate_generations
                           WHERE root_agent_id=? AND generation_type='live' AND agent_id=?
                           ORDER BY generation_number DESC LIMIT 1""", (aid, aid)
                    ).fetchone()
                    previous = c.execute(
                        """SELECT * FROM agent_candidate_generations
                           WHERE root_agent_id=? AND generation_number=? AND generation_type='live'
                             AND agent_id IS NULL ORDER BY updated_ts DESC LIMIT 1""",
                        (aid, int(backup.get("generation") or 0)),
                    ).fetchone()
                    if current_live and previous:
                        # The failed promoted generation remains immutable provenance but is
                        # inactive. "promoted" is already an inactive lineage state.
                        c.execute(
                            """UPDATE agent_candidate_generations SET agent_id=NULL,generation_type='candidate',
                               lifecycle_state='promoted',retired_ts=?,updated_ts=? WHERE generation_id=?""",
                            (now, now, current_live["generation_id"]),
                        )
                        c.execute(
                            """UPDATE agent_candidate_generations SET agent_id=?,generation_type='live',
                               lifecycle_state='live',retired_ts=NULL,updated_ts=? WHERE generation_id=?""",
                            (aid, now, previous["generation_id"]),
                        )
            self.store.touch_agent_index()
            self.engine.models.pop(aid, None)
            self.engine.runtime.pop(aid, None)
            if current_mode != "control" and old_mode == "control":
                restored = self.store.get_agent_config(aid)
                try:
                    executor.take_control(restored, refresh=True)
                except Exception:
                    with self.store.lock, self.store.conn() as c:
                        c.execute("UPDATE agents SET mode='shadow' WHERE id=?", (aid,))
                    self.store.touch_agent_index()
                    raise
        return True

    def rollback_adaptation(self, agent_id, reason="post_promotion_quality_regression"):
        state = self._state(agent_id)
        backup_id = state.get("rollback_backup_id")
        if backup_id is None:
            raise RuntimeError("No adaptation rollback snapshot is available")
        self._restore_backup(agent_id, int(backup_id))
        self._update_state(str(agent_id), status="rolled_back", recovered_ts=None)
        self.store.event(
            str(agent_id), "warning", "controlled_drift_adaptation_rolled_back",
            "Adaptation generation degraded after promotion; previous Live snapshot restored",
            {"reason": reason, "backup_id": int(backup_id)},
        )
        return self.status(agent_id)

    def _post_promotion_monitor(self, agent_id):
        state = self._state(agent_id)
        if state.get("status") != "promoted_monitoring" or state.get("promoted_ts") is None:
            return None
        rows = self._post_promotion_rows(agent_id, state["promoted_ts"])
        if len(rows) < POST_PROMOTION_EPISODES:
            return None
        recent = rows[-POST_PROMOTION_EPISODES:]
        quality = sum(float(row["quality"]) for row in recent) / len(recent)
        corrections = sum(int(row.get("correction_count") or 0) for row in recent)
        baseline = _finite(state.get("baseline_quality"))
        if baseline is not None and quality >= baseline - RECOVERY_MARGIN and corrections == 0:
            self._update_state(
                str(agent_id), status="recovered", recovered_ts=time.time(),
                episodes_to_recover=len(rows),
            )
            self.store.event(
                str(agent_id), "info", "controlled_drift_quality_recovered",
                "Adapted generation recovered pre-drift episode quality",
                {"episodes_to_recover": len(rows), "quality": quality, "baseline_quality": baseline},
            )
            return "recovered"
        if baseline is not None and (baseline - quality >= ROLLBACK_DROP_THRESHOLD or corrections >= 2):
            self.rollback_adaptation(agent_id)
            return "rolled_back"
        return None

    def observe_live(self, agent, state_map=None, now=None):
        aid = str(agent["id"])
        if aid in self._in_observe:
            return self.status(aid)
        self._in_observe.add(aid)
        try:
            now = time.time() if now is None else float(now)
            state = self._state(aid)
            self.ingest_episode_evaluator(aid)
            last = _finite(state.get("last_observe_ts"), 0.0) or 0.0
            if now - last >= OBSERVE_THROTTLE_SECONDS:
                self.record_environment(aid, now, **self._environment_from_runtime(agent, state_map))
                self._update_state(aid, last_observe_ts=now)
            self._post_promotion_monitor(aid)
            state = self._state(aid)
            detection = self.detect(aid)
            if (state.get("status") not in ("candidate_active", "promoted_monitoring")
                    and detection.get("detected")):
                self._start_adaptation(aid, detection)
            return self._status_payload(aid, detection)
        finally:
            self._in_observe.discard(aid)

    def _status_payload(self, agent_id, detection):
        state = self._state(agent_id)
        anchors = self.regression_anchors(agent_id)
        return {
            "contract_version": CONTRACT_VERSION,
            "state": state,
            "current_detection": detection,
            "regression_anchors": {
                "count": len(anchors), "training_weight": 0.0,
                "episode_ids": [row["episode_id"] for row in anchors],
            },
            "decay": decay_contract(),
            "promotion": {
                "automatic": False,
                "uses_existing_stage13_gate": True,
                "candidate_dispatch": False,
            },
            "recovery": {
                "post_promotion_min_episodes": POST_PROMOTION_EPISODES,
                "episodes_to_recover": state.get("episodes_to_recover"),
                "recovered_ts": state.get("recovered_ts"),
                "rollback_backup_id": state.get("rollback_backup_id"),
            },
        }

    def status(self, agent_id):
        return self._status_payload(str(agent_id), self.detect(agent_id))

    def schedule_observe(self, agent):
        """O(1) realtime hook: coalesce drift work for the background observer."""
        aid = str((agent or {}).get("id") or "")
        if not aid:
            return False
        with self._observer_lock:
            existed = aid in self._observer_pending
            self._observer_pending[aid] = dict(agent)
            self._observer_stats["scheduled"] += 1
            self._observer_stats["coalesced"] += int(existed)
            self._observer_stats["pending_high_water"] = max(
                int(self._observer_stats["pending_high_water"]),
                len(self._observer_pending),
            )
        self._observer_event.set()
        return True

    def observer_snapshot(self):
        with self._observer_lock:
            return {
                **self._observer_stats,
                "pending": len(self._observer_pending),
                "thread_alive": bool(self._observer_thread and self._observer_thread.is_alive()),
            }

    def _observer_loop(self):
        stop_event = getattr(self.engine, "stop_event", None)
        while stop_event is None or not stop_event.is_set():
            self._observer_event.wait(1.0)
            self._observer_event.clear()
            now = time.time()
            with self._observer_lock:
                due = [
                    (aid, agent)
                    for aid, agent in self._observer_pending.items()
                    if now - float(self._observer_last_run.get(aid) or 0.0)
                    >= OBSERVE_THROTTLE_SECONDS
                ]
                for aid, _ in due:
                    self._observer_pending.pop(aid, None)
                    self._observer_last_run[aid] = now
            for aid, agent in due:
                started = time.perf_counter()
                try:
                    with self.engine.lock:
                        states = dict(getattr(self.engine, "state_map", {}) or {})
                    self.observe_live(agent, states, now=now)
                    elapsed = (time.perf_counter() - started) * 1000.0
                    with self._observer_lock:
                        self._observer_stats["runs"] += 1
                        self._observer_stats["last_run_ms"] = elapsed
                        self._observer_stats["max_run_ms"] = max(
                            float(self._observer_stats["max_run_ms"]), elapsed
                        )
                except Exception as exc:
                    with self._observer_lock:
                        self._observer_stats["errors"] += 1
                    self.store.event(
                        aid, "warning", "controlled_drift_monitor_gap",
                        "Background drift monitor could not evaluate this runtime observation",
                        {"error": f"{type(exc).__name__}: {exc}"},
                    )

    def start_observer(self):
        if self._observer_thread is not None and self._observer_thread.is_alive():
            return self._observer_thread
        self._observer_thread = threading.Thread(
            target=self._observer_loop,
            name="adaptive-ai-drift-observer",
            daemon=True,
        )
        self._observer_thread.start()
        return self._observer_thread


def contract_descriptor():
    return {
        "version": CONTRACT_VERSION,
        "cold_start": {
            "fallback_or_shadow": True,
            "history_absence_relaxes_safety": False,
            "optional_question_budget": MAX_OPTIONAL_QUESTIONS,
        },
        "drift_inputs": [
            "episode_quality", "manual_corrections", "context_distribution",
            "sensor_health", "topology", "persistent_preference",
        ],
        "drift_classes": ["sensor_failure", "topology_change", "new_habit", "new_preference"],
        "adaptation": "isolated_candidate_only_no_live_reset",
        "promotion": "existing Stage-13 fixed future gate only; never automatic here",
        "rollback": "exact unexpired pre-promotion generation backup restored after degradation",
        "decay": decay_contract(),
        "regression_anchors": "durable evaluation references with training_weight=0",
    }


def install(manager):
    if getattr(manager, "_cold_start_drift_installed", False):
        return manager
    service = AdaptationService(manager)
    original_after = manager.after_live_process
    original_promote = manager.promote
    original_status = manager.status
    original_list_status = manager.list_status
    original_runtime_for = getattr(manager.engine, "runtime_for", None)

    def after_live_process(agent, state_map):
        result = original_after(agent, state_map)
        # Drift/adaptation diagnostics are not part of the decision deadline. Coalesce
        # them for the background observer instead of performing SQLite scans in the
        # realtime inference worker.
        service.schedule_observe(agent)
        return result

    def promote(parent_id, target_mode=None):
        result = original_promote(parent_id, target_mode)
        root_id = str((result or {}).get("agent_id") or parent_id)
        generation_id = (result or {}).get("generation_id")
        state = service._state(root_id)
        if generation_id and str(state.get("candidate_generation_id") or "") == str(generation_id):
            service.mark_promoted(root_id, generation_id)
        return result

    def decorate(result):
        if not result:
            return result
        out = dict(result)
        root_id = str(out.get("root_agent_id") or out.get("parent_agent_id") or "")
        if root_id:
            out["controlled_adaptation"] = service.status(root_id)
        out["cold_start_drift_contract"] = contract_descriptor()
        return out

    manager.after_live_process = after_live_process
    manager.promote = promote
    manager.status = lambda parent_id: decorate(original_status(parent_id))
    manager.list_status = lambda: [decorate(item) for item in (original_list_status() or []) if item]
    if callable(original_runtime_for):
        def runtime_for(agent):
            payload = dict(original_runtime_for(agent) or {})
            payload["cold_start"] = service.cold_start(agent)
            payload["controlled_adaptation"] = service.status(agent["id"])
            payload["cold_start_drift_contract"] = contract_descriptor()
            return payload
        manager.engine.runtime_for = runtime_for

    service.start_observer()
    manager.adaptation_service = service
    manager.cold_start_drift_contract = contract_descriptor()
    manager.rollback_adaptation = service.rollback_adaptation
    manager._cold_start_drift_installed = True
    return manager
