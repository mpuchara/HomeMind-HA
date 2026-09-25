"""Stage-7 Candidate lifecycle for conservative TinyMLP Offline RL.

The module is installed before the Candidate worker starts so a queued Offline-RL child
recovered after restart cannot fall through to the historical Ridge rebuild path.

Workflow:
selected parent generation
-> compatible trusted Stage-6 experiences
-> clone exact parent TinyMLP
-> conservative offline policy improvement
-> offline regression/reward gate
-> ordinary Candidate Shadow A/B

No code in this module creates ActionIntent, invokes Executor, calls Home Assistant or
promotes a neural Candidate to Live/Control.
"""
from __future__ import annotations

import json
import math
import time
import uuid

import agent_candidate_lineage as lineage
from agent_candidate_conservative_correct import (
    _copy_parent_snapshot,
    _persist_gate,
    _set_candidate_work,
)
from observation_space import ObservationMask
from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_correct import nearest_action
from policy_tiny_mlp_offline_rl import (
    offline_rl_gate,
    train_conservative_offline_rl,
)
from policy_tiny_mlp_training import build_training_artifact
from settings import OPTIONS
from telemetry import HEAVY_JOBS
from tiny_mlp_shadow import (
    load_training_record,
    publish_training_artifact,
    restore_training_record,
)
from training_budget import TRAINING_BUDGET


REASON = "offline_rl"
_BUDGET_THREAD_NAME = "adaptive-ai-candidate-offline-rl"


def _json(value, default=None):
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def _ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_offline_rl_runs (
                run_id TEXT PRIMARY KEY,
                root_agent_id TEXT NOT NULL,
                parent_generation_id TEXT NOT NULL,
                parent_agent_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                created_ts REAL NOT NULL,
                finished_ts REAL,
                status TEXT NOT NULL,
                source_model_revision TEXT,
                source_model_checksum TEXT,
                feature_schema_id TEXT,
                feature_mask_id TEXT,
                trusted_total INTEGER NOT NULL DEFAULT 0,
                compatible_total INTEGER NOT NULL DEFAULT 0,
                train_samples INTEGER NOT NULL DEFAULT 0,
                holdout_samples INTEGER NOT NULL DEFAULT 0,
                manual_samples INTEGER NOT NULL DEFAULT 0,
                trainer_json TEXT NOT NULL DEFAULT '{}',
                gate_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_offline_rl_parent
                ON candidate_offline_rl_runs(parent_generation_id,created_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_candidate_offline_rl_candidate
                ON candidate_offline_rl_runs(candidate_id,created_ts DESC);
            """
        )


def _candidate_edge_by_child(manager, candidate_id):
    finder = getattr(manager, "_row_by_candidate", None)
    if callable(finder):
        return finder(str(candidate_id))
    with manager.store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_candidates WHERE candidate_id=?",
            (str(candidate_id),),
        ).fetchone()
    return dict(row) if row else None


def _policy_for(manager, agent):
    manager.engine.models.pop(str(agent["id"]), None)
    return manager.engine.policy(agent)


def _source(manager, generation, agent):
    record = load_training_record(manager.store, agent["id"])
    if not record:
        return None, "parent_has_no_tiny_mlp_artifact"
    if record.get("selected_backend") != TinyMLPBackend.BACKEND:
        return None, "parent_tiny_mlp_not_selected"
    tournament = dict(record.get("tournament") or {})
    if not bool(tournament.get("passed")):
        return None, "parent_neural_tournament_not_passed"
    model_raw = dict(record.get("model") or {})
    if not bool(model_raw.get("trained")):
        return None, "parent_tiny_mlp_untrained"

    if str(generation.get("generation_type") or "") == "candidate":
        edge = _candidate_edge_by_child(manager, agent["id"])
        gate = _json((edge or {}).get("offline_gate_json"), {})
        if not bool(gate.get("passed")):
            return None, "parent_candidate_offline_gate_not_passed"

    try:
        mask = ObservationMask.from_export(dict(record.get("mask") or {}))
    except Exception as exc:
        return None, "parent_feature_mask_incompatible:" + str(exc)

    policy = _policy_for(manager, agent)
    source_revision = str(
        getattr(policy, "tournament_revision", None)
        or getattr(policy, "model_revision", None)
        or "unknown"
    )
    if str(record.get("source_policy_revision") or "unknown") != source_revision:
        return None, "parent_source_policy_revision_changed"
    try:
        backend = TinyMLPBackend.deserialize(
            model_raw,
            expected_schema_id=mask.schema_id,
            expected_mask_id=mask.mask_id,
            expected_feature_ids=mask.feature_ids,
            expected_actions=policy.actions,
            expected_horizons=policy.horizons,
        )
    except Exception as exc:
        return None, "parent_tiny_mlp_incompatible:" + str(exc)
    if not backend.trained:
        return None, "parent_tiny_mlp_untrained"
    return {
        "record": record,
        "mask": mask,
        "backend": backend,
        "policy": policy,
        "checksum": backend.serialize().get("model_checksum"),
    }, None


def _trusted_rows(manager, root_agent_id, backend, mask, *, limit=None):
    limit = max(
        1,
        int(
            limit
            or OPTIONS.get("offline_rl_max_trusted_samples", 512)
            or 512
        ),
    )
    with manager.store.conn() as c:
        exists = c.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='automatic_reward_experiences'"""
        ).fetchone()
        if not exists:
            return [], 0, {
                "schema_mismatch": 0,
                "mask_mismatch": 0,
                "feature_mismatch": 0,
                "action_mismatch": 0,
                "invalid_reward": 0,
            }
        raw = [
            dict(row)
            for row in c.execute(
                """SELECT * FROM automatic_reward_experiences
                   WHERE agent_id=? AND status='trusted'
                     AND trusted_reward IS NOT NULL
                   ORDER BY action_ts ASC,created_ts ASC""",
                (str(root_agent_id),),
            ).fetchall()
        ]
    trusted_total = len(raw)
    compatible = []
    rejected = {
        "schema_mismatch": 0,
        "mask_mismatch": 0,
        "feature_mismatch": 0,
        "action_mismatch": 0,
        "invalid_reward": 0,
    }
    for row in raw:
        if str(row.get("observation_schema_id") or "") != str(mask.schema_id):
            rejected["schema_mismatch"] += 1
            continue
        if str(row.get("observation_mask_id") or "") != str(mask.mask_id):
            rejected["mask_mismatch"] += 1
            continue
        observation = _json(row.get("observation_json"), {})
        if tuple(str(x) for x in observation.get("feature_ids") or ()) != tuple(
            backend.feature_ids
        ):
            rejected["feature_mismatch"] += 1
            continue
        try:
            values = [float(x) for x in observation.get("values") or ()]
        except (TypeError, ValueError):
            rejected["feature_mismatch"] += 1
            continue
        if (
            len(values) != backend.input_size
            or not all(math.isfinite(value) for value in values)
        ):
            rejected["feature_mismatch"] += 1
            continue
        try:
            action_idx = int(row.get("action_index"))
            reward = float(row.get("trusted_reward"))
        except (TypeError, ValueError):
            rejected["invalid_reward"] += 1
            continue
        if not (
            0 <= action_idx < len(backend.actions)
            and math.isfinite(reward)
        ):
            rejected["invalid_reward"] += 1
            continue
        action_value = row.get("action_value")
        if action_value is not None:
            try:
                mismatch = abs(
                    float(backend.actions[action_idx]) - float(action_value)
                )
            except (TypeError, ValueError):
                mismatch = math.inf
            tolerance = max(
                1e-6,
                abs(float(backend.actions[action_idx])) * 1e-6,
            )
            if mismatch > tolerance:
                rejected["action_mismatch"] += 1
                continue
        confidence = max(0.0, min(1.0, float(row.get("confidence") or 0.0)))
        reliability = max(
            0.0,
            min(1.0, float(row.get("source_reliability") or 0.0)),
        )
        weight = max(0.05, confidence * reliability)
        compatible.append({
            "resolution_key": row.get("resolution_key"),
            "trial_id": row.get("trial_id"),
            "decision_id": row.get("decision_id"),
            "timestamp": float(row.get("action_ts") or row.get("created_ts") or 0.0),
            "observation": observation,
            "action_idx": action_idx,
            "action_value": float(backend.actions[action_idx]),
            "reward": reward,
            "weight": weight,
            "confidence": confidence,
            "source_reliability": reliability,
            "outcome": row.get("outcome"),
            "source_entity_id": row.get("source_entity_id"),
            "source_origin": row.get("source_origin"),
        })
    if len(compatible) > limit:
        compatible = compatible[-limit:]
    return compatible, trusted_total, rejected


def _manual_anchors(manager, parent_agent_id, backend, *, limit=64):
    with manager.store.conn() as c:
        has_samples = c.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='tiny_mlp_correct_samples'"""
        ).fetchone()
        has_batches = c.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='tiny_mlp_correct_batches'"""
        ).fetchone()
        if not has_samples or not has_batches:
            return []
        rows = [
            dict(row)
            for row in c.execute(
                """SELECT s.label_id,s.sample_ts,s.desired,s.observation_json,
                          b.batch_id,b.finished_ts
                   FROM tiny_mlp_correct_samples s
                   JOIN tiny_mlp_correct_batches b ON b.batch_id=s.batch_id
                   WHERE b.candidate_id=? AND s.usable=1
                     AND s.observation_json IS NOT NULL
                   ORDER BY COALESCE(b.finished_ts,b.created_ts) DESC,s.sample_ts DESC
                   LIMIT ?""",
                (str(parent_agent_id), max(1, int(limit))),
            ).fetchall()
        ]
    anchors = []
    seen = set()
    for row in rows:
        observation = _json(row.get("observation_json"), {})
        if tuple(str(x) for x in observation.get("feature_ids") or ()) != tuple(
            backend.feature_ids
        ):
            continue
        key = (
            row.get("label_id"),
            round(float(row.get("sample_ts") or 0.0), 6),
        )
        if key in seen:
            continue
        seen.add(key)
        try:
            desired = float(row.get("desired"))
        except (TypeError, ValueError):
            continue
        anchors.append({
            "label_id": row.get("label_id"),
            "timestamp": float(row.get("sample_ts") or 0.0),
            "observation": observation,
            "action_idx": nearest_action(backend.actions, desired),
            "desired": desired,
            "weight": 1.0,
            "source": "manual_correct_anchor",
        })
    anchors.reverse()
    return anchors


def _split(rows):
    rows = list(rows or ())
    if not rows:
        return [], []
    holdout_fraction = max(
        0.10,
        min(
            0.40,
            float(OPTIONS.get("offline_rl_holdout_fraction", 0.25) or 0.25),
        ),
    )
    min_holdout = max(
        1,
        int(OPTIONS.get("offline_rl_min_holdout_samples", 8) or 8),
    )
    holdout_count = max(
        min_holdout,
        int(round(len(rows) * holdout_fraction)),
    )
    if holdout_count >= len(rows):
        holdout_count = max(1, len(rows) // 3)
    return rows[:-holdout_count], rows[-holdout_count:]


def readiness(manager, generation, agent):
    source, source_error = _source(manager, generation, agent)
    if source is None:
        return {
            "ready": False,
            "reason": source_error,
            "trusted_total": 0,
            "compatible_total": 0,
            "train_samples": 0,
            "holdout_samples": 0,
            "manual_samples": 0,
        }
    rows, trusted_total, rejected = _trusted_rows(
        manager,
        generation["root_agent_id"],
        source["backend"],
        source["mask"],
    )
    train_rows, holdout_rows = _split(rows)
    anchors = _manual_anchors(manager, agent["id"], source["backend"])
    support = {}
    for row in train_rows:
        key = str(int(row["action_idx"]))
        support[key] = int(support.get(key, 0)) + 1
    min_total = int(
        OPTIONS.get("offline_rl_min_trusted_samples", 24) or 24
    )
    min_holdout = int(
        OPTIONS.get("offline_rl_min_holdout_samples", 8) or 8
    )
    min_supported = min(
        len(source["backend"].actions),
        int(OPTIONS.get("offline_rl_min_supported_actions", 2) or 2),
    )
    supported = sum(
        1
        for value in support.values()
        if value >= int(OPTIONS.get("offline_rl_min_action_support", 4) or 4)
    )
    reason = None
    if len(rows) < min_total:
        reason = "insufficient_trusted_reward_samples"
    elif len(holdout_rows) < min_holdout:
        reason = "insufficient_offline_holdout"
    elif supported < min_supported:
        reason = "insufficient_action_support"
    return {
        "ready": reason is None,
        "reason": reason,
        "parent_generation_id": generation["generation_id"],
        "root_agent_id": generation["root_agent_id"],
        "parent_agent_id": agent["id"],
        "source_model_revision": source["backend"].model_revision,
        "source_model_checksum": source["checksum"],
        "feature_schema_id": source["mask"].schema_id,
        "feature_mask_id": source["mask"].mask_id,
        "trusted_total": int(trusted_total),
        "compatible_total": len(rows),
        "train_samples": len(train_rows),
        "holdout_samples": len(holdout_rows),
        "manual_samples": len(anchors),
        "action_support": support,
        "rejected": rejected,
        "behavior_propensity_known": False,
        "online_exploration": False,
    }


def _create_run(manager, row, generation, source, dataset, trusted_total):
    run_id = str(uuid.uuid4())
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """INSERT INTO candidate_offline_rl_runs
               (run_id,root_agent_id,parent_generation_id,parent_agent_id,candidate_id,
                created_ts,status,source_model_revision,source_model_checksum,
                feature_schema_id,feature_mask_id,trusted_total,compatible_total,
                train_samples,holdout_samples,manual_samples)
               VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                str(generation["root_agent_id"]),
                str(generation["generation_id"]),
                str(row["parent_agent_id"]),
                str(row["candidate_id"]),
                now,
                str(source["backend"].model_revision),
                str(source["checksum"] or ""),
                str(source["mask"].schema_id),
                str(source["mask"].mask_id),
                int(trusted_total),
                int(len(dataset["all"])),
                int(len(dataset["train"])),
                int(len(dataset["holdout"])),
                int(len(dataset["manual"])),
            ),
        )
    return run_id


def _finish_run(manager, run_id, status, *, trainer=None, gate=None, error=None):
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE candidate_offline_rl_runs
               SET finished_ts=?,status=?,trainer_json=?,gate_json=?,error=?
               WHERE run_id=?""",
            (
                time.time(),
                str(status),
                json.dumps(
                    trainer or {},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                json.dumps(
                    gate or {},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                None if error is None else str(error)[:800],
                str(run_id),
            ),
        )


def _latest_run(manager, candidate_id):
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_offline_rl_runs
               WHERE candidate_id=?
               ORDER BY created_ts DESC LIMIT 1""",
            (str(candidate_id),),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["trainer"] = _json(out.pop("trainer_json", "{}"), {})
    out["gate"] = _json(out.pop("gate_json", "{}"), {})
    return out


def _current_parent_source_still_matches(manager, parent, checksum, revision):
    generation = lineage._row(manager.store, agent_id=str(parent["id"]))
    if not generation:
        return False
    source, _reason = _source(manager, generation, parent)
    if source is None:
        return False
    return (
        str(source["checksum"] or "") == str(checksum or "")
        and str(source["backend"].model_revision) == str(revision)
    )


def install(manager):
    if getattr(manager, "_candidate_offline_rl_installed", False):
        return manager
    _ensure_tables(manager.store)
    original_start = manager._start_build
    original_status = manager.status
    original_list_status = manager.list_status

    def start_build(row):
        if str(row.get("reason") or "") != REASON:
            return original_start(row)

        parent = manager.store.get_agent_config(str(row["parent_agent_id"]))
        candidate = manager.store.get_agent_config(str(row["candidate_id"]))
        generation = lineage._row(
            manager.store, agent_id=str(row["parent_agent_id"])
        )
        if not parent or not candidate or not generation:
            manager._fail(row, "Offline RL parent/Candidate generation disappeared")
            return True

        source, source_error = _source(manager, generation, parent)
        if source is None:
            manager._fail(row, "Offline RL unavailable: " + str(source_error))
            return True

        rows, trusted_total, rejected = _trusted_rows(
            manager,
            generation["root_agent_id"],
            source["backend"],
            source["mask"],
        )
        train_rows, holdout_rows = _split(rows)
        manual = _manual_anchors(
            manager, parent["id"], source["backend"]
        )
        dataset = {
            "all": rows,
            "train": train_rows,
            "holdout": holdout_rows,
            "manual": manual,
            "rejected": rejected,
        }

        min_total = int(
            OPTIONS.get("offline_rl_min_trusted_samples", 24) or 24
        )
        min_holdout = int(
            OPTIONS.get("offline_rl_min_holdout_samples", 8) or 8
        )
        if len(rows) < min_total or len(holdout_rows) < min_holdout:
            gate = {
                "contract": "tiny_mlp_offline_rl_gate_v1",
                "status": "insufficient_evidence",
                "passed": False,
                "reasons": [
                    "insufficient_trusted_reward_samples"
                    if len(rows) < min_total
                    else "insufficient_offline_holdout"
                ],
                "samples_total": len(rows),
                "train_samples": len(train_rows),
                "holdout_samples": len(holdout_rows),
                "trusted_total": trusted_total,
                "rejected": rejected,
                "online_exploration": False,
                "physical_authority": False,
            }
            _persist_gate(manager, row, gate)
            return True

        owner = "candidate_offline_rl"
        if not HEAVY_JOBS.acquire(owner):
            _set_candidate_work(
                manager,
                parent["id"],
                state="queued",
                phase="waiting_for_heavy_slot",
                progress=0.0,
                blocked_by=HEAVY_JOBS.owner,
                policy_backend=TinyMLPBackend.BACKEND,
            )
            return False

        TRAINING_BUDGET.begin(thread_name=_BUDGET_THREAD_NAME)
        run_id = None
        previous_child_record = load_training_record(
            manager.store, candidate["id"]
        )
        published = False
        build_revision = int(row.get("feedback_revision") or 0)
        try:
            now = time.time()
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates
                       SET state='building',build_revision=?,dirty=0,
                           build_started_ts=?,build_finished_ts=NULL,
                           comparison_started_ts=NULL,comparison_json='{}',
                           offline_gate_json='{}',last_error=NULL,updated_ts=?
                       WHERE parent_agent_id=? AND candidate_id=?""",
                    (
                        build_revision,
                        now,
                        now,
                        str(row["parent_agent_id"]),
                        str(row["candidate_id"]),
                    ),
                )
            _set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="offline_rl_snapshot",
                progress=0.05,
                started_ts=now,
                blocked_by=None,
                policy_backend=TinyMLPBackend.BACKEND,
            )
            candidate = _copy_parent_snapshot(
                manager, parent["id"], candidate["id"]
            )
            manager._sync_feedback(parent, candidate)
            candidate = (
                manager.store.get_agent_config(candidate["id"])
                or candidate
            )
            run_id = _create_run(
                manager, row, generation, source, dataset, trusted_total
            )
            manager.store.event(
                parent["id"],
                "info",
                "candidate_offline_rl_started",
                "Offline RL started from trusted observed experiences",
                {
                    "run_id": run_id,
                    "candidate_id": candidate["id"],
                    "parent_generation_id": generation["generation_id"],
                    "trusted_total": trusted_total,
                    "compatible_total": len(rows),
                    "train_samples": len(train_rows),
                    "holdout_samples": len(holdout_rows),
                    "manual_anchors": len(manual),
                    "online_exploration": False,
                },
            )

            _set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="offline_rl_training",
                progress=0.35,
                labels_total=len(train_rows),
                replay_samples=len(train_rows),
                holdout_samples=len(holdout_rows),
                policy_backend=TinyMLPBackend.BACKEND,
            )
            child_backend, trainer = train_conservative_offline_rl(
                source["backend"],
                train_rows,
                manual_samples=manual,
                max_samples=int(
                    OPTIONS.get("offline_rl_max_train_samples", 384) or 384
                ),
                max_epochs=int(
                    OPTIONS.get("offline_rl_max_epochs", 6) or 6
                ),
                batch_size=int(
                    OPTIONS.get("offline_rl_batch_size", 16) or 16
                ),
                learning_rate=float(
                    OPTIONS.get("offline_rl_learning_rate", 0.0015)
                    or 0.0015
                ),
                reward_clip=float(
                    OPTIONS.get("offline_rl_reward_clip", 1.0) or 1.0
                ),
                advantage_clip=float(
                    OPTIONS.get("offline_rl_advantage_clip", 1.5) or 1.5
                ),
                kl_beta=float(
                    OPTIONS.get("offline_rl_kl_beta", 2.0) or 2.0
                ),
                parent_l2=float(
                    OPTIONS.get("offline_rl_parent_l2", 0.002) or 0.002
                ),
                manual_weight=float(
                    OPTIONS.get("offline_rl_manual_weight", 4.0) or 4.0
                ),
                gradient_clip=float(
                    OPTIONS.get("offline_rl_gradient_clip", 0.5) or 0.5
                ),
                max_parent_relative_l2=float(
                    OPTIONS.get(
                        "offline_rl_max_parent_relative_l2", 0.08
                    )
                    or 0.08
                ),
                min_action_support=int(
                    OPTIONS.get("offline_rl_min_action_support", 4) or 4
                ),
                early_stop_patience=int(
                    OPTIONS.get("offline_rl_early_stop_patience", 2) or 2
                ),
                early_stop_min_delta=float(
                    OPTIONS.get("offline_rl_early_stop_min_delta", 0.0001)
                    or 0.0001
                ),
                checkpoint=lambda name, force=False: TRAINING_BUDGET.checkpoint(
                    name,
                    force=force,
                    thread_name=_BUDGET_THREAD_NAME,
                ),
            )
            if not trainer.get("trained"):
                gate = {
                    "contract": "tiny_mlp_offline_rl_gate_v1",
                    "status": "insufficient_evidence",
                    "passed": False,
                    "reasons": [str(trainer.get("reason") or "offline_rl_not_trained")],
                    "samples_total": len(rows),
                    "train_samples": len(train_rows),
                    "holdout_samples": len(holdout_rows),
                    "online_exploration": False,
                    "physical_authority": False,
                }
            else:
                _set_candidate_work(
                    manager,
                    parent["id"],
                    state="active",
                    phase="offline_rl_gate",
                    progress=0.82,
                    policy_backend=TinyMLPBackend.BACKEND,
                )
                gate = offline_rl_gate(
                    source["backend"],
                    child_backend,
                    train_rows=train_rows,
                    holdout_rows=holdout_rows,
                    manual_samples=manual,
                    min_total_samples=min_total,
                    min_holdout_samples=min_holdout,
                    min_supported_actions=int(
                        OPTIONS.get("offline_rl_min_supported_actions", 2)
                        or 2
                    ),
                    min_action_support=int(
                        OPTIONS.get("offline_rl_min_action_support", 4)
                        or 4
                    ),
                    min_effective_sample_size=float(
                        OPTIONS.get(
                            "offline_rl_min_effective_sample_size", 4.0
                        )
                        or 4.0
                    ),
                    min_reward_gain=float(
                        OPTIONS.get("offline_rl_min_reward_gain", 0.0)
                        or 0.0
                    ),
                    min_parent_agreement=float(
                        OPTIONS.get(
                            "offline_rl_min_parent_agreement", 0.80
                        )
                        or 0.80
                    ),
                    max_mean_tv=float(
                        OPTIONS.get("offline_rl_max_mean_tv", 0.10)
                        or 0.10
                    ),
                    max_max_tv=float(
                        OPTIONS.get("offline_rl_max_max_tv", 0.25)
                        or 0.25
                    ),
                    max_parent_relative_l2=float(
                        OPTIONS.get(
                            "offline_rl_max_parent_relative_l2", 0.08
                        )
                        or 0.08
                    ),
                    max_unsupported_probability_lift=float(
                        OPTIONS.get(
                            "offline_rl_max_unsupported_probability_lift",
                            0.02,
                        )
                        or 0.02
                    ),
                    max_regression_fraction=float(
                        OPTIONS.get(
                            "offline_rl_max_regression_fraction", 0.10
                        )
                        or 0.10
                    ),
                    max_unseen_context_rate=float(
                        OPTIONS.get(
                            "offline_rl_max_unseen_context_rate", 0.75
                        )
                        or 0.75
                    ),
                    reward_clip=float(
                        OPTIONS.get("offline_rl_reward_clip", 1.0) or 1.0
                    ),
                    context_threshold=float(
                        OPTIONS.get(
                            "offline_rl_context_distance_threshold", 1.5
                        )
                        or 1.5
                    ),
                )

            fresh = manager._candidate_row(parent["id"]) or row
            if (
                int(fresh.get("feedback_revision") or 0) > build_revision
                or int(fresh.get("dirty") or 0)
            ):
                with manager.store.lock, manager.store.conn() as c:
                    c.execute(
                        """UPDATE agent_candidates
                           SET state='queued',dirty=1,updated_ts=?
                           WHERE parent_agent_id=? AND candidate_id=?""",
                        (
                            time.time(),
                            str(parent["id"]),
                            str(candidate["id"]),
                        ),
                    )
                _finish_run(
                    manager,
                    run_id,
                    "stale_requeued",
                    trainer=trainer,
                    gate=gate,
                )
                manager.wake_event.set()
                return True

            if not _current_parent_source_still_matches(
                manager,
                parent,
                source["checksum"],
                source["backend"].model_revision,
            ):
                with manager.store.lock, manager.store.conn() as c:
                    c.execute(
                        """UPDATE agent_candidates
                           SET state='queued',dirty=1,updated_ts=?
                           WHERE parent_agent_id=? AND candidate_id=?""",
                        (
                            time.time(),
                            str(parent["id"]),
                            str(candidate["id"]),
                        ),
                    )
                _finish_run(
                    manager,
                    run_id,
                    "parent_changed_requeued",
                    trainer=trainer,
                    gate=gate,
                )
                manager.wake_event.set()
                return True

            candidate_policy = _policy_for(manager, candidate)
            holdout = dict(gate.get("holdout") or {})
            tournament = {
                "contract": "offline_rl_parent_vs_candidate_logged_reward_v1",
                "passed": bool(gate.get("passed")),
                "selected_backend": (
                    TinyMLPBackend.BACKEND
                    if gate.get("passed")
                    else "diagonal_linucb"
                ),
                "reason": (
                    "conservative_offline_rl_gate_passed"
                    if gate.get("passed")
                    else "conservative_offline_rl_gate_blocked"
                ),
                "samples": int(gate.get("holdout_samples") or 0),
                "mlp_score": holdout.get("candidate_reward_proxy"),
                "ridge_score": None,
                "parent_reward_proxy": holdout.get("parent_reward_proxy"),
                "reward_improvement_estimate": holdout.get(
                    "reward_improvement_estimate"
                ),
                "parent_action_agreement": holdout.get(
                    "parent_action_agreement"
                ),
                "action_drift_mean_tv": holdout.get(
                    "action_drift_mean_tv"
                ),
                "behavior_propensity_known": False,
                "automatic_physical_switch": False,
                "online_exploration": False,
            }
            artifact = build_training_artifact(
                agent=candidate,
                policy=candidate_policy,
                mask=source["mask"],
                backend=child_backend,
                trainer={
                    "contract": "tiny_mlp_conservative_offline_policy_improvement_v1",
                    "offline_rl": trainer,
                    "run_id": run_id,
                    "trusted_total": trusted_total,
                    "compatible_total": len(rows),
                    "rejected": rejected,
                },
                tournament=tournament,
            )
            publish_training_artifact(manager.store, artifact)
            published = True
            service = getattr(manager.engine, "tiny_mlp_shadow", None)
            if service is not None and callable(
                getattr(service, "invalidate", None)
            ):
                service.invalidate(candidate["id"])

            _persist_gate(manager, fresh, gate)
            _finish_run(
                manager,
                run_id,
                "passed" if gate.get("passed") else "blocked",
                trainer=trainer,
                gate=gate,
            )
            _set_candidate_work(
                manager,
                parent["id"],
                state="done",
                phase="complete",
                progress=1.0,
                finished_ts=time.time(),
                labels_done=len(train_rows),
                labels_total=len(train_rows),
                replay_samples=len(train_rows),
                holdout_samples=len(holdout_rows),
                policy_backend=TinyMLPBackend.BACKEND,
            )
            manager.store.event(
                parent["id"],
                "info" if gate.get("passed") else "warning",
                "candidate_offline_rl_complete",
                (
                    "Offline RL Candidate passed its conservative gate and entered Shadow"
                    if gate.get("passed")
                    else "Offline RL Candidate was blocked by the conservative offline gate"
                ),
                {
                    "run_id": run_id,
                    "candidate_id": candidate["id"],
                    "offline_gate": gate,
                    "policy_updated_online": False,
                    "online_exploration": False,
                },
            )
            try:
                child_generation = lineage._row(
                    manager.store, agent_id=str(candidate["id"])
                )
                if child_generation:
                    lineage._refresh_generation_metadata(
                        manager.store,
                        candidate["id"],
                        lifecycle_state=(
                            "comparing"
                            if gate.get("passed")
                            else (
                                "insufficient_evidence"
                                if gate.get("status") == "insufficient_evidence"
                                else "offline_blocked"
                            )
                        ),
                    )
            except Exception:
                pass
            return True
        except Exception as exc:
            if published:
                restore_training_record(
                    manager.store,
                    candidate["id"],
                    previous_child_record,
                )
            if run_id:
                _finish_run(
                    manager,
                    run_id,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            _set_candidate_work(
                manager,
                parent["id"],
                state="failed",
                phase="failed",
                progress=0.0,
                error=f"{type(exc).__name__}: {exc}",
                finished_ts=time.time(),
                policy_backend=TinyMLPBackend.BACKEND,
            )
            manager._fail(row, f"{type(exc).__name__}: {exc}")
            return True
        finally:
            TRAINING_BUDGET.end()
            HEAVY_JOBS.release(owner)

    def enrich(result):
        if not isinstance(result, dict):
            return result
        candidate_id = result.get("candidate_id")
        if not candidate_id:
            return result
        run = _latest_run(manager, candidate_id)
        if run:
            result["offline_rl"] = run
            gate = dict(run.get("gate") or {})
            comparison = dict(gate.get("comparison") or {})
            result["offline_rl_candidate"] = dict(
                comparison.get("offline_rl_candidate") or {}
            )
            result["offline_rl_parent"] = dict(
                comparison.get("parent") or {}
            )
            result["offline_rl_online_exploration"] = False
            result["offline_rl_physical_authority"] = False
        return result

    def status(parent_id):
        return enrich(original_status(parent_id))

    def list_status(*args, **kwargs):
        return [
            enrich(dict(row))
            for row in original_list_status(*args, **kwargs)
        ]

    manager._start_build = start_build
    manager.status = status
    manager.list_status = list_status
    manager._candidate_offline_rl_installed = True
    manager.candidate_offline_rl_contract = (
        "trusted_stage6_observations_only_parent_kl_manual_anchor_"
        "offline_gate_then_shadow_no_online_exploration"
    )
    manager.offline_rl_readiness = lambda generation, agent: readiness(
        manager, generation, agent
    )
    return manager


def workflow_offline_rl(manager, ref):
    from agent_workflow_actions import (
        _create_or_coalesce_child,
        _resolve_generation,
    )

    generation, agent = _resolve_generation(manager, ref)
    info = readiness(manager, generation, agent)
    if not info.get("ready"):
        raise ValueError(
            "Offline RL is not ready: "
            + str(info.get("reason") or "insufficient trusted evidence")
        )
    result = _create_or_coalesce_child(
        manager,
        generation,
        REASON,
        "offline_rl",
        allow_coalesce=False,
    )
    result["offline_rl_readiness"] = info
    return result


def register_routes(router, core, manager):
    if getattr(manager, "_candidate_offline_rl_routes", False):
        return router

    def run(http, params):
        try:
            result = workflow_offline_rl(
                manager, params["generation_ref"]
            )
            return http.send_json(202, result)
        except ValueError as exc:
            return http.send_json(409, {"error": str(exc)})
        except Exception as exc:
            return http.send_json(
                500,
                {"error": f"{type(exc).__name__}: {exc}"},
            )

    def status(http, params):
        from agent_workflow_actions import _resolve_generation

        try:
            generation, agent = _resolve_generation(
                manager, params["generation_ref"]
            )
            return http.send_json(
                200, readiness(manager, generation, agent)
            )
        except ValueError as exc:
            return http.send_json(404, {"error": str(exc)})

    router.register(
        "POST",
        "offline_rl.run",
        r"^/api/agent-workflow/(?P<generation_ref>[^/]+)/offline-rl$",
        run,
        require_trusted=True,
        require_runtime=True,
        priority=125,
    )
    router.register(
        "GET",
        "offline_rl.status",
        r"^/api/agent-workflow/(?P<generation_ref>[^/]+)/offline-rl-status$",
        status,
        require_trusted=True,
        require_runtime=True,
        priority=125,
    )
    manager._candidate_offline_rl_routes = True
    return router
