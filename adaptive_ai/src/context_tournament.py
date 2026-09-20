"""Persistent champion/challenger context state for Adaptive AI agents.

The policy schema remains the champion (``active_features``). Promising non-active
sensors are tracked as ``challenger_features`` and evaluated in parallel shadow mode.
The shadow evaluator never creates an ActionIntent, never calls the Executor, never
changes the live policy schema and never sends a Home Assistant service call.

Step 6 deliberately avoids rebuilding MultiHorizonPolicy for every challenger/event.
Each challenger owns a tiny persisted prequential residual table keyed by the active
prediction and a bounded sensor-value bucket. It learns only from later observed target
transitions. This makes shadow comparison cheap enough to run alongside normal inference
while leaving the production control path untouched.
"""
import json
import math
import threading
import time

from context import (
    action_values,
    context_scalar,
    controllable_context_exclusions,
    electrical_context_exclusions,
    is_context_candidate_entity,
    target_value,
)
from settings import OPTIONS


SHADOW_MODEL_VERSION = 1
SHADOW_BUCKET_COUNT = 9


class ContextTournament:
    """Persist bounded Sensor Tournament state plus non-controlling shadow evaluators."""

    def __init__(self, store, engine):
        self.store = store
        self.engine = engine
        self.lock = threading.RLock()
        self._cache = {}
        self._runtime_fingerprints = {}
        self._shadow_models = {}
        self._shadow_runtime = {}
        self._shadow_dirty = {}
        self._shadow_flush_event = threading.Event()
        self._shadow_persistence_stats = {"queued": 0, "flushed": 0, "flushes": 0, "errors": 0}
        self._last_error_ts = 0.0
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
                CREATE TABLE IF NOT EXISTS context_tournament_shadow (
                    agent_id TEXT NOT NULL,
                    challenger_entity TEXT NOT NULL,
                    model_json TEXT NOT NULL DEFAULT '{}',
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(agent_id, challenger_entity)
                );
                """
            )
        threading.Thread(
            target=self._shadow_writer,
            name="adaptive-ai-context-shadow-writer",
            daemon=True,
        ).start()

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
        return {
            eid for eid, state in states.items()
            if eid not in excluded
            and eid not in set(active)
            and is_context_candidate_entity(eid, state, excluded)
        }

    def sync_agent(self, agent, *, policy=None, active_features=None, feature_scores=None,
                   evaluated_at=None):
        """Refresh tournament membership without changing the live policy schema."""
        aid = str(agent["id"])
        previous = self.state(aid)
        active = ([str(x) for x in active_features] if active_features is not None
                  else self._active_from_policy_or_store(agent, policy))

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
        """Cheap hook: tournament sync never constructs or rebuilds another policy."""
        aid = str(agent["id"])
        score_map = getattr(self.engine, "context_relevance", {}).get(aid) or {}
        # Content fingerprint catches in-place score updates while avoiding a DB write on
        # every policy() call. The active schema identity is represented by the policy id.
        score_fingerprint = tuple(sorted((str(k), round(float(v), 8)) for k, v in score_map.items()))
        fingerprint = (id(policy), score_fingerprint)
        with self.lock:
            if self._runtime_fingerprints.get(aid) == fingerprint and aid in self._cache:
                return dict(self._cache[aid])
            self._runtime_fingerprints[aid] = fingerprint
        return self.sync_agent(agent, policy=policy, feature_scores=score_map)

    @staticmethod
    def _shadow_bucket(value):
        """Quantize the already bounded context scalar into a tiny stable lookup key."""
        value = max(-1.0, min(1.0, float(value)))
        return int(round((value + 1.0) * 0.5 * (SHADOW_BUCKET_COUNT - 1)))

    @staticmethod
    def _blank_shadow_model(action_count):
        return {
            "version": SHADOW_MODEL_VERSION,
            "action_count": int(action_count),
            "counts": {},
            "samples": 0,
            "active_correct": 0,
            "shadow_correct": 0,
            "last_scored_ts": None,
        }

    def _load_shadow_model(self, agent_id, challenger, action_count):
        key = (str(agent_id), str(challenger))
        with self.lock:
            cached = self._shadow_models.get(key)
            if cached is not None and int(cached.get("action_count") or 0) == int(action_count):
                return cached
        with self.store.conn() as c:
            row = c.execute(
                "SELECT model_json FROM context_tournament_shadow WHERE agent_id=? AND challenger_entity=?",
                key,
            ).fetchone()
        model = None
        if row:
            try:
                candidate = json.loads(row["model_json"] or "{}")
                if (int(candidate.get("version") or 0) == SHADOW_MODEL_VERSION
                        and int(candidate.get("action_count") or 0) == int(action_count)):
                    model = candidate
            except Exception:
                model = None
        if model is None:
            model = self._blank_shadow_model(action_count)
        with self.lock:
            self._shadow_models[key] = model
        return model

    def _flush_shadow_models(self):
        with self.lock:
            if not self._shadow_dirty:
                return 0
            batch = dict(self._shadow_dirty)
            self._shadow_dirty.clear()
        now = time.time()
        packed = [
            (key[0], key[1], raw, now)
            for key, raw in batch.items()
        ]
        try:
            with self.store.lock, self.store.conn() as c:
                c.executemany(
                    """INSERT INTO context_tournament_shadow(agent_id,challenger_entity,model_json,updated_ts)
                       VALUES(?,?,?,?)
                       ON CONFLICT(agent_id,challenger_entity) DO UPDATE SET
                         model_json=excluded.model_json,updated_ts=excluded.updated_ts""",
                    packed,
                )
            with self.lock:
                self._shadow_persistence_stats["flushed"] += len(packed)
                self._shadow_persistence_stats["flushes"] += 1
            return len(packed)
        except Exception:
            with self.lock:
                for key, raw in batch.items():
                    self._shadow_dirty.setdefault(key, raw)
                self._shadow_persistence_stats["errors"] += 1
            raise

    def _shadow_writer(self):
        while not self.engine.stop_event.is_set():
            self._shadow_flush_event.wait(5.0)
            self._shadow_flush_event.clear()
            try:
                self._flush_shadow_models()
            except Exception as exc:
                self.report_runtime_error(exc)
                time.sleep(0.1)
        try:
            self._flush_shadow_models()
        except Exception:
            pass

    def shadow_persistence_snapshot(self):
        with self.lock:
            return {
                **self._shadow_persistence_stats,
                "pending": len(self._shadow_dirty),
            }

    def _save_shadow_model(self, agent_id, challenger, model):
        key = (str(agent_id), str(challenger))
        raw = json.dumps(model, separators=(",", ":"), sort_keys=True)
        with self.lock:
            self._shadow_models[key] = model
            self._shadow_dirty[key] = raw
            self._shadow_persistence_stats["queued"] += 1
            wake = len(self._shadow_dirty) >= 32
        if wake:
            self._shadow_flush_event.set()

    @staticmethod
    def _shadow_context_key(active_idx, bucket):
        return f"{int(active_idx)}:{int(bucket)}"

    def _shadow_predict_index(self, model, active_idx, bucket):
        action_count = max(1, int(model.get("action_count") or 1))
        counts = list((model.get("counts") or {}).get(
            self._shadow_context_key(active_idx, bucket), []
        ))
        if len(counts) != action_count or not counts or max(counts) <= 0:
            return int(active_idx)
        best = max(counts)
        tied = [idx for idx, count in enumerate(counts) if count == best]
        # Conservative tie break: stay with champion instead of inventing a challenger win.
        return int(active_idx) if int(active_idx) in tied else int(min(tied))

    def _score_shadow_sample(self, agent_id, challenger, pending, actual_idx, action_count, now):
        model = self._load_shadow_model(agent_id, challenger, action_count)
        active_idx = int(pending["active_index"])
        shadow_idx = int(pending["shadow_index"])
        bucket = int(pending["bucket"])

        # Prequential order is important: score the prediction made before this outcome,
        # then learn from the observed target transition.
        model["samples"] = int(model.get("samples") or 0) + 1
        model["active_correct"] = int(model.get("active_correct") or 0) + int(active_idx == actual_idx)
        model["shadow_correct"] = int(model.get("shadow_correct") or 0) + int(shadow_idx == actual_idx)
        model["last_scored_ts"] = float(now)

        key = self._shadow_context_key(active_idx, bucket)
        counts = list((model.get("counts") or {}).get(key, []))
        if len(counts) != action_count:
            counts = [0] * action_count
        counts[int(actual_idx)] += 1
        model.setdefault("counts", {})[key] = counts
        self._save_shadow_model(agent_id, challenger, model)

    @staticmethod
    def _shadow_ttl(agent):
        """Bound how long a shadow prediction can remain a valid pre-outcome sample."""
        interval = max(0.0, float(agent.get("action_interval") or 0.0))
        prop = str(agent.get("target_property") or "")
        if prop in ("power", "option_index"):
            return max(30.0, min(300.0, 30.0 + 10.0 * interval))
        return max(300.0, min(3600.0, 300.0 + 10.0 * interval))

    def observe_shadow(self, agent, state_map=None, changed_entities=None):
        """Run challenger inference and prequential scoring without any control surface.

        This method never calls engine.policy(), Executor, HA service methods, ActionIntent,
        or schema-selection code. It consumes the champion prediction already produced by
        normal inference and evaluates challengers in a separate residual lookup model.
        """
        aid = str(agent["id"])
        tournament = self.state(aid)
        challengers = list(tournament.get("challenger_features") or [])
        if not challengers:
            return {"predictions": [], "scored": 0}

        states = dict(state_map or getattr(self.engine, "state_map", {}) or {})
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        if current is None or not math.isfinite(float(current)):
            return {"predictions": [], "scored": 0}

        actions = [float(x) for x in action_values(agent)]
        if not actions:
            return {"predictions": [], "scored": 0}
        action_count = len(actions)
        rt = (getattr(self.engine, "runtime", {}) or {}).get(aid) or {}
        active_value = rt.get("last_prediction")
        if active_value is None:
            active_value = float(current)
        active_idx = min(range(action_count), key=lambda i: abs(actions[i] - float(active_value)))
        actual_idx = min(range(action_count), key=lambda i: abs(actions[i] - float(current)))
        now = time.time()

        with self.lock:
            shadow_rt = self._shadow_runtime.setdefault(aid, {
                "last_target": None,
                "pending": {},
                "predictions": {},
                "last_scored_ts": None,
                "scored_samples": 0,
            })
            last_target = shadow_rt.get("last_target")
            pending = dict(shadow_rt.get("pending") or {})

        deadband = max(0.01, float(agent.get("deadband") or 0.01) * 0.05)
        changed = last_target is not None and abs(float(current) - float(last_target)) > deadband
        scored = 0
        if changed:
            origin = str(rt.get("last_change_origin") or "")
            # Own-command acknowledgements are not independent evidence. Manual and
            # external/automation transitions are legitimate shadow labels.
            if origin != "own_command":
                ttl = self._shadow_ttl(agent)
                active_challengers = set(challengers)
                for challenger, sample in pending.items():
                    if challenger not in active_challengers:
                        continue
                    age = now - float(sample.get("ts") or 0.0)
                    if age < 0 or age > ttl:
                        continue
                    self._score_shadow_sample(
                        aid, challenger, sample, actual_idx, action_count, now
                    )
                    scored += 1
            # Residual learning is durable model state, not disposable telemetry. Preserve
            # the restart contract while collapsing N challenger writes into one SQLite
            # transaction per observed target transition.
            if scored:
                self._flush_shadow_models()

        predictions = {}
        for challenger in challengers:
            scalar = context_scalar(challenger, states.get(challenger), agent)
            if scalar is None:
                continue
            try:
                scalar = float(scalar)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(scalar):
                continue
            bucket = self._shadow_bucket(scalar)
            model = self._load_shadow_model(aid, challenger, action_count)
            shadow_idx = self._shadow_predict_index(model, active_idx, bucket)
            predictions[challenger] = {
                "ts": now,
                "sensor_value": scalar,
                "bucket": bucket,
                "active_index": int(active_idx),
                "active_value": actions[active_idx],
                "shadow_index": int(shadow_idx),
                "shadow_value": actions[shadow_idx],
                "sample_count": int(model.get("samples") or 0),
            }

        with self.lock:
            shadow_rt["last_target"] = float(current)
            shadow_rt["pending"] = dict(predictions)
            shadow_rt["predictions"] = dict(predictions)
            if scored:
                shadow_rt["last_scored_ts"] = now
                shadow_rt["scored_samples"] = int(shadow_rt.get("scored_samples") or 0) + scored

        return {"predictions": list(predictions.values()), "scored": scored}

    def shadow_status(self, agent):
        aid = str(agent["id"])
        tournament = self.state(aid)
        actions = [float(x) for x in action_values(agent)]
        action_count = len(actions)
        with self.lock:
            runtime = dict(self._shadow_runtime.get(aid) or {})
            predictions = dict(runtime.get("predictions") or {})
        rows = []
        for challenger in tournament.get("challenger_features") or []:
            model = self._load_shadow_model(aid, challenger, action_count) if action_count else self._blank_shadow_model(0)
            samples = int(model.get("samples") or 0)
            active_correct = int(model.get("active_correct") or 0)
            shadow_correct = int(model.get("shadow_correct") or 0)
            pred = predictions.get(challenger) or {}
            rows.append({
                "entity_id": challenger,
                "feature_score": float((tournament.get("feature_scores") or {}).get(challenger, 0.0)),
                "prediction": pred.get("shadow_value"),
                "prediction_index": pred.get("shadow_index"),
                "active_prediction": pred.get("active_value"),
                "sensor_value": pred.get("sensor_value"),
                "samples": samples,
                "active_accuracy": (active_correct / samples) if samples else None,
                "shadow_accuracy": (shadow_correct / samples) if samples else None,
                "accuracy_gain": ((shadow_correct - active_correct) / samples) if samples else None,
                "last_scored_ts": model.get("last_scored_ts"),
            })
        return {
            "mode": "shadow_only",
            "controls_device": False,
            "rebuilds_policy": False,
            "challengers": rows,
            "last_scored_ts": runtime.get("last_scored_ts"),
            "scored_samples": int(runtime.get("scored_samples") or 0),
        }

    def state_for_agent(self, agent):
        current = self.state(agent["id"])
        if current["schema_revision"] == 0 and not current["active_features"]:
            current = self.sync_agent(agent)
        else:
            current = dict(current)
        current["shadow_evaluation"] = self.shadow_status(agent)
        return current

    def report_runtime_error(self, exc):
        now = time.time()
        if now - self._last_error_ts < 60.0:
            return
        self._last_error_ts = now
        try:
            self.store.event(None, "warning", "context_tournament_shadow_error",
                             f"Sensor Tournament shadow evaluation skipped: {type(exc).__name__}: {exc}", None)
        except Exception:
            pass


def install(store, engine):
    """Attach shadow tournament hooks without inserting anything into the control path."""
    existing = getattr(engine, "context_tournament", None)
    if existing is not None:
        return existing

    service = ContextTournament(store, engine)
    original_policy = engine.policy
    original_runtime_for = engine.runtime_for
    original_process_agent = getattr(engine, "process_agent", None)

    def policy_with_tournament(agent):
        policy = original_policy(agent)
        service.sync_if_needed(agent, policy)
        return policy

    def runtime_with_tournament(agent):
        payload = original_runtime_for(agent)
        payload["context_tournament"] = service.state_for_agent(agent)
        return payload

    def process_agent_with_tournament(agent, state_map, changed_entities=None):
        # Production inference/control runs first and remains entirely authoritative.
        # Shadow evaluation only reads its resulting champion prediction afterwards.
        result = original_process_agent(agent, state_map, changed_entities)
        try:
            service.observe_shadow(agent, state_map, changed_entities)
        except Exception as exc:
            service.report_runtime_error(exc)
        return result

    engine.policy = policy_with_tournament
    engine.runtime_for = runtime_with_tournament
    if callable(original_process_agent):
        engine.process_agent = process_agent_with_tournament
    engine.context_tournament = service
    return service
