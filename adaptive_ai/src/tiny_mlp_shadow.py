"""Stage-3 tiny MLP Shadow observer.

The service is intentionally outside the authoritative Ridge -> ActionIntent -> Executor
decision chain. It observes only completed Shadow inferences, persists its own untrained
model plus the exact Stage-2 feature mask and publishes diagnostics in engine.runtime.
It never consumes rewards, Correct labels or Candidate training jobs and it never
dispatches Home Assistant services.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from observation_space import (
    ObservationMask,
    global_observation_catalog,
    observation_as_of,
    select_observation_mask,
)
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
    """Add isolated Stage-3 persistence without touching rl_models/Candidate storage."""
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
                mask_json TEXT,
                source_policy_revision TEXT,
                training_json TEXT,
                tournament_json TEXT,
                selected_backend TEXT NOT NULL DEFAULT 'diagonal_linucb',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            """
        )
        # The table only exists from Stage 3, but keep this additive migration so an
        # installation that briefly ran an intermediate 0.14.82 build is recoverable.
        columns = {
            str(row["name"] if hasattr(row, "keys") else row[1])
            for row in c.execute("PRAGMA table_info(tiny_mlp_shadow_models)").fetchall()
        }
        if "mask_json" not in columns:
            c.execute("ALTER TABLE tiny_mlp_shadow_models ADD COLUMN mask_json TEXT")
        if "source_policy_revision" not in columns:
            c.execute(
                "ALTER TABLE tiny_mlp_shadow_models ADD COLUMN source_policy_revision TEXT"
            )
        if "training_json" not in columns:
            c.execute("ALTER TABLE tiny_mlp_shadow_models ADD COLUMN training_json TEXT")
        if "tournament_json" not in columns:
            c.execute("ALTER TABLE tiny_mlp_shadow_models ADD COLUMN tournament_json TEXT")
        if "selected_backend" not in columns:
            c.execute(
                "ALTER TABLE tiny_mlp_shadow_models ADD COLUMN selected_backend TEXT NOT NULL DEFAULT 'diagonal_linucb'"
            )



def load_training_record(store, agent_id):
    ensure_tables(store)
    with store.conn() as c:
        row = c.execute(
            """
            SELECT model_json,mask_json,source_policy_revision,
                   training_json,tournament_json,selected_backend
            FROM tiny_mlp_shadow_models WHERE agent_id=?
            """,
            (str(agent_id),),
        ).fetchone()
    if not row:
        return None
    return {
        "model": json.loads(row[0]) if row[0] else None,
        "mask": json.loads(row[1]) if row[1] else None,
        "source_policy_revision": str(row[2] or "unknown"),
        "training": json.loads(row[3]) if row[3] else {},
        "tournament": json.loads(row[4]) if row[4] else {},
        "selected_backend": str(row[5] or "diagonal_linucb"),
    }


def publish_training_artifact(store, artifact):
    """Persist a verified offline-training artifact after parent stale-job checks."""
    if not isinstance(artifact, dict) or artifact.get("format") != "homemind-tiny-mlp-training-artifact":
        raise ValueError("invalid tiny MLP training artifact")
    ensure_tables(store)
    agent_id = str(artifact["agent_id"])
    model = dict(artifact.get("model") or {})
    mask = dict(artifact.get("mask") or {})
    source_revision = str(artifact.get("source_policy_revision") or "unknown")
    training = dict(artifact.get("trainer") or {})
    tournament = dict(artifact.get("tournament") or {})
    selected = str(artifact.get("selected_backend") or "diagonal_linucb")
    backend = TinyMLPBackend.deserialize(
        model,
        expected_schema_id=mask.get("schema_id"),
        expected_mask_id=mask.get("mask_id"),
        expected_feature_ids=mask.get("feature_ids"),
    )
    guard = getattr(store, "training_publish_guard", None)
    if callable(guard):
        guard(agent_id, model)
    now = time.time()
    with store.lock, store.conn() as c:
        c.execute(
            """
            INSERT INTO tiny_mlp_shadow_models
                (agent_id,backend,backend_version,feature_schema_id,feature_mask_id,
                 model_json,mask_json,source_policy_revision,training_json,tournament_json,
                 selected_backend,created_ts,updated_ts)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
                backend=excluded.backend,
                backend_version=excluded.backend_version,
                feature_schema_id=excluded.feature_schema_id,
                feature_mask_id=excluded.feature_mask_id,
                model_json=excluded.model_json,
                mask_json=excluded.mask_json,
                source_policy_revision=excluded.source_policy_revision,
                training_json=excluded.training_json,
                tournament_json=excluded.tournament_json,
                selected_backend=excluded.selected_backend,
                updated_ts=excluded.updated_ts
            """,
            (
                agent_id, backend.BACKEND, backend.VERSION, backend.schema_id,
                backend.mask_id,
                json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(mask, sort_keys=True, separators=(",", ":"), allow_nan=False),
                source_revision,
                json.dumps(training, sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(tournament, sort_keys=True, separators=(",", ":"), allow_nan=False),
                selected, now, now,
            ),
        )
    return {
        "agent_id": agent_id,
        "backend": backend.BACKEND,
        "trained": backend.trained,
        "selected_backend": selected,
        "model_revision": backend.model_revision,
        "model_checksum": backend.persisted_checksum or model.get("model_checksum"),
    }


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
        # agent_id -> exact model-lifecycle bundle. Nothing here changes Ridge state.
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

    @staticmethod
    def _source_policy_revision(policy):
        # tournament_revision intentionally survives ordinary online Ridge updates and
        # changes only when a genuinely fresh Train/Rebuild champion is constructed.
        return str(
            getattr(policy, "tournament_revision", None)
            or getattr(policy, "model_revision", None)
            or "unknown"
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

    def _load_record(self, agent_id):
        return load_training_record(self.store, agent_id)

    def _persist(self, agent_id, backend, mask, source_policy_revision):
        raw = backend.serialize()
        now = time.time()
        encoded = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        mask_encoded = json.dumps(
            mask.export(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """
                INSERT INTO tiny_mlp_shadow_models
                    (agent_id,backend,backend_version,feature_schema_id,
                     feature_mask_id,model_json,mask_json,source_policy_revision,
                     training_json,tournament_json,selected_backend,
                     created_ts,updated_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    backend=excluded.backend,
                    backend_version=excluded.backend_version,
                    feature_schema_id=excluded.feature_schema_id,
                    feature_mask_id=excluded.feature_mask_id,
                    model_json=excluded.model_json,
                    mask_json=excluded.mask_json,
                    source_policy_revision=excluded.source_policy_revision,
                    training_json=excluded.training_json,
                    tournament_json=excluded.tournament_json,
                    selected_backend=excluded.selected_backend,
                    updated_ts=excluded.updated_ts
                """,
                (
                    str(agent_id),
                    TinyMLPBackend.BACKEND,
                    TinyMLPBackend.VERSION,
                    backend.schema_id,
                    backend.mask_id,
                    encoded,
                    mask_encoded,
                    str(source_policy_revision),
                    "{}",
                    "{}",
                    "diagonal_linucb",
                    now,
                    now,
                ),
            )
        backend.persisted_checksum = raw.get("model_checksum")
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

    def _persisted_mask(
        self, agent, policy, state_map, registry, source_policy_revision
    ):
        """Recover the exact mask used by the persisted model before considering reselection."""
        record = self._load_record(agent["id"])
        if not record or not record.get("mask"):
            return None, record
        if record.get("source_policy_revision") != str(source_policy_revision):
            return None, record
        try:
            mask = ObservationMask.from_export(record["mask"])
            # A persisted mask is stable across ordinary state changes. Revalidate only
            # hard context admission so a source that became controllable/electrical is
            # never retained merely for restart continuity.
            catalog = global_observation_catalog(state_map, registry)
            eligible = set(catalog.get("eligible_entities") or ())
            forbidden = sorted(set(mask.selected_entities) - eligible)
            if forbidden:
                raise ValueError(
                    "NEEDS_RETRAIN: persisted tiny MLP mask contains newly excluded "
                    + ",".join(forbidden[:8])
                )
            return mask, record
        except Exception as exc:
            self.store.event(
                agent["id"],
                "warning",
                "tiny_mlp_shadow_mask_invalidated",
                "Persisted Tiny MLP Shadow feature mask was invalidated",
                {"reason": str(exc)[:400], "physical_authority": False},
            )
            return None, record

    def _backend(
        self,
        agent,
        policy,
        mask,
        *,
        source_policy_revision,
        persisted_record=None,
    ):
        aid = str(agent["id"])
        source_policy_revision = str(source_policy_revision)
        signature = self._signature(mask, policy)
        with self.lock:
            cached = self.cache.get(aid)
            if (
                cached
                and cached["signature"] == signature
                and cached.get("source_policy_revision") == source_policy_revision
            ):
                return cached["backend"], "memory"

            record = persisted_record
            if record is None:
                record = self._load_record(aid)
            raw = (record or {}).get("model")
            same_source = (
                (record or {}).get("source_policy_revision")
                == source_policy_revision
            )
            if raw is not None and same_source:
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
                    # The Stage-3 model is untrained and has no authority. A structural
                    # mismatch invalidates this isolated Shadow copy only; Live/Candidate
                    # policy and generation state are never touched or rebuilt.
                    self.store.event(
                        aid,
                        "warning",
                        "tiny_mlp_shadow_reinitialized",
                        "Tiny MLP Shadow copy was incompatible and was reinitialized",
                        {"reason": str(exc)[:400], "physical_authority": False},
                    )

            backend = self._new_backend(agent, policy, mask)
            self._persist(
                aid, backend, mask, source_policy_revision
            )
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
                    "source_policy_revision": source_policy_revision,
                    "architecture": list(backend.architecture),
                    "parameter_count": backend.parameter_count,
                    "training_enabled": False,
                    "dispatch_capability": False,
                },
            )
            return backend, "created"

    def _bundle(self, agent, policy, state_map):
        """Resolve model+mask once per source-policy lifecycle, never once per HA event."""
        aid = str(agent["id"])
        source_policy_revision = self._source_policy_revision(policy)
        actions = tuple(float(x) for x in policy.actions)
        horizons = tuple(int(x) for x in policy.horizons)
        with self.lock:
            cached = self.cache.get(aid)
            if (
                cached
                and cached.get("source_policy_revision") == source_policy_revision
                and cached["signature"][3] == actions
                and cached["signature"][4] == horizons
            ):
                return cached["mask"], cached["backend"], "memory"

        registry = self.engine.context.resolved_registry()
        mask, record = self._persisted_mask(
            agent, policy, state_map, registry, source_policy_revision
        )
        if mask is None:
            hints = list(
                getattr(getattr(policy, "schema", None), "entities", ()) or ()
            )
            relevance = dict(
                (getattr(policy, "selection_meta", {}) or {}).get(
                    "selection_scores"
                ) or {}
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
            persisted_record=record,
        )
        return mask, backend, model_source

    def observe(self, agent, policy, state_map, temporal, *, timestamp):
        if not self.enabled or str(agent.get("mode") or "") != "shadow":
            return None
        mask, backend, model_source = self._bundle(agent, policy, state_map)
        observation = observation_as_of(
            mask,
            state_map,
            temporal,
            float(timestamp),
            agent,
            home_provider=self.engine.context,
        )
        started = time.perf_counter_ns()
        chosen, confidence, arms, horizon, support, novelty = backend.predict(
            observation
        )
        inference_us = (time.perf_counter_ns() - started) / 1000.0
        result = {
            "contract_version": self.CONTRACT_VERSION,
            "enabled": True,
            "mode": "shadow",
            "shadow_only": True,
            "dispatch_capability": False,
            "physical_authority": False,
            "training_enabled": bool(backend.trained),
            "historical_training": bool(backend.trained),
            "baseline_backend": getattr(policy, "BACKEND", "unknown"),
            "source_policy_revision": self._source_policy_revision(policy),
            "backend": backend.BACKEND,
            "backend_version": backend.VERSION,
            "model_revision": backend.model_revision,
            "model_checksum": backend.persisted_checksum,
            "model_source": model_source,
            "schema_id": mask.schema_id,
            "mask_id": mask.mask_id,
            "selected_feature_count": len(mask.feature_ids),
            "global_feature_count": int(mask.global_feature_count),
            "missing_feature_count": int(
                observation.get("missing_feature_count") or 0
            ),
            "architecture": list(backend.architecture),
            "parameter_count": backend.parameter_count,
            "dtype": backend.DTYPE,
            "timestamp": float(timestamp),
            "inference_us": float(inference_us),
            "chosen_index": int(chosen["index"]),
            "chosen_value": float(chosen["value"]),
            "score": float(chosen["score"]),
            # Explicit zeros: an untrained random network has no calibrated confidence.
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
            "note": (
                "trained supervised neural inference; Shadow diagnostic only"
                if backend.trained else
                "untrained neural inference; diagnostic only"
            ),
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
        record = self._load_record(agent_id)
        return (record or {}).get("model")

    def persisted_mask(self, agent_id):
        record = self._load_record(agent_id)
        return (record or {}).get("mask")

    def persisted_record(self, agent_id):
        record = self._load_record(agent_id)
        return dict(record or {})

    def invalidate(self, agent_id):
        with self.lock:
            self.cache.pop(str(agent_id), None)

    def predict_persisted(self, agent, policy, state_map, temporal, *, timestamp, require_selected=False):
        record = self._load_record(agent["id"])
        if not record or not record.get("model") or not record.get("mask"):
            return None
        if require_selected and record.get("selected_backend") != TinyMLPBackend.BACKEND:
            return None
        backend = TinyMLPBackend.deserialize(
            record["model"],
            expected_schema_id=record["mask"].get("schema_id"),
            expected_mask_id=record["mask"].get("mask_id"),
            expected_feature_ids=record["mask"].get("feature_ids"),
            expected_actions=policy.actions,
            expected_horizons=policy.horizons,
        )
        if require_selected and not backend.trained:
            return None
        mask = ObservationMask.from_export(record["mask"])
        observation = observation_as_of(
            mask, state_map, temporal, float(timestamp), agent,
            home_provider=self.engine.context,
        )
        chosen, confidence, arms, horizon, support, novelty = backend.predict(observation)
        return {
            "backend": backend,
            "record": record,
            "observation": observation,
            "chosen": chosen,
            "confidence": confidence,
            "arms": arms,
            "horizon": horizon,
            "support": support,
            "novelty": novelty,
        }

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
            "mask_selection": "once_per_source_policy_revision_then_persisted",
            "training_enabled": bool(OPTIONS.get("tiny_mlp_supervised_training_enabled", True)),
            "historical_training": bool(OPTIONS.get("tiny_mlp_supervised_training_enabled", True)),
            "dispatch_capability": False,
            "physical_authority": False,
            "ridge_baseline_changed": False,
        }


def install(core):
    engine = core.ENGINE
    if engine is None:
        return None
    existing = getattr(engine, "tiny_mlp_shadow", None)
    if existing is not None and getattr(
        engine, "_tiny_mlp_shadow_installed", False
    ):
        return existing

    service = TinyMLPShadowService(core.STORE, engine)
    engine.tiny_mlp_shadow = service
    original = engine.process_agent

    def process_agent_with_tiny_mlp_shadow(
        agent, state_map, changed_entities=None
    ):
        aid = str(agent["id"])
        with engine.lock:
            before = float(
                (engine.runtime.get(aid) or {}).get("last_inference_ts") or 0.0
            )

        # Authoritative behavior runs first. Its Ridge/Correct/Composer/Candidate/
        # ActionIntent/Executor result is returned unchanged.
        result = original(agent, state_map, changed_entities)

        # Hard Stage-3 authority gate: neural inference is not even evaluated in Control.
        if (
            not service.enabled
            or str(agent.get("mode") or "") != "shadow"
        ):
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
            # Shadow diagnostics fail open with respect to the authoritative Ridge path.
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
