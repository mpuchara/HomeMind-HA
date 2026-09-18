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
import time
from collections import Counter

from confidence_contract import CONTRACT_VERSION as CONFIDENCE_CONTRACT_VERSION, DEFAULT_HALF_LIFE_EPISODES
from settings import OPTIONS, iso_now


CONTRACT_VERSION = 2
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
MIN_REGRESSION_ANCHORS = 2
MAX_REGRESSION_ANCHORS = 8
MAX_ANCHOR_NET_LOSSES = 0
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
    """One semantic contract for statistical evidence, instructions and regression memory."""
    return {
        "version": CONTRACT_VERSION,
        "rule": (
            "statistical evidence may decay according to its declared clock; explicit "
            "persistent instructions do not decay; retained regression anchors never train"
        ),
        "policy_learning": {
            "kind": "statistical_training_evidence",
            "basis": "wall_clock",
            "half_life_days": float(OPTIONS.get("policy_half_life_days", 30)),
            "meaning": "old statistical policy evidence gradually loses training influence",
        },
        "context_statistics": {
            "kind": "statistical_context_model",
            "basis": "wall_clock",
            "meaning": (
                "context/topology models use their own declared wall-clock half-life; "
                "their decay is diagnostic/statistical and never weakens explicit instructions"
            ),
        },
        "future_evaluation": {
            "kind": "independent_evaluation_evidence",
            "basis": "episode_order",
            "half_life_episodes": float(DEFAULT_HALF_LIFE_EPISODES),
            "confidence_contract_version": CONFIDENCE_CONTRACT_VERSION,
            "meaning": (
                "Stage-13 weighting applies outside the locked fixed future test; the "
                "locked holdout is not enlarged or healed by later evidence"
            ),
        },
        "probability_calibration": {
            "kind": "independent_probability_evidence",
            "basis": "episode_order_plus_dependency_effective_n",
            "half_life_episodes": float(DEFAULT_HALF_LIFE_EPISODES),
            "meaning": (
                "canonical probability claims use Stage-13 deduplicated/dependency-adjusted "
                "calibration, not a model's own training labels"
            ),
        },
        "persistent_instruction": {
            "kind": "instruction",
            "decays": False,
            "meaning": "explicit persistent preference is an instruction, not historical statistics",
        },
        "regression_anchor": {
            "kind": "evaluation_memory",
            "decays_for_retention": False,
            "training_weight": 0.0,
            "meaning": (
                "durable regression reference only; can veto a replay regression but is "
                "never multiplied into training weight or Stage-13 final calibration"
            ),
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
                recovery_seconds REAL,
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
                anchor_ts REAL,
                desired_action REAL,
                label_source TEXT,
                PRIMARY KEY(agent_id,episode_id)
            );

            CREATE TABLE IF NOT EXISTS adaptation_regression_reports (
                agent_id TEXT NOT NULL,
                candidate_generation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_ts REAL NOT NULL,
                PRIMARY KEY(agent_id,candidate_generation_id)
            );
            """
        )
        state_columns = {row["name"] for row in c.execute("PRAGMA table_info(adaptation_state)").fetchall()}
        if "recovery_seconds" not in state_columns:
            c.execute("ALTER TABLE adaptation_state ADD COLUMN recovery_seconds REAL")
        anchor_columns = {row["name"] for row in c.execute("PRAGMA table_info(adaptation_regression_anchors)").fetchall()}
        if "anchor_ts" not in anchor_columns:
            c.execute("ALTER TABLE adaptation_regression_anchors ADD COLUMN anchor_ts REAL")
        if "desired_action" not in anchor_columns:
            c.execute("ALTER TABLE adaptation_regression_anchors ADD COLUMN desired_action REAL")
        if "label_source" not in anchor_columns:
            c.execute("ALTER TABLE adaptation_regression_anchors ADD COLUMN label_source TEXT")


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
                "selection_reason": "missing_independent_on_evidence",
                "creates": "explicit_demonstration", "auto_dispatch": False,
            })
        if demonstrations["off"] <= 0:
            questions.append({
                "id": "cold_start_off_preference", "optional": True,
                "question": "W jednej niepewnej sytuacji: czy urządzenie powinno wtedy pozostać/przejść do OFF?",
                "selection_reason": "missing_independent_off_evidence",
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
            "automation_baseline_available": bool(automations),
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
                "stage13_final_evaluation_required": True,
                "stage13_confidence_contract_version": CONFIDENCE_CONTRACT_VERSION,
                "optional_answers_do_not_bypass_promotion_gates": True,
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

    def ingest_episode_evaluator(self, agent_id):
        aid = str(agent_id)
        with self.store.conn() as c:
            if not (_table_exists(c, "episode_evaluator_episodes")
                    and _table_exists(c, "episode_evaluator_policy_results")):
                return 0
            rows = c.execute(
                """SELECT e.episode_id,e.end_ts,e.context_json,r.metrics_json
                   FROM episode_evaluator_episodes e
                   JOIN episode_evaluator_policy_results r ON r.episode_id=e.episode_id
                   WHERE e.agent_id=? AND r.role='live' AND r.executed=1
                   ORDER BY e.end_ts,e.episode_id""", (aid,),
            ).fetchall()
        added = 0
        last_ts = None
        for raw in rows:
            row = dict(raw)
            metrics = _json(row.get("metrics_json"), {})
            quality, cost = _quality_from_metrics(metrics)
            context = _json(row.get("context_json"), {})
            ts = float(row["end_ts"])
            if self.record_episode(
                aid, row["episode_id"], ts, quality=quality, cost=cost,
                harmful=bool(metrics.get("harmful")),
                correction_count=int(metrics.get("manual_correction_count") or 0),
                context_bucket=_context_bucket(context, ts), source="episode_evaluator_live",
            ):
                added += 1
                last_ts = ts
        if last_ts is not None:
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

    def _episode_rows(self, agent_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM adaptation_episode_observations WHERE agent_id=? ORDER BY ts,episode_id",
                (str(agent_id),),
            ).fetchall()]

    def _env_rows(self, agent_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM adaptation_environment_snapshots WHERE agent_id=? ORDER BY ts,id",
                (str(agent_id),),
            ).fetchall()]

    def _episode_shift(self, agent_id):
        rows = [row for row in self._episode_rows(agent_id) if row.get("quality") is not None]
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
        rows = self._env_rows(agent_id)
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

    def _regression_anchor_label(self, episode_id):
        """Resolve only durable independent labels; never treat automation replay as truth."""
        with self.store.conn() as c:
            if _table_exists(c, "manual_feedback_journal"):
                row = c.execute(
                    """SELECT selected_ts,correct_action,feedback_id FROM manual_feedback_journal
                       WHERE episode_id=? AND undone_ts IS NULL AND correct_action IS NOT NULL
                       ORDER BY created_ts DESC LIMIT 1""",
                    (str(episode_id),),
                ).fetchone()
                if row:
                    return {
                        "anchor_ts": _finite(row["selected_ts"]),
                        "desired_action": _finite(row["correct_action"]),
                        "label_source": "manual_feedback:" + str(row["feedback_id"]),
                    }
            if _table_exists(c, "episode_evaluator_episodes"):
                row = c.execute(
                    "SELECT start_ts,labels_json FROM episode_evaluator_episodes WHERE episode_id=?",
                    (str(episode_id),),
                ).fetchone()
                if row:
                    labels = _json(row["labels_json"], {})
                    light_need = str(labels.get("light_need") or "")
                    if light_need in ("true", "false"):
                        return {
                            "anchor_ts": float(row["start_ts"]),
                            "desired_action": 1.0 if light_need == "true" else 0.0,
                            "label_source": "episode_evaluator:light_need",
                        }
        return {"anchor_ts": None, "desired_action": None, "label_source": None}

    def retain_regression_anchors(self, agent_id, episode_ids, reason="pre_drift_baseline"):
        now = time.time()
        rows = [
            (str(episode_id), self._regression_anchor_label(episode_id))
            for episode_id in list(episode_ids or [])[:MAX_REGRESSION_ANCHORS]
        ]
        with self.store.lock, self.store.conn() as c:
            for episode_id, label in rows:
                c.execute(
                    """INSERT INTO adaptation_regression_anchors
                       (agent_id,episode_id,reason,retained_ts,training_weight,
                        anchor_ts,desired_action,label_source)
                       VALUES(?,?,?,?,0,?,?,?)
                       ON CONFLICT(agent_id,episode_id) DO UPDATE SET
                         anchor_ts=COALESCE(adaptation_regression_anchors.anchor_ts,excluded.anchor_ts),
                         desired_action=COALESCE(adaptation_regression_anchors.desired_action,excluded.desired_action),
                         label_source=COALESCE(adaptation_regression_anchors.label_source,excluded.label_source)""",
                    (
                        str(agent_id), str(episode_id), str(reason), now,
                        label.get("anchor_ts"), label.get("desired_action"), label.get("label_source"),
                    ),
                )

    def regression_anchors(self, agent_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM adaptation_regression_anchors WHERE agent_id=? ORDER BY retained_ts,episode_id",
                (str(agent_id),),
            ).fetchall()]

    @staticmethod
    def _model_revision(model):
        model = dict(model or {})
        return str(model.get("model_revision") or _hash(model) if model else "missing")

    def _replay_prediction(self, agent_id, timestamp):
        agent = self.store.get_agent_config(str(agent_id))
        if not agent or self.store.get_model(str(agent_id)) is None:
            return {"complete": False, "reason": "model_or_agent_missing"}
        teaching = getattr(self.engine, "teaching", None)
        if teaching is None or not callable(getattr(teaching, "point_context", None)):
            return {"complete": False, "reason": "historical_replay_unavailable"}
        try:
            policy = self.engine.policy(agent)
            states, temporal, _ = teaching.point_context(
                self.engine, agent, float(timestamp), policy=policy
            )
            features, _labels, meta = policy.features(states, temporal, at_ts=float(timestamp))
            if not bool((meta or {}).get("reconstruction_complete", False)):
                return {
                    "complete": False,
                    "reason": "historical_context_incomplete",
                    "details": dict(meta or {}),
                }
            prediction = policy.predict(features)[0]["value"]
            prediction = _finite(prediction)
            if prediction is None:
                return {"complete": False, "reason": "prediction_unavailable"}
            return {"complete": True, "prediction": prediction}
        except Exception as exc:
            return {"complete": False, "reason": f"{type(exc).__name__}: {exc}"}

    def regression_anchor_report(self, agent_id, candidate_agent_id, candidate_generation_id):
        aid = str(agent_id)
        candidate_id = str(candidate_agent_id or "")
        generation_id = str(candidate_generation_id or "")
        anchors = [
            row for row in self.regression_anchors(aid)
            if _finite(row.get("anchor_ts")) is not None
            and _finite(row.get("desired_action")) is not None
        ]
        parent_model = self.store.get_model(aid) or {}
        child_model = self.store.get_model(candidate_id) or {}
        fingerprint = _hash({
            "contract_version": CONTRACT_VERSION,
            "confidence_contract_version": CONFIDENCE_CONTRACT_VERSION,
            "parent_model": self._model_revision(parent_model),
            "child_model": self._model_revision(child_model),
            "anchors": [
                (row["episode_id"], row.get("anchor_ts"), row.get("desired_action"), row.get("label_source"))
                for row in anchors
            ],
        })
        with self.store.conn() as c:
            cached = c.execute(
                """SELECT report_json FROM adaptation_regression_reports
                   WHERE agent_id=? AND candidate_generation_id=? AND fingerprint=?""",
                (aid, generation_id, fingerprint),
            ).fetchone()
        if cached:
            return _json(cached["report_json"], {})

        evaluated = []
        for row in anchors:
            ts = float(row["anchor_ts"])
            desired = 1.0 if float(row["desired_action"]) >= .5 else 0.0
            parent = self._replay_prediction(aid, ts)
            child = self._replay_prediction(candidate_id, ts)
            item = {
                "episode_id": row["episode_id"],
                "anchor_ts": ts,
                "desired_action": desired,
                "label_source": row.get("label_source"),
                "parent": parent,
                "candidate": child,
            }
            if parent.get("complete") and child.get("complete"):
                p_action = 1.0 if float(parent["prediction"]) >= .5 else 0.0
                c_action = 1.0 if float(child["prediction"]) >= .5 else 0.0
                item["parent_correct"] = bool(p_action == desired)
                item["candidate_correct"] = bool(c_action == desired)
                evaluated.append(item)
            else:
                item["parent_correct"] = None
                item["candidate_correct"] = None

        parent_correct = sum(int(row["parent_correct"]) for row in evaluated)
        candidate_correct = sum(int(row["candidate_correct"]) for row in evaluated)
        child_wins = sum(
            int(row["candidate_correct"] and not row["parent_correct"]) for row in evaluated
        )
        parent_wins = sum(
            int(row["parent_correct"] and not row["candidate_correct"]) for row in evaluated
        )
        applicable = len(evaluated) >= MIN_REGRESSION_ANCHORS
        passed = bool(applicable and (parent_wins - child_wins) <= MAX_ANCHOR_NET_LOSSES)
        report = {
            "contract_version": CONTRACT_VERSION,
            "evidence_semantics": "offline_regression_replay_only_not_stage13_future_calibration",
            "training_weight": 0.0,
            "retained_anchors": len(anchors),
            "evaluated_anchors": len(evaluated),
            "minimum_evaluable_anchors": MIN_REGRESSION_ANCHORS,
            "gate_applicable": applicable,
            "passed": passed if applicable else None,
            "parent_correct": parent_correct,
            "candidate_correct": candidate_correct,
            "parent_wins": parent_wins,
            "candidate_wins": child_wins,
            "max_net_losses": MAX_ANCHOR_NET_LOSSES,
            "rows": evaluated,
            "recommendation": (
                "pass_regression_anchor_replay"
                if passed else
                "fail_regression_anchor_replay"
                if applicable else
                "insufficient_replayable_anchors_stage13_still_required"
            ),
        }
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO adaptation_regression_reports
                   (agent_id,candidate_generation_id,fingerprint,report_json,created_ts)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(agent_id,candidate_generation_id) DO UPDATE SET
                     fingerprint=excluded.fingerprint,report_json=excluded.report_json,
                     created_ts=excluded.created_ts""",
                (aid, generation_id, fingerprint,
                 json.dumps(report, separators=(",", ":"), default=str), time.time()),
            )
        return report

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
            recovery_seconds=None,
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
        return [
            row for row in self._episode_rows(agent_id)
            if row.get("quality") is not None and float(row.get("ts") or 0.0) > float(promoted_ts or 0.0)
        ]

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
            self.engine.models.pop(aid, None)
            self.engine.runtime.pop(aid, None)
            if current_mode != "control" and old_mode == "control":
                restored = self.store.get_agent_config(aid)
                try:
                    executor.take_control(restored, refresh=True)
                except Exception:
                    with self.store.lock, self.store.conn() as c:
                        c.execute("UPDATE agents SET mode='shadow' WHERE id=?", (aid,))
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
            recovered_ts = float(recent[-1]["ts"])
            recovery_seconds = max(0.0, recovered_ts - float(state["promoted_ts"]))
            self._update_state(
                str(agent_id), status="recovered", recovered_ts=recovered_ts,
                episodes_to_recover=len(rows), recovery_seconds=recovery_seconds,
            )
            self.store.event(
                str(agent_id), "info", "controlled_drift_quality_recovered",
                "Adapted generation recovered pre-drift episode quality",
                {
                    "episodes_to_recover": len(rows),
                    "seconds_to_recover": recovery_seconds,
                    "quality": quality,
                    "baseline_quality": baseline,
                },
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
            if state.get("status") not in ("candidate_active", "promoted_monitoring"):
                detection = self.detect(aid)
                if detection.get("detected"):
                    self._start_adaptation(aid, detection)
            return self.status(aid)
        finally:
            self._in_observe.discard(aid)

    def status(self, agent_id):
        state = self._state(agent_id)
        detection = self.detect(agent_id)
        anchors = self.regression_anchors(agent_id)
        promoted_ts = _finite(state.get("promoted_ts"))
        post_rows = self._post_promotion_rows(agent_id, promoted_ts) if promoted_ts is not None else []
        latest_post_ts = max([_finite(row.get("ts"), promoted_ts) for row in post_rows], default=promoted_ts)
        elapsed = (
            max(0.0, float(latest_post_ts) - promoted_ts)
            if promoted_ts is not None and latest_post_ts is not None else None
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "state": state,
            "current_detection": detection,
            "regression_anchors": {
                "count": len(anchors),
                "labeled_count": sum(_finite(row.get("desired_action")) is not None for row in anchors),
                "training_weight": 0.0,
                "episode_ids": [row["episode_id"] for row in anchors],
                "semantics": "offline replay guard only; never Stage-13 final calibration",
            },
            "decay": decay_contract(),
            "promotion": {
                "automatic": False,
                "uses_existing_stage13_gate": True,
                "stage13_confidence_contract_version": CONFIDENCE_CONTRACT_VERSION,
                "candidate_dispatch": False,
            },
            "recovery": {
                "post_promotion_min_episodes": POST_PROMOTION_EPISODES,
                "observed_post_promotion_episodes": len(post_rows),
                "monitoring_elapsed_seconds": elapsed,
                "episodes_to_recover": state.get("episodes_to_recover"),
                "seconds_to_recover": state.get("recovery_seconds"),
                "recovered_ts": state.get("recovered_ts"),
                "rollback_backup_id": state.get("rollback_backup_id"),
            },
        }


def contract_descriptor():
    return {
        "version": CONTRACT_VERSION,
        "cold_start": {
            "fallback_or_shadow": True,
            "history_absence_relaxes_safety": False,
            "optional_question_budget": MAX_OPTIONAL_QUESTIONS,
            "questions_are_optional_and_non_dispatching": True,
        },
        "drift_inputs": [
            "episode_quality", "manual_corrections", "context_distribution",
            "sensor_health", "topology", "persistent_preference",
        ],
        "drift_classes": ["sensor_failure", "topology_change", "new_habit", "new_preference"],
        "adaptation": "isolated_candidate_only_no_live_reset",
        "promotion": (
            "Stage-13 fixed future independent calibration remains mandatory; "
            "replay anchors are an additional non-training regression veto when evaluable"
        ),
        "stage13_confidence_contract_version": CONFIDENCE_CONTRACT_VERSION,
        "rollback": "exact unexpired pre-promotion generation backup restored after degradation",
        "recovery_metrics": ["episodes_to_recover", "seconds_to_recover"],
        "decay": decay_contract(),
        "regression_anchors": {
            "retention": "durable",
            "training_weight": 0.0,
            "evaluation": "cached parent-vs-candidate historical replay",
            "not_final_calibration": True,
        },
    }


def install(manager):
    if getattr(manager, "_cold_start_drift_installed", False):
        return manager
    service = AdaptationService(manager)
    original_after = manager.after_live_process
    original_promote = manager.promote
    original_summary = getattr(manager, "_comparison_summary", None)
    original_status = manager.status
    original_list_status = manager.list_status
    original_runtime_for = getattr(manager.engine, "runtime_for", None)

    def after_live_process(agent, state_map):
        result = original_after(agent, state_map)
        try:
            service.observe_live(agent, state_map)
        except Exception as exc:
            manager.store.event(
                agent.get("id"), "warning", "controlled_drift_monitor_gap",
                "Drift monitor could not evaluate this runtime observation",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        return result

    def comparison_summary(row, parent=None, candidate=None):
        out = dict(original_summary(row, parent, candidate) or {})
        root_id = str((parent or {}).get("id") or row.get("parent_agent_id") or "")
        candidate_id = str((candidate or {}).get("id") or row.get("candidate_id") or "")
        if not root_id or not candidate_id:
            return out
        state = service._state(root_id)
        if (
            state.get("status") == "candidate_active"
            and str(state.get("candidate_agent_id") or "") == candidate_id
            and state.get("candidate_generation_id")
        ):
            report = service.regression_anchor_report(
                root_id, candidate_id, state.get("candidate_generation_id")
            )
            out["drift_regression_anchor_report"] = report
            if report.get("gate_applicable"):
                passed = bool(report.get("passed"))
                gates = dict(out.get("promotion_gates") or {})
                gates["drift_regression_anchors"] = {
                    "passed": passed,
                    "reason": (
                        "Candidate preserved retained zero-weight regression anchors"
                        if passed else
                        "Candidate regressed on retained zero-weight regression anchors"
                    ),
                    "custom_override": "never",
                    "metric_semantics": "offline_regression_replay_not_future_calibration",
                    "observed": report,
                }
                out["promotion_gates"] = gates
                out["promotable"] = bool(out.get("promotable")) and passed
        return out

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
    if callable(original_summary):
        manager._comparison_summary = comparison_summary
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

    manager.adaptation_service = service
    manager.cold_start_drift_contract = contract_descriptor()
    manager.rollback_adaptation = service.rollback_adaptation
    manager._cold_start_drift_installed = True
    return manager
