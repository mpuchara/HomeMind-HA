"""Persistent champion/challenger context state for Adaptive AI agents.

This module introduces the Sensor Tournament *state layer* without changing control
behaviour.  The policy schema remains the champion (``active_features``); promising
non-active sensors are recorded as ``challenger_features`` for later shadow evaluation.
No challenger can dispatch an action from this module.

Later releases can attach out-of-sample challenger metrics and promotion/rollback logic
to this state without changing the policy or Executor contracts.
"""
import json
import math
import threading
import time

from context import (
    controllable_context_exclusions,
    electrical_context_exclusions,
    is_context_candidate_entity,
)
from settings import OPTIONS


class ContextTournament:
    """Persist one bounded Sensor Tournament snapshot per agent."""

    def __init__(self, store, engine):
        self.store = store
        self.engine = engine
        self.lock = threading.RLock()
        self._cache = {}
        self._runtime_fingerprints = {}
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_tournament_state (
                    agent_id TEXT PRIMARY KEY,
                    active_features_json TEXT NOT NULL DEFAULT '[]',
                    challenger_features_json TEXT NOT NULL DEFAULT '[]',
                    feature_scores_json TEXT NOT NULL DEFAULT '{}',
                    last_evaluation REAL,
                    schema_revision INTEGER NOT NULL DEFAULT 0,
                    previous_schema_json TEXT NOT NULL DEFAULT '[]',
                    updated_ts REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _json_list(raw):
        try:
            value = json.loads(raw or "[]")
            return [str(x) for x in value] if isinstance(value, list) else []
        except Exception:
            return []

    @staticmethod
    def _json_scores(raw):
        try:
            value = json.loads(raw or "{}")
        except Exception:
            return {}
        if not isinstance(value, dict):
            return {}
        out = {}
        for key, val in value.items():
            try:
                score = float(val)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score):
                out[str(key)] = max(0.0, min(1.0, score))
        return out

    def _row_state(self, row):
        if not row:
            return None
        return {
            "agent_id": str(row["agent_id"]),
            "active_features": self._json_list(row["active_features_json"]),
            "challenger_features": self._json_list(row["challenger_features_json"]),
            "feature_scores": self._json_scores(row["feature_scores_json"]),
            "last_evaluation": (float(row["last_evaluation"]) if row["last_evaluation"] is not None else None),
            "schema_revision": int(row["schema_revision"] or 0),
            "previous_schema": self._json_list(row["previous_schema_json"]),
        }

    def state(self, agent_id):
        aid = str(agent_id)
        with self.lock:
            cached = self._cache.get(aid)
            if cached is not None:
                return dict(cached)
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM context_tournament_state WHERE agent_id=?", (aid,)
            ).fetchone()
        state = self._row_state(row)
        if state is None:
            state = {
                "agent_id": aid,
                "active_features": [],
                "challenger_features": [],
                "feature_scores": {},
                "last_evaluation": None,
                "schema_revision": 0,
                "previous_schema": [],
            }
        with self.lock:
            self._cache[aid] = dict(state)
        return dict(state)

    def _active_from_policy_or_store(self, agent, policy=None):
        if policy is not None:
            schema = getattr(policy, "schema", None)
            entities = list(getattr(schema, "entities", []) or [])
            if entities:
                return [str(x) for x in entities]
        cached = getattr(self.engine, "models", {}).get(agent["id"])
        if cached is not None:
            entities = list(getattr(getattr(cached, "schema", None), "entities", []) or [])
            if entities:
                return [str(x) for x in entities]
        raw = self.store.get_model(agent["id"]) or {}
        return [str(x) for x in ((raw.get("schema") or {}).get("entities") or [])]

    def _current_scores(self, agent_id, explicit=None):
        source = explicit
        if source is None:
            source = getattr(self.engine, "context_relevance", {}).get(agent_id) or {}
        out = {}
        for key, val in (source or {}).items():
            try:
                score = float(val)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score):
                out[str(key)] = max(0.0, min(1.0, score))
        return out

    def _eligible_entities(self, agent, active):
        with self.engine.lock:
            states = dict(getattr(self.engine, "state_map", {}) or {})
            registry = dict(getattr(self.engine, "entity_registry", {}) or {})
        if not states:
            return None
        excluded_control, _ = controllable_context_exclusions(states, registry)
        excluded_electrical, _ = electrical_context_exclusions(states, registry)
        excluded = set(excluded_control) | set(excluded_electrical) | {agent["target_entity"]}
        # Active features remain the source of truth even if a later registry refresh would
        # now exclude one of them. The tournament only screens *new* challengers here.
        return {
            eid for eid, state in states.items()
            if eid not in excluded
            and eid not in set(active)
            and is_context_candidate_entity(eid, state, excluded)
        }

    def sync_agent(self, agent, *, policy=None, active_features=None, feature_scores=None,
                   evaluated_at=None):
        """Refresh persisted tournament state without changing the policy schema.

        ``active_features`` always mirrors the policy champion. Candidate ranking only
        chooses a bounded challenger list; it never adds those sensors to the policy.
        """
        aid = str(agent["id"])
        previous = self.state(aid)
        active = ([str(x) for x in active_features] if active_features is not None
                  else self._active_from_policy_or_store(agent, policy))

        # Preserve the last useful score map across restart/startup phases where the engine
        # has not reconstructed historical relevance yet.
        scores = self._current_scores(aid, feature_scores)
        if not scores:
            scores = dict(previous.get("feature_scores") or {})

        eligible = self._eligible_entities(agent, active)
        challenger_count = max(0, int(OPTIONS.get("context_challenger_count", 4)))
        if eligible is None:
            challengers = list(previous.get("challenger_features") or [])[:challenger_count]
            filtered_scores = dict(scores)
            last_evaluation = previous.get("last_evaluation")
        else:
            filtered_scores = {
                eid: score for eid, score in scores.items()
                if eid in eligible or eid in set(active)
            }
            ranked = [
                (float(filtered_scores.get(eid, 0.0)), eid)
                for eid in eligible
                if float(filtered_scores.get(eid, 0.0)) > 0.0
            ]
            ranked.sort(key=lambda item: (-item[0], item[1]))
            challengers = [eid for _, eid in ranked[:challenger_count]]
            # A candidate pass is an evaluation only when scored evidence exists. Merely
            # loading states on startup should not make diagnostics claim a real evaluation.
            last_evaluation = (
                float(evaluated_at if evaluated_at is not None else time.time())
                if filtered_scores else previous.get("last_evaluation")
            )

        old_active = list(previous.get("active_features") or [])
        old_revision = int(previous.get("schema_revision") or 0)
        if active != old_active:
            previous_schema = old_active
            schema_revision = old_revision + 1 if old_revision else (1 if active else 0)
        else:
            previous_schema = list(previous.get("previous_schema") or [])
            schema_revision = old_revision or (1 if active else 0)

        state = {
            "agent_id": aid,
            "active_features": active,
            "challenger_features": challengers,
            "feature_scores": filtered_scores,
            "last_evaluation": last_evaluation,
            "schema_revision": schema_revision,
            "previous_schema": previous_schema,
        }
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO context_tournament_state
                   (agent_id,active_features_json,challenger_features_json,feature_scores_json,
                    last_evaluation,schema_revision,previous_schema_json,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                     active_features_json=excluded.active_features_json,
                     challenger_features_json=excluded.challenger_features_json,
                     feature_scores_json=excluded.feature_scores_json,
                     last_evaluation=excluded.last_evaluation,
                     schema_revision=excluded.schema_revision,
                     previous_schema_json=excluded.previous_schema_json,
                     updated_ts=excluded.updated_ts""",
                (
                    aid,
                    json.dumps(active, separators=(",", ":")),
                    json.dumps(challengers, separators=(",", ":")),
                    json.dumps(filtered_scores, separators=(",", ":"), sort_keys=True),
                    last_evaluation,
                    schema_revision,
                    json.dumps(previous_schema, separators=(",", ":")),
                    now,
                ),
            )
        with self.lock:
            self._cache[aid] = dict(state)
        return dict(state)

    def sync_if_needed(self, agent, policy):
        """Cheap runtime hook: write only when policy or relevance object changes."""
        aid = str(agent["id"])
        score_map = getattr(self.engine, "context_relevance", {}).get(aid)
        fingerprint = (id(policy), id(score_map))
        with self.lock:
            if self._runtime_fingerprints.get(aid) == fingerprint and aid in self._cache:
                return dict(self._cache[aid])
            self._runtime_fingerprints[aid] = fingerprint
        return self.sync_agent(agent, policy=policy, feature_scores=score_map)

    def state_for_agent(self, agent):
        current = self.state(agent["id"])
        if current["schema_revision"] == 0 and not current["active_features"]:
            return self.sync_agent(agent)
        return current


def install(store, engine):
    """Attach the tournament as a non-controlling runtime extension."""
    existing = getattr(engine, "context_tournament", None)
    if existing is not None:
        return existing

    service = ContextTournament(store, engine)
    original_policy = engine.policy
    original_runtime_for = engine.runtime_for

    def policy_with_tournament(agent):
        policy = original_policy(agent)
        service.sync_if_needed(agent, policy)
        return policy

    def runtime_with_tournament(agent):
        payload = original_runtime_for(agent)
        payload["context_tournament"] = service.state_for_agent(agent)
        return payload

    engine.policy = policy_with_tournament
    engine.runtime_for = runtime_with_tournament
    engine.context_tournament = service
    return service
