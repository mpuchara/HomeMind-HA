"""Flagged shadow runtime for Stage-12 backend comparison.

The shadow backend observes the exact live feature vector and allowed action set but never
returns an ActionIntent and never calls Executor. Logged rewards train only the action that
was actually executed. TrialRecord rewards keep their logged propensities for later
benchmarking; they are not counterfactual labels for unchosen actions.
"""
from __future__ import annotations

import json
import math
import os
import time

from policy_full_ridge import FullRidgeLinUCBBackend
from policy_backend_benchmark import semantic_feature_indices
from settings import OPTIONS


FAST_DOMAINS = {"light", "switch", "input_boolean"}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def ensure_shadow_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS policy_backend_shadow_models (
                agent_id TEXT NOT NULL,
                backend TEXT NOT NULL,
                backend_version INTEGER NOT NULL,
                model_json TEXT NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(agent_id,backend)
            );
            CREATE TABLE IF NOT EXISTS policy_backend_shadow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_policy_backend_shadow_events_agent
                ON policy_backend_shadow_events(agent_id,ts);
            CREATE TABLE IF NOT EXISTS policy_backend_shadow_sources (
                agent_id TEXT NOT NULL,
                backend TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                applied_ts REAL NOT NULL,
                PRIMARY KEY(agent_id,backend,source_type,source_id)
            );
            """
        )


class PolicyBackendShadowService:
    def __init__(self, store, enabled=None):
        self.store = store
        if enabled is None:
            env = os.environ.get("HOMEMIND_POLICY_BACKEND_SHADOW", "").strip().lower()
            enabled = bool(OPTIONS.get("policy_backend_shadow_enabled", False) or env in {"1", "true", "yes", "on"})
        self.enabled = bool(enabled)
        self.max_features = max(4, int(OPTIONS.get("policy_backend_shadow_max_features", 24)))
        self.ridge = float(OPTIONS.get("policy_backend_shadow_ridge", 1.0))
        self.alpha = float(OPTIONS.get("rl_alpha", 0.65))
        self.backends = {}
        self.last_predictions = {}
        ensure_shadow_tables(store)

    @staticmethod
    def _fast(agent):
        target = str(agent.get("target_entity") or "")
        domain = target.split(".", 1)[0]
        return domain in FAST_DOMAINS

    def _benchmark_gate(self, agent_id):
        try:
            with self.store.conn() as c:
                row = c.execute(
                    """SELECT benchmark_version,result_json FROM policy_backend_benchmarks
                       WHERE agent_id=? ORDER BY created_ts DESC LIMIT 1""",
                    (str(agent_id),),
                ).fetchone()
        except Exception:
            row = None
        if not row:
            return {"supported": False, "reason": "no_persisted_benchmark"}
        result = _json(row["result_json"], {})
        if int(row["benchmark_version"] or 0) < 2:
            return {"supported": False, "reason": "benchmark_contract_too_old"}
        if str(result.get("candidate_status") or "") != "shadow_candidate_supported":
            return {"supported": False, "reason": "latest_benchmark_keeps_diagonal"}
        indices = [int(x) for x in ((result.get("feature_selection") or {}).get("indices") or [])]
        hp = result.get("hyperparameter_selection") or {}
        if not indices:
            return {"supported": False, "reason": "benchmark_projection_missing"}
        return {
            "supported": True, "reason": "supported_future_holdout",
            "run_id": result.get("run_id"), "feature_indices": indices,
            "ridge": float(hp.get("ridge", self.ridge)),
            "alpha": float(hp.get("alpha", self.alpha)),
        }

    def _load_model(self, agent_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT model_json FROM policy_backend_shadow_models WHERE agent_id=? AND backend=?",
                (str(agent_id), FullRidgeLinUCBBackend.BACKEND),
            ).fetchone()
        return _json(row[0], {}) if row else None

    def _persist(self, agent_id, backend):
        raw = backend.serialize()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO policy_backend_shadow_models
                   (agent_id,backend,backend_version,model_json,updated_ts)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(agent_id,backend) DO UPDATE SET
                     backend_version=excluded.backend_version,
                     model_json=excluded.model_json,updated_ts=excluded.updated_ts""",
                (str(agent_id), FullRidgeLinUCBBackend.BACKEND, FullRidgeLinUCBBackend.VERSION,
                 json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False), time.time()),
            )

    def _event(self, agent_id, event_type, payload):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "INSERT INTO policy_backend_shadow_events(agent_id,ts,event_type,payload_json) VALUES(?,?,?,?)",
                (str(agent_id), time.time(), str(event_type),
                 json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)),
            )

    def _backend(self, agent, policy, features, labels):
        aid = str(agent["id"])
        gate = self._benchmark_gate(aid)
        if not gate.get("supported"):
            self.backends.pop(aid, None)
            return None, gate
        selected = sorted(set(int(x) for x in gate["feature_indices"]))
        target_actions = [float(x) for x in policy.actions]
        target_horizons = [int(x) for x in policy.horizons]
        cached = self.backends.get(aid)
        if (cached is not None and cached.actions == target_actions
                and cached.horizons == target_horizons
                and cached.feature_indices == selected
                and abs(float(cached.ridge) - float(gate["ridge"])) <= 1e-12
                and abs(float(cached.alpha) - float(gate["alpha"])) <= 1e-12):
            return cached, gate
        raw = self._load_model(aid)
        if raw:
            try:
                backend = FullRidgeLinUCBBackend.deserialize(raw)
                if (backend.actions == target_actions and backend.horizons == target_horizons
                        and backend.feature_indices == selected
                        and abs(float(backend.ridge) - float(gate["ridge"])) <= 1e-12
                        and abs(float(backend.alpha) - float(gate["alpha"])) <= 1e-12):
                    self.backends[aid] = backend
                    return backend, gate
            except Exception:
                pass
        backend = FullRidgeLinUCBBackend(
            actions=policy.actions, horizons=policy.horizons, feature_indices=selected,
            alpha=gate["alpha"], ridge=gate["ridge"],
        )
        self.backends[aid] = backend
        self._persist(aid, backend)
        self._event(aid, "shadow_backend_created", {
            "backend": backend.BACKEND, "backend_version": backend.VERSION,
            "feature_indices": selected, "source": "persisted_benchmark_v2",
            "benchmark_run_id": gate.get("run_id"),
        })
        return backend, gate

    def observe_decision(self, agent, policy, features, labels, allowed_indices=None, timestamp=None):
        if not self.enabled or not self._fast(agent):
            return None
        backend, gate = self._backend(agent, policy, features, labels)
        if backend is None:
            result = {
                "backend": FullRidgeLinUCBBackend.BACKEND,
                "backend_version": FullRidgeLinUCBBackend.VERSION,
                "evaluation": "shadow_waiting_for_supported_benchmark",
                "reason": gate.get("reason"), "dispatch_capability": False,
            }
            self.last_predictions[str(agent["id"])] = result
            return result
        chosen, confidence, _arms, horizon, support, novelty = backend.predict(
            features, allowed_indices=allowed_indices
        )
        result = {
            "backend": backend.BACKEND, "backend_version": backend.VERSION,
            "chosen_index": int(chosen["index"]), "chosen_value": float(chosen["value"]),
            "mean": float(chosen["mean"]), "confidence": float(confidence),
            "horizon": int(horizon), "support": float(support), "novelty": float(novelty),
            "evaluation": "shadow_only_no_dispatch",
            "benchmark_run_id": gate.get("run_id"),
            "dispatch_capability": False,
        }
        self.last_predictions[str(agent["id"])] = result
        return result

    def observe_reward(self, agent, pending, reward, reason=None):
        if not self.enabled or not self._fast(agent) or not pending:
            return
        backend = self.backends.get(str(agent["id"]))
        if backend is None:
            return
        try:
            horizon = int(pending.get("policy_head") or min(backend.horizons))
            action_idx = int(pending["action_index"])
            features = dict(pending["features"])
            value = float(reward)
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        backend.update(horizon, action_idx, features, value)
        self._persist(agent["id"], backend)
        self._event(agent["id"], "logged_reward", {
            "horizon": horizon, "executed_action": action_idx, "reward": value,
            "reason": reason, "counterfactual_rewards_added": 0,
        })

    def observe_demonstration(self, agent, policy, features, action_idx, timestamp=None):
        if not self.enabled or not self._fast(agent):
            return
        backend = self._backend(agent, policy, features, {})
        for horizon in backend.horizons:
            backend.update(horizon, int(action_idx), features, 1.0, timestamp)
        self._persist(agent["id"], backend)
        self._event(agent["id"], "manual_demonstration", {
            "action": int(action_idx), "other_actions_rewarded": False,
        })

    def observe_trial_record(self, record, policy=None):
        if not self.enabled or not record or record.get("reward") is None:
            return False
        aid = str(record.get("owner_agent_id") or "")
        trial_id = str(record.get("trial_id") or "")
        backend = self.backends.get(aid)
        context = _json(record.get("context_json"), {})
        if not aid or not trial_id:
            return False
        if backend is None and policy is not None:
            agent = self.store.get_agent_config(aid)
            if agent:
                features = {int(k): float(v) for k, v in dict(context.get("policy_features") or {}).items()}
                labels = dict(context.get("policy_feature_labels") or {})
                backend, _gate = self._backend(agent, policy, features, labels)
        if backend is None:
            return False
        with self.store.conn() as c:
            if c.execute(
                """SELECT 1 FROM policy_backend_shadow_sources
                   WHERE agent_id=? AND backend=? AND source_type='trial_record' AND source_id=?""",
                (aid, FullRidgeLinUCBBackend.BACKEND, trial_id),
            ).fetchone():
                return False
        assigned = _json(record.get("assigned_action_json"), {})
        try:
            action_idx = int(assigned["index"])
            horizon = int(round(float(context.get("horizon") or min(backend.horizons))))
            features = {int(k): float(v) for k, v in dict(context.get("policy_features") or {}).items()}
            reward = float(record["reward"])
        except (KeyError, TypeError, ValueError):
            return False
        if horizon not in backend.horizons or action_idx < 0 or action_idx >= len(backend.actions):
            return False
        backend.update(horizon, action_idx, features, reward,
                       _json(record.get("episode_result_json"), {}).get("finished_at"))
        raw = backend.serialize()
        now = time.time()
        # Model state and source marker commit together: restart cannot count the same
        # TrialRecord twice even if the process stops immediately after this transaction.
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO policy_backend_shadow_models
                   (agent_id,backend,backend_version,model_json,updated_ts) VALUES(?,?,?,?,?)
                   ON CONFLICT(agent_id,backend) DO UPDATE SET
                     backend_version=excluded.backend_version,model_json=excluded.model_json,
                     updated_ts=excluded.updated_ts""",
                (aid, FullRidgeLinUCBBackend.BACKEND, FullRidgeLinUCBBackend.VERSION,
                 json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False), now),
            )
            c.execute(
                """INSERT INTO policy_backend_shadow_sources
                   (agent_id,backend,source_type,source_id,applied_ts) VALUES(?,?,?,?,?)""",
                (aid, FullRidgeLinUCBBackend.BACKEND, "trial_record", trial_id, now),
            )
            c.execute(
                "INSERT INTO policy_backend_shadow_events(agent_id,ts,event_type,payload_json) VALUES(?,?,?,?)",
                (aid, now, "trial_record_reward", json.dumps({
                    "trial_id": trial_id, "executed_action": action_idx,
                    "reward": reward, "logged_propensity": record.get("propensity"),
                    "counterfactual_rewards_added": 0,
                }, sort_keys=True, separators=(",", ":"), allow_nan=False)),
            )
        return True

    def diagnostics(self, agent_id=None):
        if agent_id is not None:
            backend = self.backends.get(str(agent_id))
            return {
                "enabled": self.enabled, "mode": "shadow", "dispatch_capability": False,
                "benchmark_gate": self._benchmark_gate(str(agent_id)) if self.enabled else {"supported": False, "reason": "disabled"},
                "backend": backend.diagnostics() if backend else None,
                "last_prediction": self.last_predictions.get(str(agent_id)),
            }
        return {
            "enabled": self.enabled, "mode": "shadow", "dispatch_capability": False,
            "backend_name": FullRidgeLinUCBBackend.BACKEND,
            "agents": len(self.backends), "default_backend_changed": False,
        }


def install_policy_backend_shadow(engine, store):
    service = PolicyBackendShadowService(store)
    engine.policy_backend_shadow = service
    return service
