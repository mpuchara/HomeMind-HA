"""Stage 6 trusted Automatic Correct outcome/reward journal.

This module is deliberately observer-only. It watches the existing
ActionIntent -> Executor -> real-world outcome path and persists auditable outcome facts
for later Offline RL. It never updates a policy, never dispatches a Home Assistant
service and never converts Manual Correct labels into scalar rewards.

Only strongly attributable outcomes become trusted:
* an explicit user reversal of the exact controlled target;
* a verified same-area presence source changing inside the exact observation window.

Silence / lack of override is persisted as unknown even when the legacy contextual
bandit currently assigns a small acceptance reward. Cross-area or non-authoritative
signals are rejected instead of becoming positive reward.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time

from control import timing_for
from home_sources import source_kind
from observation_space import observation_as_of, select_observation_mask
from settings import OPTIONS

CONTRACT_VERSION = 1
STATUSES = {"pending", "trusted", "unknown", "rejected"}
_JSON_FIELDS = {
    "observation_json": ("observation", {}),
    "observation_mask_json": ("observation_mask", {}),
    "prediction_inputs_json": ("prediction_inputs", []),
    "background_dependencies_json": ("background_dependencies", []),
    "outcome_sources_json": ("outcome_sources", {}),
    "reward_sources_json": ("reward_sources", []),
    "metadata_json": ("metadata", {}),
}


def _dumps(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def _loads(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _resolution_key(*, agent_id, decision_id=None, trial_id=None,
                    action_ts=None, action_value=None):
    if trial_id:
        return "trial:" + str(trial_id)
    if decision_id:
        return "decision:" + str(decision_id)
    raw = _dumps([
        str(agent_id),
        None if action_ts is None else float(action_ts),
        _finite(action_value),
    ])
    return "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _active_state(state):
    raw = str((state or {}).get("state") or "").lower()
    return raw in {
        "on", "home", "open", "opening", "occupied", "detected", "true",
    }


def _source_reliability(kind):
    if kind == "binary":
        return 0.95
    if kind == "tracker":
        return 0.90
    return 0.0


class AutomaticRewardJournal:
    """Durable one-resolution-per-action/trial reward buffer with RAM diagnostics."""

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self._lock = threading.RLock()
        self._latest = {}
        self._counts = {}
        self._migrate()
        self._interrupt_stale_pending()
        self._warm_cache()

    def _migrate(self):
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS automatic_reward_experiences (
                    resolution_key TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    created_ts REAL NOT NULL,
                    resolved_ts REAL,
                    status TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    generation_id TEXT,
                    decision_id TEXT,
                    trial_id TEXT,
                    action_index INTEGER,
                    action_value REAL,
                    action_ts REAL NOT NULL,
                    observation_start REAL NOT NULL,
                    observation_end REAL NOT NULL,
                    target_entity TEXT NOT NULL,
                    target_property TEXT NOT NULL,
                    area_id TEXT,
                    observation_schema_id TEXT,
                    observation_mask_id TEXT,
                    observation_json TEXT NOT NULL DEFAULT '{}',
                    observation_mask_json TEXT NOT NULL DEFAULT '{}',
                    prediction_inputs_json TEXT NOT NULL DEFAULT '[]',
                    background_dependencies_json TEXT NOT NULL DEFAULT '[]',
                    outcome_sources_json TEXT NOT NULL DEFAULT '{}',
                    reward_sources_json TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT,
                    proposed_reward REAL,
                    trusted_reward REAL,
                    confidence REAL NOT NULL DEFAULT 0,
                    attribution_reason TEXT,
                    source_entity_id TEXT,
                    source_event_id TEXT,
                    source_origin TEXT,
                    source_reliability REAL NOT NULL DEFAULT 0,
                    unknown_reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_auto_reward_agent_time
                    ON automatic_reward_experiences(agent_id,created_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_auto_reward_status_time
                    ON automatic_reward_experiences(status,resolved_ts DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_reward_trial_once
                    ON automatic_reward_experiences(trial_id)
                    WHERE trial_id IS NOT NULL AND trial_id!='';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_reward_decision_once
                    ON automatic_reward_experiences(decision_id)
                    WHERE decision_id IS NOT NULL AND decision_id!='';
                """
            )

    def _interrupt_stale_pending(self):
        now = float(self.clock())
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE automatic_reward_experiences
                   SET status='unknown',resolved_ts=?,outcome='unknown',
                       attribution_reason='restart_interrupted',
                       unknown_reason='restart interrupted observation before a trustworthy outcome'
                   WHERE status='pending'""",
                (now,),
            )

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        out = dict(row)
        for source, (target, default) in _JSON_FIELDS.items():
            out[target] = _loads(out.pop(source, None), default)
        return out

    def _warm_cache(self):
        counts = {}
        latest = {}
        with self.store.conn() as c:
            for row in c.execute(
                """SELECT agent_id,status,COUNT(*) AS n
                   FROM automatic_reward_experiences GROUP BY agent_id,status"""
            ).fetchall():
                counts.setdefault(str(row["agent_id"]), {})[
                    str(row["status"])
                ] = int(row["n"])
            rows = c.execute(
                """SELECT * FROM automatic_reward_experiences
                   ORDER BY COALESCE(resolved_ts,created_ts) DESC LIMIT 1024"""
            ).fetchall()
        for row in rows:
            decoded = self._decode(row)
            latest.setdefault(str(decoded["agent_id"]), decoded)
        with self._lock:
            self._counts = counts
            self._latest = latest

    def _cache_transition(self, row, previous_status=None):
        aid = str(row["agent_id"])
        status = str(row["status"])
        with self._lock:
            bucket = self._counts.setdefault(aid, {})
            if previous_status and previous_status != status:
                bucket[previous_status] = max(
                    0, int(bucket.get(previous_status, 0)) - 1
                )
                bucket[status] = int(bucket.get(status, 0)) + 1
            elif previous_status is None:
                bucket[status] = int(bucket.get(status, 0)) + 1
            self._latest[aid] = dict(row)

    def get(self, resolution_key):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM automatic_reward_experiences WHERE resolution_key=?",
                (str(resolution_key),),
            ).fetchone()
        return self._decode(row)

    def start(self, payload):
        row = dict(payload or {})
        key = str(row["resolution_key"])
        now = float(row.get("created_ts") or self.clock())
        values = (
            key, CONTRACT_VERSION, now, "pending", str(row["agent_id"]),
            row.get("generation_id"), row.get("decision_id"), row.get("trial_id"),
            row.get("action_index"), _finite(row.get("action_value")),
            float(row["action_ts"]), float(row["observation_start"]),
            float(row["observation_end"]), str(row["target_entity"]),
            str(row["target_property"]), row.get("area_id"),
            row.get("observation_schema_id"), row.get("observation_mask_id"),
            _dumps(row.get("observation") or {}),
            _dumps(row.get("observation_mask") or {}),
            _dumps(row.get("prediction_inputs") or []),
            _dumps(row.get("background_dependencies") or []),
            _dumps(row.get("outcome_sources") or {}),
            _dumps(row.get("reward_sources") or []),
            _dumps(row.get("metadata") or {}),
        )
        with self.store.lock, self.store.conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO automatic_reward_experiences
                   (resolution_key,contract_version,created_ts,status,agent_id,
                    generation_id,decision_id,trial_id,action_index,action_value,
                    action_ts,observation_start,observation_end,target_entity,
                    target_property,area_id,observation_schema_id,observation_mask_id,
                    observation_json,observation_mask_json,prediction_inputs_json,
                    background_dependencies_json,outcome_sources_json,reward_sources_json,
                    metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            inserted = bool(cur.rowcount)
            saved = c.execute(
                "SELECT * FROM automatic_reward_experiences WHERE resolution_key=?",
                (key,),
            ).fetchone()
        decoded = self._decode(saved)
        if inserted:
            self._cache_transition(decoded)
        return decoded, inserted

    def resolve(self, resolution_key, *, status, outcome, proposed_reward=None,
                trusted_reward=None, confidence=0.0, attribution_reason=None,
                source_entity_id=None, source_event_id=None, source_origin=None,
                source_reliability=0.0, unknown_reason=None, outcome_sources=None,
                reward_sources=None, metadata=None, resolved_ts=None):
        status = str(status)
        if status not in STATUSES - {"pending"}:
            raise ValueError("invalid Automatic Correct resolution status")
        key = str(resolution_key)
        now = float(self.clock() if resolved_ts is None else resolved_ts)
        current = self.get(key)
        if current is None:
            return None, False
        if current["status"] != "pending":
            return current, False
        merged_meta = dict(current.get("metadata") or {})
        merged_meta.update(dict(metadata or {}))
        sources = (
            current.get("outcome_sources")
            if outcome_sources is None else outcome_sources
        )
        reward_src = (
            current.get("reward_sources")
            if reward_sources is None else reward_sources
        )
        with self.store.lock, self.store.conn() as c:
            cur = c.execute(
                """UPDATE automatic_reward_experiences SET
                     resolved_ts=?,status=?,outcome=?,proposed_reward=?,trusted_reward=?,
                     confidence=?,attribution_reason=?,source_entity_id=?,source_event_id=?,
                     source_origin=?,source_reliability=?,unknown_reason=?,
                     outcome_sources_json=?,reward_sources_json=?,metadata_json=?
                   WHERE resolution_key=? AND status='pending'""",
                (
                    now, status, str(outcome), _finite(proposed_reward),
                    _finite(trusted_reward),
                    max(0.0, min(1.0, float(confidence or 0.0))),
                    attribution_reason, source_entity_id, source_event_id,
                    source_origin,
                    max(0.0, min(1.0, float(source_reliability or 0.0))),
                    unknown_reason, _dumps(sources or {}),
                    _dumps(reward_src or []), _dumps(merged_meta), key,
                ),
            )
            changed = bool(cur.rowcount)
            saved = c.execute(
                "SELECT * FROM automatic_reward_experiences WHERE resolution_key=?",
                (key,),
            ).fetchone()
        decoded = self._decode(saved)
        if changed:
            self._cache_transition(decoded, previous_status="pending")
        return decoded, changed

    def summary(self, agent_id):
        aid = str(agent_id)
        with self._lock:
            counts = dict(self._counts.get(aid) or {})
            latest = dict(self._latest.get(aid) or {})
        compact = None
        if latest:
            compact = {
                key: latest.get(key) for key in (
                    "resolution_key", "status", "outcome", "proposed_reward",
                    "trusted_reward", "confidence", "attribution_reason",
                    "source_entity_id", "source_event_id", "source_origin",
                    "source_reliability", "unknown_reason", "trial_id",
                    "decision_id", "action_ts", "observation_start",
                    "observation_end", "area_id", "observation_schema_id",
                    "observation_mask_id",
                )
            }
            compact["reward_sources"] = list(
                latest.get("reward_sources") or []
            )
        return {
            "contract_version": CONTRACT_VERSION,
            "learning_enabled": False,
            "mode": "trusted_outcome_reward_buffer_only",
            "counts": {
                "pending": int(counts.get("pending", 0)),
                "trusted": int(counts.get("trusted", 0)),
                "unknown": int(counts.get("unknown", 0)),
                "rejected": int(counts.get("rejected", 0)),
            },
            "latest": compact,
        }

    def recent(self, agent_id, limit=50):
        limit = max(1, min(200, int(limit)))
        with self.store.conn() as c:
            rows = c.execute(
                """SELECT * FROM automatic_reward_experiences
                   WHERE agent_id=?
                   ORDER BY COALESCE(resolved_ts,created_ts) DESC LIMIT ?""",
                (str(agent_id), limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def trusted(self, *, limit=4096):
        limit = max(1, min(20000, int(limit)))
        with self.store.conn() as c:
            rows = c.execute(
                """SELECT * FROM automatic_reward_experiences
                   WHERE status='trusted' AND trusted_reward IS NOT NULL
                   ORDER BY resolved_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._decode(row) for row in rows]


class TrustedAutomaticRewardService:
    def __init__(self, core):
        self.core = core
        self.engine = core.ENGINE
        self.store = core.STORE
        self.journal = AutomaticRewardJournal(self.store)
        self._mask_cache = {}
        self._lock = threading.RLock()
        self.min_confidence = float(
            OPTIONS.get("automatic_correct_min_confidence", 0.80)
        )
        self.min_source_reliability = float(
            OPTIONS.get("automatic_correct_min_source_reliability", 0.80)
        )

    def _generation_id(self, agent_id):
        provenance = getattr(self.engine, "provenance", None)
        row = (
            provenance.generation_for_agent(agent_id)
            if provenance is not None else None
        )
        return (row or {}).get("generation_id")

    def _mask(self, agent, states, registry):
        aid = str(agent["id"])
        policy = self.engine.policy(agent)
        existing = getattr(policy, "observation_mask", None)
        if existing is not None:
            return existing
        with self._lock:
            cached = self._mask_cache.get(aid)
        if cached is not None:
            return cached
        mask, _diagnostics = select_observation_mask(
            agent, states, registry, (),
            relevance_scores=getattr(
                self.engine, "context_relevance", {}
            ).get(aid),
        )
        with self._lock:
            self._mask_cache[aid] = mask
        return mask

    def _observation(self, agent, at_ts):
        with self.engine.lock:
            states = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        mask = self._mask(agent, states, registry)
        observation = observation_as_of(
            mask, states, self.engine.temporal_history, float(at_ts), agent,
            home_provider=self.engine.context,
        )
        return mask, observation, states, registry

    @staticmethod
    def _experiment_trial(engine, aid):
        try:
            return dict(
                (engine.experiments._get(aid).get("active") or {})
            )
        except Exception:
            return {}

    def start_action(self, intent, result):
        if (
            not isinstance(result, dict)
            or str(result.get("status") or "") != "ACCEPTED"
        ):
            return None
        agent = self.store.get_agent_config(intent.agent_id)
        if not agent:
            return None
        rt = self.engine.runtime.get(agent["id"]) or {}
        pending = rt.get("pending")
        if not isinstance(pending, dict):
            return None
        action_ts = float(
            pending.get("started_ts") or intent.created_at
        )
        trial = (
            self._experiment_trial(self.engine, agent["id"])
            if intent.experiment_token else {}
        )
        trial_id = trial.get("trial_id")
        decision_id = pending.get("decision_id") or intent.intent_id
        mask, observation, _states, registry = self._observation(
            agent, action_ts
        )
        target_area = (
            registry.get(agent["target_entity"], {}) or {}
        ).get("area_id")
        timing = timing_for(agent)
        default_end = action_ts + max(
            float(OPTIONS.get("reward_window_seconds", 90)),
            float(timing.acknowledgement + timing.settling),
            float(pending.get("horizon") or 0.0)
            + float(timing.settling),
        )
        start = float(
            trial.get("observation_start") or action_ts
        )
        end = float(
            trial.get("observation_end") or default_end
        )
        dependency_entities = [
            str(item[0])
            for item in (
                getattr(intent, "context_dependencies", ()) or ()
            )
            if item and item[0]
        ]
        prediction_inputs = list(mask.selected_entities)
        background = sorted(
            set(dependency_entities)
            - set(prediction_inputs)
            - {str(agent["target_entity"])}
        )
        key = _resolution_key(
            agent_id=agent["id"], decision_id=decision_id,
            trial_id=trial_id, action_ts=action_ts,
            action_value=pending.get("action_value"),
        )
        payload = {
            "resolution_key": key,
            "agent_id": agent["id"],
            "generation_id": self._generation_id(agent["id"]),
            "decision_id": decision_id,
            "trial_id": trial_id,
            "action_index": pending.get("action_index"),
            "action_value": pending.get("action_value"),
            "action_ts": action_ts,
            "observation_start": start,
            "observation_end": end,
            "target_entity": agent["target_entity"],
            "target_property": agent["target_property"],
            "area_id": target_area,
            "observation_schema_id": mask.schema_id,
            "observation_mask_id": mask.mask_id,
            "observation": observation,
            "observation_mask": mask.export(),
            "prediction_inputs": prediction_inputs,
            "background_dependencies": background,
            "outcome_sources": dict(
                trial.get("outcome_sources") or {}
            ),
            "reward_sources": [],
            "metadata": {
                "decision_source": getattr(
                    intent, "decision_source", None
                ),
                "intent_confidence": getattr(
                    intent, "confidence", None
                ),
                "intent_support": getattr(
                    intent, "support", None
                ),
                "intent_novelty": getattr(
                    intent, "novelty", None
                ),
                "experiment_kind": trial.get("kind"),
                "experiment_focus": trial.get("focus"),
                "legacy_pending_horizon": pending.get("horizon"),
            },
        }
        saved, inserted = self.journal.start(payload)
        if inserted:
            self.store.event(
                agent["id"], "info",
                "automatic_correct_observation_started",
                "Automatic Correct observation window started",
                {
                    "resolution_key": key,
                    "decision_id": decision_id,
                    "trial_id": trial_id,
                    "observation_start": start,
                    "observation_end": end,
                    "learning_enabled": False,
                },
            )
        return saved

    def _row_for_pending(self, agent, pending):
        if not isinstance(pending, dict):
            return None
        key = _resolution_key(
            agent_id=agent["id"],
            decision_id=pending.get("decision_id"),
            trial_id=pending.get("experiment_id"),
            action_ts=pending.get("started_ts"),
            action_value=pending.get("action_value"),
        )
        return self.journal.get(key)

    def _latest_target_user_event(self, row, user_id=None):
        eid = str(row["target_entity"])
        latest = getattr(
            self.engine, "_provenance_latest_events", {}
        ).get(eid)
        if not latest:
            return None
        event_time, event_id, origin = latest
        if not (
            float(row["observation_start"])
            <= float(event_time)
            <= float(row["observation_end"]) + 1.0
        ):
            return None
        provenance = getattr(self.engine, "provenance", None)
        event = (
            provenance.event(event_id)
            if provenance is not None else None
        )
        event = dict(event or {})
        actual_origin = str(
            event.get("origin") or origin or "unknown"
        )
        if actual_origin not in {"user", "user_intent"}:
            return None
        if user_id is not None and not event.get("user_id"):
            return None
        return {
            "entity_id": eid,
            "event_id": str(event_id),
            "origin": actual_origin,
            "reliability": 1.0,
        }

    def _presence_event(self, row, *, allowed_sources=None):
        with self.engine.lock:
            states = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        area = row.get("area_id")
        if not area:
            return None, "target area is unknown"
        candidates = set(
            allowed_sources
            or row.get("prediction_inputs")
            or []
        )
        valid = []
        rejected = []
        latest_events = getattr(
            self.engine, "_provenance_latest_events", {}
        )
        for eid in sorted(candidates):
            state = states.get(eid)
            reg = registry.get(eid, {}) or {}
            kind, _reason = source_kind(eid, state, reg)
            if kind not in {"binary", "tracker"}:
                continue
            source_area = reg.get("area_id")
            latest = latest_events.get(eid)
            if not latest or not _active_state(state):
                continue
            event_time, event_id, origin = latest
            if not (
                float(row["observation_start"])
                <= float(event_time)
                <= float(row["observation_end"])
            ):
                continue
            detail = {
                "entity_id": str(eid),
                "event_id": str(event_id),
                "origin": str(origin or "unknown"),
                "reliability": _source_reliability(kind),
                "area_id": source_area,
                "kind": kind,
                "event_time": float(event_time),
            }
            if source_area and source_area == area:
                valid.append(detail)
            else:
                rejected.append(detail)
        if valid:
            valid.sort(
                key=lambda item: (
                    item["event_time"], item["entity_id"]
                )
            )
            return valid[0], None
        if rejected:
            return (
                None,
                "presence-like outcome source belongs to another area",
            )
        return (
            None,
            "no verified same-area outcome source changed inside "
            "the observation window",
        )

    def _trusted(self, *, reward, confidence, source_reliability):
        return (
            reward is not None
            and float(confidence) >= self.min_confidence
            and float(source_reliability)
            >= self.min_source_reliability
        )

    def resolve_runtime(
        self, agent, rt, reward, reason,
        user_id=None, experience=None,
    ):
        pending = dict(
            experience or rt.get("pending") or {}
        )
        row = self._row_for_pending(agent, pending)
        if row is None:
            return None
        proposed = _finite(reward)
        components = dict(
            rt.get("reward_components_pending") or {}
        )
        reason_text = str(reason or "")
        metadata = {
            "legacy_reward_components": components,
            "legacy_reason": reason_text,
        }

        if reason_text == "manual correction":
            source = self._latest_target_user_event(
                row, user_id=user_id
            )
            if source and self._trusted(
                reward=proposed,
                confidence=1.0,
                source_reliability=source["reliability"],
            ):
                return self.journal.resolve(
                    row["resolution_key"],
                    status="trusted",
                    outcome="explicit_user_reversal",
                    proposed_reward=proposed,
                    trusted_reward=proposed,
                    confidence=1.0,
                    attribution_reason=(
                        "explicit user reversed the exact controlled target"
                    ),
                    source_entity_id=source["entity_id"],
                    source_event_id=source["event_id"],
                    source_origin=source["origin"],
                    source_reliability=source["reliability"],
                    reward_sources=["explicit_user_reversal"],
                    metadata=metadata,
                )[0]
            return self.journal.resolve(
                row["resolution_key"],
                status="unknown",
                outcome="user_reversal_unverified",
                proposed_reward=proposed,
                confidence=0.0,
                attribution_reason=(
                    "manual correction lacked verifiable "
                    "exact-target user provenance"
                ),
                unknown_reason=(
                    "cannot prove that the resolving target event "
                    "was an explicit user action"
                ),
                metadata=metadata,
            )[0]

        if reason_text in {
            "anticipation outcome",
            "completed earlier anticipation",
        }:
            source, rejected_reason = self._presence_event(row)
            if source and self._trusted(
                reward=proposed,
                confidence=source["reliability"],
                source_reliability=source["reliability"],
            ):
                return self.journal.resolve(
                    row["resolution_key"],
                    status="trusted",
                    outcome="verified_same_area_presence_outcome",
                    proposed_reward=proposed,
                    trusted_reward=proposed,
                    confidence=source["reliability"],
                    attribution_reason=(
                        "verified same-area presence transition "
                        "inside observation window"
                    ),
                    source_entity_id=source["entity_id"],
                    source_event_id=source["event_id"],
                    source_origin=source["origin"],
                    source_reliability=source["reliability"],
                    outcome_sources={
                        source["entity_id"]: {
                            "area_id": source["area_id"],
                            "kind": source["kind"],
                        }
                    },
                    reward_sources=[source["entity_id"]],
                    metadata=metadata,
                )[0]
            status = (
                "rejected"
                if rejected_reason
                and "another area" in rejected_reason
                else "unknown"
            )
            return self.journal.resolve(
                row["resolution_key"],
                status=status,
                outcome="anticipation_outcome_unattributed",
                proposed_reward=proposed,
                confidence=0.0,
                attribution_reason=(
                    "anticipation reward was not backed by "
                    "a verified local source"
                ),
                unknown_reason=rejected_reason,
                metadata=metadata,
            )[0]

        if reason_text == "weak acceptance after settling":
            return self.journal.resolve(
                row["resolution_key"],
                status="unknown",
                outcome="no_override_observed",
                proposed_reward=proposed,
                confidence=0.20,
                attribution_reason=(
                    "lack of override is insufficient evidence "
                    "for trusted reward"
                ),
                source_reliability=0.20,
                unknown_reason=(
                    "no explicit real-world outcome source "
                    "confirmed usefulness"
                ),
                reward_sources=["absence_of_override_only"],
                metadata=metadata,
            )[0]

        return self.journal.resolve(
            row["resolution_key"],
            status="unknown",
            outcome="unclassified_runtime_outcome",
            proposed_reward=proposed,
            confidence=0.0,
            attribution_reason=(
                "runtime outcome is not yet in the trusted "
                "Automatic Correct contract"
            ),
            unknown_reason=(
                reason_text or "unknown runtime outcome"
            ),
            metadata=metadata,
        )[0]

    def resolve_experiment(
        self, agent_id, trial, reward, reason, outcome=None
    ):
        trial = dict(trial or {})
        if not trial:
            return None
        key = _resolution_key(
            agent_id=agent_id,
            decision_id=trial.get("decision_id"),
            trial_id=trial.get("trial_id"),
            action_ts=(
                trial.get("action_at") or trial.get("started")
            ),
            action_value=trial.get("value"),
        )
        row = self.journal.get(key)
        if row is None:
            return None
        proposed = _finite(reward)
        reason_text = str(reason or "")
        metadata = {
            "experiment_reason": reason_text,
            "experiment_outcome": dict(outcome or {}),
            "experiment_kind": trial.get("kind"),
            "experiment_focus": trial.get("focus"),
        }

        if reason_text == "manual correction":
            source = self._latest_target_user_event(row)
            if source and self._trusted(
                reward=proposed,
                confidence=1.0,
                source_reliability=source["reliability"],
            ):
                return self.journal.resolve(
                    key,
                    status="trusted",
                    outcome="explicit_user_reversal",
                    proposed_reward=proposed,
                    trusted_reward=proposed,
                    confidence=1.0,
                    attribution_reason=(
                        "explicit user reversed the exact "
                        "experiment target"
                    ),
                    source_entity_id=source["entity_id"],
                    source_event_id=source["event_id"],
                    source_origin=source["origin"],
                    source_reliability=1.0,
                    reward_sources=["explicit_user_reversal"],
                    metadata=metadata,
                )[0]

        if reason_text == "presence confirmed after decision":
            allowed = set(
                (trial.get("outcome_sources") or {}).keys()
            )
            source, rejected_reason = self._presence_event(
                row, allowed_sources=allowed
            )
            if source and self._trusted(
                reward=proposed,
                confidence=source["reliability"],
                source_reliability=source["reliability"],
            ):
                return self.journal.resolve(
                    key,
                    status="trusted",
                    outcome="verified_same_area_presence_outcome",
                    proposed_reward=proposed,
                    trusted_reward=proposed,
                    confidence=source["reliability"],
                    attribution_reason=(
                        "experiment outcome source matched "
                        "same area and trial window"
                    ),
                    source_entity_id=source["entity_id"],
                    source_event_id=source["event_id"],
                    source_origin=source["origin"],
                    source_reliability=source["reliability"],
                    outcome_sources=dict(
                        trial.get("outcome_sources") or {}
                    ),
                    reward_sources=[source["entity_id"]],
                    metadata=metadata,
                )[0]
            return self.journal.resolve(
                key,
                status="rejected",
                outcome="experiment_presence_unattributed",
                proposed_reward=proposed,
                confidence=0.0,
                attribution_reason=(
                    "reported experiment reward could not be "
                    "tied to its verified outcome_sources"
                ),
                unknown_reason=(
                    rejected_reason
                    or "verified source event missing"
                ),
                outcome_sources=dict(
                    trial.get("outcome_sources") or {}
                ),
                metadata=metadata,
            )[0]

        return self.journal.resolve(
            key,
            status="unknown",
            outcome="experiment_outcome_unknown",
            proposed_reward=proposed,
            confidence=0.0,
            attribution_reason=(
                "experiment outcome lacks a trusted "
                "Stage-6 attribution"
            ),
            unknown_reason=(
                reason_text or "unlabelled experiment outcome"
            ),
            outcome_sources=dict(
                trial.get("outcome_sources") or {}
            ),
            metadata=metadata,
        )[0]

    def summary(self, agent_id):
        return self.journal.summary(agent_id)

    def recent(self, agent_id, limit=50):
        return self.journal.recent(agent_id, limit=limit)


def install(core):
    engine = core.ENGINE
    if (
        engine is None
        or core.STORE is None
    ):
        return None
    existing = getattr(
        engine, "automatic_correct_rewards", None
    )
    if existing is not None:
        return existing

    service = TrustedAutomaticRewardService(core)
    engine.automatic_correct_rewards = service

    original_submit = engine.executor.submit

    def submit(intent, features=None, action_index=None):
        result = original_submit(
            intent, features, action_index
        )
        try:
            service.start_action(intent, result)
        except Exception as exc:
            core.STORE.event(
                intent.agent_id,
                "warning",
                "automatic_correct_capture_failed",
                "Automatic Correct could not capture "
                "the action observation",
                {
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "learning_enabled": False,
                },
            )
        return result

    engine.executor.submit = submit

    original_reward_pending = engine._reward_pending

    def reward_pending(
        agent, rt, reward, reason,
        user_id=None, experience=None,
    ):
        pending = (
            experience
            if experience is not None
            else rt.get("pending")
        )
        if not pending:
            return None
        # Experiment/teaching pending items already have separate established contracts.
        # Their base handler does not inject the counterfactual action as ordinary policy
        # feedback, so preserve that lifecycle. Automatic Correct owns ordinary delayed
        # outcome rewards and deliberately stops scalar reward learning at Stage 6.
        if pending.get("experiment") or pending.get("teaching_id"):
            return original_reward_pending(
                agent, rt, reward, reason,
                user_id, experience,
            )
        resolved = None
        try:
            resolved = service.resolve_runtime(
                agent, rt, reward, reason,
                user_id, experience,
            )
        except Exception as exc:
            core.STORE.event(
                agent["id"],
                "warning",
                "automatic_correct_resolution_failed",
                "Automatic Correct outcome remained untrusted "
                "after an attribution error",
                {
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "reason": str(reason),
                },
            )

        # Preserve runtime observability and close the existing pending-action lifecycle,
        # but intentionally do NOT call the legacy live-feedback handler: it performs
        # policy.update + save_model + add_feedback. Stage 6 must only collect evidence.
        rt["last_reward_components"] = rt.pop(
            "reward_components_pending", {}
        )
        rt["last_reward"] = reward
        rt["last_reward_reason"] = (
            "Automatic Correct proposal: " + str(reason)
        )
        if rt.get("pending") is pending:
            rt["pending"] = None

        decision_id = pending.get("decision_id")
        provenance = getattr(engine, "provenance", None)
        if decision_id and provenance is not None:
            provenance.mark_outcome(
                decision_id, reward, str(reason)
            )
        core.STORE.event(
            agent["id"],
            (
                "info"
                if resolved
                and resolved.get("status") == "trusted"
                else "warning"
            ),
            "automatic_correct_outcome",
            (
                "Automatic Correct trusted outcome"
                if resolved
                and resolved.get("status") == "trusted"
                else "Automatic Correct outcome kept out of learning"
            ),
            {
                "proposed_reward": reward,
                "reason": str(reason),
                "status": (
                    (resolved or {}).get("status")
                    or "unknown"
                ),
                "resolution_key": (
                    (resolved or {}).get("resolution_key")
                ),
                "policy_updated": False,
            },
        )
        return bool(resolved)

    engine._reward_pending = reward_pending

    original_finish = engine.experiments._finish

    def finish(aid, reward, reason):
        data = engine.experiments._get(aid)
        trial = dict(
            data.get("active") or {}
        )
        result = original_finish(
            aid, reward, reason
        )
        try:
            outcome = dict(
                (
                    engine.experiments
                    ._get(aid)
                    .get("last_outcome")
                    or {}
                )
            )
            service.resolve_experiment(
                aid, trial, reward, reason, outcome
            )
        except Exception as exc:
            core.STORE.event(
                aid,
                "warning",
                "automatic_correct_experiment_resolution_failed",
                "Automatic Correct could not attribute "
                "the experiment outcome",
                {
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "reason": str(reason),
                },
            )
        return result

    engine.experiments._finish = finish

    original_runtime_for = engine.runtime_for

    def runtime_for(agent):
        payload = original_runtime_for(agent)
        payload["automatic_correct"] = (
            service.summary(agent["id"])
        )
        return payload

    engine.runtime_for = runtime_for
    engine.automatic_correct_contract = {
        "version": CONTRACT_VERSION,
        "mode": "trusted_outcome_reward_buffer_only",
        "policy_updates": False,
        "online_rl": False,
        "manual_correct_reinterpreted": False,
        "trusted_sources": [
            "explicit_user_reversal_exact_target",
            "verified_same_area_presence_transition",
        ],
        "silence_semantics": "unknown_not_positive_reward",
        "deduplication": (
            "one resolution_key per decision or trial"
        ),
        "physical_authority": False,
    }
    return service


def register_routes(registry, core):
    service = getattr(
        getattr(core, "ENGINE", None),
        "automatic_correct_rewards",
        None,
    )
    if service is None:
        return registry

    def recent(http, params):
        agent_id = params["agent_id"]
        if not core.STORE.get_agent_config(agent_id):
            return http.send_json(
                404, {"error": "agent not found"}
            )
        return http.send_json(
            200,
            {
                "automatic_correct": (
                    service.summary(agent_id)
                ),
                "experiences": service.recent(
                    agent_id, limit=50
                ),
            },
        )

    registry.register(
        "GET",
        "automatic_correct.recent",
        (
            r"^/api/agents/"
            r"(?P<agent_id>[^/]+)/automatic-correct$"
        ),
        recent,
        require_trusted=True,
        require_runtime=True,
        priority=120,
    )
    return registry
