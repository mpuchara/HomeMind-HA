"""Stage-3 tiny MLP Shadow observer.

The service is intentionally outside the authoritative Ridge -> ActionIntent -> Executor
decision chain.  It observes only completed Shadow inferences, persists its own untrained
model and publishes diagnostics in engine.runtime.  It never consumes rewards, Correct
labels or Candidate training jobs and it never dispatches Home Assistant services.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from observation_space import observation_as_of, select_observation_mask
from policy_tiny_mlp import TinyMLPBackend
from settings import OPTIONS


def _parse_hidden(value):
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "32,16").split(",")
    hidden = tuple(int(str(x).strip()) for x in parts if str(x).strip())
    return hidden or TinyMLPBackend.DEFAULT_HIDDEN


def ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tiny_mlp_shadow_models (
                agent_id TEXT PRIMARY KEY,
                backend TEXT NOT NULL,
                backend_version INTEGER NOT NULL,
                feature_schema_id TEXT NOT NULL,
                feature_mask_id TEXT NOT NULL,
                model_json TEXT NOT NULL,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            """
        )


class TinyMLPShadowService:
    CONTRACT_VERSION = 1

    def __init__(self, store, engine, *, enabled=None):
        self.store = store
        self.engine = engine
        self.enabled = bool(
            OPTIONS.get("tiny_mlp_shadow_enabled", True)
            if enabled is None else enabled
        )
        self.hidden = _parse_hidden(OPTIONS.get("tiny_mlp_hidden_layers", "32,16"))
        self.base_seed = int(OPTIONS.get("tiny_mlp_init_seed", 1482))
        self.lock = threading.RLock()
        self.cache = {}
        ensure_tables(store)

    @staticmethod
    def _signature(mask, policy):
        return (
            str(mask.schema_id),
            str(mask.mask_id),
            tuple(mask.feature_ids),
            tuple(float(x) for x in policy.actions),
            tuple(int(x) for x in policy.horizons),
        )

    def _seed_for(self, agent_id, mask, policy):
        payload = json.dumps(
            {
                "base_seed": self.base_seed,
                "agent_id": str(agent_id),
                "schema_id": mask.schema_id,
                "mask_id": mask.mask_id,
                "actions": [float(x) for x in policy.actions],
                "horizons": [int(x) for x in policy.horizons],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return int.from_bytes(
            hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big"
        ) & 0x7FFFFFFFFFFFFFFF

    def _load_raw(self, agent_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT model_json FROM tiny_mlp_shadow_models WHERE agent_id=?",
                (str(agent_id),),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def _persist(self, agent_id, backend):
        raw = backend.serialize()
        now = time.time()
        encoded = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """
                INSERT INTO tiny_mlp_shadow_models
                    (agent_id,backend,backend_version,feature_schema_id,
                     feature_mask_id,model_json,created_ts,updated_ts)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    backend=excluded.backend,
                    backend_version=excluded.backend_version,
                    feature_schema_id=excluded.feature_schema_id,
                    feature_mask_id=excluded.feature_mask_id,
                    model_json=excluded.model_json,
                    updated_ts=excluded.updated_ts
                """,
                (
                    str(agent_id),
                    TinyMLPBackend.BACKEND,
                    TinyMLPBackend.VERSION,
                    backend.schema_id,
                    backend.mask_id,
                    encoded,
                    now,
                    now,
                ),
            )
        return raw

    def _new_backend(self, agent, policy, mask):
        return TinyMLPBackend(
            actions=policy.actions,
            horizons=policy.horizons,
            feature_ids=mask.feature_ids,
            schema_id=mask.schema_id,
            mask_id=mask.mask_id,
            hidden=self.hidden,
            init_seed=self._seed_for(agent["id"], mask, policy),
        )

    def _backend(self, agent, policy, mask, *, source_policy_revision=None):
        aid = str(agent["id"])
        signature = self._signature(mask, policy)
        source_policy_revision = str(source_policy_revision or "unknown")
        with self.lock:
            cached = self.cache.get(aid)
            if (
                cached
                and cached["signature"] == signature
                and cached.get("source_policy_revision") == source_policy_revision
            ):
                return cached["backend"], "memory"

            raw = self._load_raw(aid)
            if raw is not None:
                try:
                    backend = TinyMLPBackend.deserialize(
                        raw,
                        expected_schema_id=mask.schema_id,
                        expected_mask_id=mask.mask_id,
                        expected_feature_ids=mask.feature_ids,
                        expected_actions=policy.actions,
                        expected_horizons=policy.horizons,
                    )
                    if tuple(backend.hidden) != tuple(self.hidden):
                        raise ValueError(
                            "NEEDS_RETRAIN: tiny MLP configured architecture changed"
                        )
                    self.cache[aid] = {
                        "signature": signature,
                        "source_policy_revision": source_policy_revision,
                        "mask": mask,
                        "backend": backend,
                    }
                    return backend, "persisted_restart"
                except Exception as exc:
                    # The Stage-3 model is untrained and has no authority.  A structural
                    # mismatch can therefore only invalidate this isolated Shadow copy;
                    # Live/Candidate models are never touched or rebuilt.
                    self.store.event(
                        aid,
                        "warning",
                        "tiny_mlp_shadow_reinitialized",
                        "Tiny MLP Shadow copy was incompatible and was reinitialized",
                        {"reason": str(exc)[:400], "physical_authority": False},
                    )

            backend = self._new_backend(agent, policy, mask)
            self._persist(aid, backend)
            self.cache[aid] = {
                "signature": signature,
                "source_policy_revision": source_policy_revision,
                "mask": mask,
                "backend": backend,
            }
            self.store.event(
                aid,
                "info",
                "tiny_mlp_shadow_created",
                "Tiny MLP inference-only Shadow model created",
                {
                    "backend": backend.BACKEND,
                    "backend_version": backend.VERSION,
                    "schema_id": backend.schema_id,
                    "mask_id": backend.mask_id,
                    "architecture": list(backend.architecture),
                    "parameter_count": backend.parameter_count,
                    "training_enabled": False,
                    "dispatch_capability": False,
                },
            )
            return backend, "created"

    def observe(self, agent, policy, state_map, temporal, *, timestamp):
        if not self.enabled or str(agent.get("mode") or "") != "shadow":
            return None
        aid = str(agent["id"])
        source_policy_revision = str(
            getattr(policy, "tournament_revision", None)
            or getattr(policy, "model_revision", None)
            or "unknown"
        )
        actions = tuple(float(x) for x in policy.actions)
        horizons = tuple(int(x) for x in policy.horizons)
        with self.lock:
            cached = self.cache.get(aid)
            reusable = bool(
                cached
                and cached.get("source_policy_revision") == source_policy_revision
                and cached["signature"][3] == actions
                and cached["signature"][4] == horizons
            )
            if reusable:
                mask = cached["mask"]
                backend = cached["backend"]
                model_source = "memory"
            else:
                mask = None
                backend = None
                model_source = None

        if mask is None:
            # Feature selection is a model-lifecycle operation, not per-event work.  A
            # stable tournament_revision keeps the same Stage-2 mask through ordinary
            # online Ridge updates; Train/Rebuild produces a new revision and rematerializes
            # the isolated neural copy once.
            registry = self.engine.context.resolved_registry()
            hints = list(getattr(getattr(policy, "schema", None), "entities", ()) or ())
            relevance = dict(
                (getattr(policy, "selection_meta", {}) or {}).get("selection_scores") or {}
            )
            mask, _mask_diagnostics = select_observation_mask(
                agent,
                state_map,
                registry,
                hints,
                relevance_scores=relevance,
            )
            backend, model_source = self._backend(
                agent,
                policy,
                mask,
                source_policy_revision=source_policy_revision,
            )
        observation = observation_as_of(
            mask,
            state_map,
            temporal,
            float(timestamp),
            agent,
            home_provider=self.engine.context,
        )
        started = time.perf_counter_ns()
        chosen, confidence, arms, horizon, support, novelty = backend.predict(observation)
        inference_us = (time.perf_counter_ns() - started) / 1000.0
        result = {
            "contract_version": self.CONTRACT_VERSION,
            "enabled": True,
            "mode": "shadow",
            "shadow_only": True,
            "dispatch_capability": False,
            "physical_authority": False,
            "training_enabled": False,
            "historical_training": False,
            "baseline_backend": getattr(policy, "BACKEND", "unknown"),
            "backend": backend.BACKEND,
            "backend_version": backend.VERSION,
            "model_revision": backend.model_revision,
            "model_checksum": backend.serialize().get("model_checksum"),
            "model_source": model_source,
            "schema_id": mask.schema_id,
            "mask_id": mask.mask_id,
            "selected_feature_count": len(mask.feature_ids),
            "global_feature_count": int(mask.global_feature_count),
            "missing_feature_count": int(observation.get("missing_feature_count") or 0),
            "architecture": list(backend.architecture),
            "parameter_count": backend.parameter_count,
            "dtype": backend.DTYPE,
            "timestamp": float(timestamp),
            "inference_us": float(inference_us),
            "chosen_index": int(chosen["index"]),
            "chosen_value": float(chosen["value"]),
            "score": float(chosen["score"]),
            "confidence": float(confidence),
            "support": float(support),
            "novelty": float(novelty),
            "horizon": int(horizon),
            "action_scores": [
                {
                    "index": int(row["index"]),
                    "value": float(row["value"]),
                    "score": float(row["score"]),
                }
                for row in arms
            ],
            "note": "untrained neural inference; diagnostic only",
        }
        with self.engine.lock:
            runtime = self.engine.runtime.setdefault(str(agent["id"]), {})
            runtime["tiny_mlp_shadow"] = result
        return result

    def record_error(self, agent_id, exc):
        row = {
            "contract_version": self.CONTRACT_VERSION,
            "enabled": self.enabled,
            "mode": "shadow",
            "shadow_only": True,
            "dispatch_capability": False,
            "physical_authority": False,
            "training_enabled": False,
            "error": str(exc)[:500],
            "timestamp": time.time(),
        }
        with self.engine.lock:
            runtime = self.engine.runtime.setdefault(str(agent_id), {})
            runtime["tiny_mlp_shadow"] = row
        return row

    def persisted_model(self, agent_id):
        return self._load_raw(agent_id)

    def diagnostics(self):
        with self.lock:
            loaded = len(self.cache)
        return {
            "contract_version": self.CONTRACT_VERSION,
            "enabled": self.enabled,
            "mode": "shadow",
            "backend": TinyMLPBackend.BACKEND,
            "backend_version": TinyMLPBackend.VERSION,
            "hidden": list(self.hidden),
            "loaded_models": loaded,
            "training_enabled": False,
            "historical_training": False,
            "dispatch_capability": False,
            "physical_authority": False,
            "ridge_baseline_changed": False,
        }


def install(core):
    engine = core.ENGINE
    if engine is None:
        return None
    existing = getattr(engine, "tiny_mlp_shadow", None)
    if existing is not None and getattr(engine, "_tiny_mlp_shadow_installed", False):
        return existing

    service = TinyMLPShadowService(core.STORE, engine)
    engine.tiny_mlp_shadow = service
    original = engine.process_agent

    def process_agent_with_tiny_mlp_shadow(agent, state_map, changed_entities=None):
        aid = str(agent["id"])
        with engine.lock:
            before = float(
                (engine.runtime.get(aid) or {}).get("last_inference_ts") or 0.0
            )
        # The authoritative path runs first and its return value is preserved exactly.
        result = original(agent, state_map, changed_entities)

        # Hard Stage-3 authority gate: neural inference is not even evaluated in Control.
        if not service.enabled or str(agent.get("mode") or "") != "shadow":
            return result
        with engine.lock:
            after = float(
                (engine.runtime.get(aid) or {}).get("last_inference_ts") or 0.0
            )
        if after <= before:
            return result
        try:
            policy = engine.policy(agent)
            service.observe(
                agent,
                policy,
                state_map,
                engine.temporal_history,
                timestamp=after,
            )
        except Exception as exc:
            # Shadow diagnostics must be fail-open with respect to the existing Ridge path.
            service.record_error(aid, exc)
        return result

    engine.process_agent = process_agent_with_tiny_mlp_shadow
    engine._tiny_mlp_shadow_installed = True
    core.STORE.event(
        None,
        "info",
        "tiny_mlp_shadow_ready",
        "Tiny MLP inference/persistence observer enabled in Shadow only",
        service.diagnostics(),
    )
    return service
