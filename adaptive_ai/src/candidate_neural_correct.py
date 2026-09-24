"""Stage-5 generation-aware Manual Correct for trained Tiny MLP Candidates.

The existing chart/label UX stays authoritative.  This layer only changes the Candidate
build backend when the *selected direct parent generation* is already a qualified Tiny
MLP Candidate.  Ridge/current-backend Correct remains on the established conservative
path.

Neural path:
  exact parent neural artifact
  -> exact as-of observations for selected Correct timestamps
  -> bounded correction+historical replay fine-tune
  -> unaffected holdout / nearby-context / parent-distance gate
  -> Candidate Shadow only

Structural incompatibility escalates explicitly to the existing Full Rebuild path.  The
module never creates ActionIntent, invokes Executor, or calls Home Assistant services.
"""
from __future__ import annotations

import json
import math
import time
import uuid

import agent_candidate_conservative_correct as conservative
import agent_candidate_lineage as lineage
from observation_space import ObservationMask, observation_as_of
from context import controllable_context_exclusions, electrical_context_exclusions
from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_correct import (
    incremental_correct_finetune,
    nearest_action,
)
from policy_tiny_mlp_training import build_training_artifact
from replay import SQLiteTemporalTracker
from settings import OPTIONS
from telemetry import HEAVY_JOBS
from tiny_mlp_shadow import (
    load_training_record,
    publish_training_artifact,
    restore_training_record,
)
from training_budget import TRAINING_BUDGET


_BUDGET_THREAD_NAME = "adaptive-ai-index-candidate-neural-correct"
_PATH_COLUMN = "correct_path_json"


class StructuralRebuildRequired(RuntimeError):
    def __init__(self, reason, detail=None):
        super().__init__(detail or reason)
        self.reason = str(reason)
        self.detail = str(detail or reason)


def _json(raw, default=None):
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _ensure_schema(store):
    with store.lock, store.conn() as c:
        columns = {
            str(row[1])
            for row in c.execute("PRAGMA table_info(agent_candidates)").fetchall()
        }
        if _PATH_COLUMN not in columns:
            c.execute(
                "ALTER TABLE agent_candidates "
                "ADD COLUMN correct_path_json TEXT NOT NULL DEFAULT '{}'"
            )
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tiny_mlp_correct_batches (
                batch_id TEXT PRIMARY KEY,
                root_agent_id TEXT,
                parent_generation_id TEXT,
                parent_agent_id TEXT NOT NULL,
                child_generation_id TEXT,
                candidate_id TEXT NOT NULL,
                created_ts REAL NOT NULL,
                finished_ts REAL,
                source_backend TEXT NOT NULL,
                source_model_revision TEXT,
                source_model_checksum TEXT,
                feature_schema_id TEXT,
                feature_mask_id TEXT,
                path TEXT NOT NULL,
                rebuild_reason TEXT,
                status TEXT NOT NULL,
                report_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_tiny_mlp_correct_parent
                ON tiny_mlp_correct_batches(parent_agent_id,created_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_tiny_mlp_correct_candidate
                ON tiny_mlp_correct_batches(candidate_id,created_ts DESC);

            CREATE TABLE IF NOT EXISTS tiny_mlp_correct_samples (
                batch_id TEXT NOT NULL,
                label_id INTEGER,
                sample_ts REAL NOT NULL,
                original_decision REAL,
                desired REAL NOT NULL,
                observation_ts REAL,
                observation_json TEXT,
                usable INTEGER NOT NULL,
                unusable_reason TEXT,
                PRIMARY KEY(batch_id,label_id,sample_ts)
            );
            """
        )


def _set_path(manager, row, **fields):
    payload = {
        "contract": "manual_correct_path_v1",
        "updated_ts": time.time(),
        **fields,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            f"UPDATE agent_candidates SET {_PATH_COLUMN}=?,updated_ts=? "
            "WHERE parent_agent_id=? AND candidate_id=?",
            (
                encoded,
                time.time(),
                str(row["parent_agent_id"]),
                str(row["candidate_id"]),
            ),
        )
    return payload


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


def _source_neural_record(manager, parent_id):
    """Return a neural source only when the selected parent generation truly ran MLP."""
    generation = lineage._row(manager.store, agent_id=str(parent_id))
    if not generation or generation.get("generation_type") != "candidate":
        return None
    record = load_training_record(manager.store, parent_id)
    if not record:
        return None
    if record.get("selected_backend") != TinyMLPBackend.BACKEND:
        return None
    if not bool((record.get("model") or {}).get("trained")):
        return None
    if not bool((record.get("tournament") or {}).get("passed")):
        return None
    edge = _candidate_edge_by_child(manager, parent_id)
    gate = _json((edge or {}).get("offline_gate_json"), {})
    if not bool(gate.get("passed")):
        return None
    return generation, record


def _source_policy(manager, agent):
    manager.engine.models.pop(str(agent["id"]), None)
    return manager.engine.policy(agent)


def _validate_source(manager, parent, record):
    mask_raw = dict(record.get("mask") or {})
    model_raw = dict(record.get("model") or {})
    try:
        mask = ObservationMask.from_export(mask_raw)
    except Exception as exc:
        raise StructuralRebuildRequired("feature_mask_incompatible", str(exc))

    registry = manager.engine.context.resolved_registry()
    state_map = manager.engine.state_map
    excluded_control, _ = controllable_context_exclusions(state_map, registry)
    excluded_electrical, _ = electrical_context_exclusions(state_map, registry)
    forbidden = sorted(
        set(mask.selected_entities)
        & (set(excluded_control) | set(excluded_electrical))
    )
    if forbidden:
        raise StructuralRebuildRequired(
            "feature_mask_changed",
            "Persisted neural mask now contains hard-excluded entities: "
            + ",".join(forbidden[:8]),
        )

    policy = _source_policy(manager, parent)
    source_revision = str(
        getattr(policy, "tournament_revision", None)
        or getattr(policy, "model_revision", None)
        or "unknown"
    )
    if str(record.get("source_policy_revision") or "unknown") != source_revision:
        raise StructuralRebuildRequired(
            "source_policy_revision_changed",
            "Neural artifact no longer matches the selected generation policy revision",
        )
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
        raise StructuralRebuildRequired("neural_model_incompatible", str(exc))
    if not backend.trained:
        raise StructuralRebuildRequired(
            "neural_model_untrained",
            "Selected neural source has no trained parameters",
        )
    return policy, mask, backend


def _correct_operation(manager, source_generation, row):
    """Resolve the newest durable Correct operation for this exact parent->child edge."""
    child_generation = lineage._row(
        manager.store, agent_id=str(row["candidate_id"])
    )
    child_generation_id = str((child_generation or {}).get("generation_id") or "")
    try:
        with manager.store.conn() as c:
            rows = [
                dict(item) for item in c.execute(
                    """SELECT * FROM agent_correct_operations
                       WHERE parent_generation_id=?
                         AND status IN ('prepared','committed')
                       ORDER BY created_ts DESC LIMIT 8""",
                    (str(source_generation["generation_id"]),),
                ).fetchall()
            ]
    except Exception:
        return None
    for operation in rows:
        bound_child = str(operation.get("child_generation_id") or "")
        if bound_child and child_generation_id and bound_child != child_generation_id:
            continue
        try:
            label_ids = [
                int(value)
                for value in json.loads(operation.get("label_ids_json") or "[]")
            ]
        except Exception:
            label_ids = []
        if not label_ids:
            continue
        operation["label_ids"] = label_ids
        return operation
    return None


def _experience_rows(store, agent_id, correction_times):
    train_limit = max(
        0, int(OPTIONS.get("tiny_mlp_correct_replay_samples", 192) or 192)
    )
    holdout_limit = max(
        0, int(OPTIONS.get("tiny_mlp_correct_holdout_samples", 64) or 64)
    )
    exclusion = max(
        0.0,
        float(OPTIONS.get("tiny_mlp_correct_holdout_exclusion_seconds", 90) or 90),
    )
    fetch_limit = max(32, train_limit + holdout_limit + 128)
    with store.conn() as c:
        rows = [
            dict(row)
            for row in c.execute(
                """
                SELECT h.id,h.action_index,h.action_value,e.ts
                FROM historical_experiences h
                JOIN entity_history e ON e.id=h.target_history_id
                WHERE h.agent_id=? AND e.ts IS NOT NULL
                ORDER BY h.id DESC LIMIT ?
                """,
                (str(agent_id), fetch_limit),
            ).fetchall()
        ]
    rows.reverse()
    correction_times = [float(x) for x in correction_times]
    clean = []
    for row in rows:
        try:
            ts = float(row["ts"])
            action_idx = int(row["action_index"])
        except (TypeError, ValueError, KeyError):
            continue
        if any(abs(ts - selected) <= exclusion for selected in correction_times):
            continue
        clean.append({
            "timestamp": ts,
            "action_idx": action_idx,
            "source": "historical_replay",
            "history_id": row.get("id"),
        })

    holdout = clean[-holdout_limit:] if holdout_limit else []
    before_holdout = clean[:-len(holdout)] if holdout else clean
    replay = before_holdout[-train_limit:] if train_limit else []
    return replay, holdout


def _reconstruct_dataset(
    manager, parent, candidate, mask, labels, actions, *, current_label_ids=None
):
    labels = list(labels or ())
    correction_times = [
        float(row["sample_ts"])
        for row in labels
        if row.get("sample_ts") is not None
    ]
    current_ids = (
        {int(value) for value in current_label_ids}
        if current_label_ids is not None
        else {int(row["id"]) for row in labels if row.get("id") is not None}
    )
    replay_specs, holdout_specs = _experience_rows(
        manager.store, candidate["id"], correction_times
    )

    nearby_seconds = str(
        OPTIONS.get("tiny_mlp_correct_nearby_seconds", "10,30") or "10,30"
    )
    offsets = []
    for token in nearby_seconds.replace(";", ",").split(","):
        try:
            value = abs(float(token.strip()))
        except (TypeError, ValueError):
            continue
        if 0.0 < value <= 300.0:
            offsets.extend((-value, value))

    requests = []
    for label in labels:
        try:
            ts = float(label["sample_ts"])
            desired = float(label["desired"])
        except (KeyError, TypeError, ValueError):
            continue
        requests.append((
            "correct_current" if int(label.get("id") or -1) in current_ids else "correct_prior",
            ts,
            label,
        ))
        for offset in offsets:
            if ts + offset > 0:
                requests.append(("nearby", ts + offset, {"source_ts": ts}))
    for row in replay_specs:
        requests.append(("replay", float(row["timestamp"]), row))
    for row in holdout_specs:
        requests.append(("holdout", float(row["timestamp"]), row))

    if not requests:
        return {
            "corrections": [],
            "replay": [],
            "holdout": [],
            "nearby": [],
            "sample_audit": [],
            "tracker": {},
        }

    timestamps = [item[1] for item in requests]
    tracker = SQLiteTemporalTracker(
        manager.store,
        mask.selected_entities,
        manager.engine.context,
        min(timestamps),
        max(timestamps),
        context_cache_contract="tiny_mlp_manual_correct_v1:" + str(mask.mask_id),
    )
    corrections = []
    replay = []
    prior_correct_replay = []
    holdout = []
    nearby = []
    sample_audit = []
    try:
        for kind, timestamp, source in sorted(requests, key=lambda item: (item[1], item[0])):
            TRAINING_BUDGET.checkpoint(
                "tiny_mlp_correct_reconstruct",
                thread_name=_BUDGET_THREAD_NAME,
            )
            try:
                observation = observation_as_of(
                    mask,
                    {},
                    tracker,
                    float(timestamp),
                    parent,
                )
            except Exception as exc:
                if kind == "correct_current":
                    sample_audit.append({
                        "label_id": source.get("id"),
                        "sample_ts": float(timestamp),
                        "original_decision": source.get("previous_desired"),
                        "desired": source.get("desired"),
                        "observation": None,
                        "usable": False,
                        "unusable_reason": "reconstruction_failed:" + str(exc)[:240],
                    })
                continue

            missing = int(observation.get("missing_feature_count") or 0)
            # Missingness is explicit in the Stage-2 observation contract, but an exact
            # human Correct must never invent values for a source model feature. Replay
            # rows may be skipped; selected Correct points are retained as unusable audit.
            if missing:
                if kind == "correct_current":
                    sample_audit.append({
                        "label_id": source.get("id"),
                        "sample_ts": float(timestamp),
                        "original_decision": source.get("previous_desired"),
                        "desired": source.get("desired"),
                        "observation": observation,
                        "usable": False,
                        "unusable_reason": f"missing_source_features:{missing}",
                    })
                continue

            if kind in ("correct_current", "correct_prior"):
                desired = float(source["desired"])
                item = {
                    "label_id": source.get("id"),
                    "timestamp": float(timestamp),
                    "observation": observation,
                    "action_idx": nearest_action(actions, desired),
                    "weight": 1.0,
                    "source": "manual_correct",
                }
                if kind == "correct_current":
                    corrections.append(item)
                    sample_audit.append({
                        "label_id": source.get("id"),
                        "sample_ts": float(timestamp),
                        "original_decision": source.get("previous_desired"),
                        "desired": desired,
                        "observation": observation,
                        "usable": True,
                        "unusable_reason": None,
                    })
                else:
                    item["source"] = "prior_manual_correct_replay"
                    prior_correct_replay.append(item)
            elif kind == "replay":
                item = dict(source)
                item["observation"] = observation
                item["weight"] = 1.0
                replay.append(item)
            elif kind == "holdout":
                item = dict(source)
                item["observation"] = observation
                item["weight"] = 1.0
                holdout.append(item)
            elif kind == "nearby":
                nearby.append(observation)
    finally:
        tracker_stats = tracker.stats()
        tracker.close()

    return {
        "corrections": corrections,
        # mixed_training_rows keeps the newest/tail replay entries under its cap.
        # Place prior explicit Correct at the tail so bounded history can never evict
        # stronger human supervision before ordinary historical replay.
        "replay": replay + prior_correct_replay,
        "prior_correct_replay": prior_correct_replay,
        "historical_replay": replay,
        "holdout": holdout,
        "nearby": nearby,
        "sample_audit": sample_audit,
        "tracker": tracker_stats,
    }


def _create_batch(manager, row, source_generation, backend, mask, *, batch_id=None):
    child_generation = lineage._row(
        manager.store, agent_id=str(row["candidate_id"])
    )
    batch_id = str(batch_id or uuid.uuid4())
    now = time.time()
    raw = backend.serialize()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """
            INSERT INTO tiny_mlp_correct_batches
                (batch_id,root_agent_id,parent_generation_id,parent_agent_id,
                 child_generation_id,candidate_id,created_ts,source_backend,
                 source_model_revision,source_model_checksum,feature_schema_id,
                 feature_mask_id,path,status,finished_ts,report_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,? ,NULL,'{}')
            ON CONFLICT(batch_id) DO UPDATE SET
                root_agent_id=excluded.root_agent_id,
                parent_generation_id=excluded.parent_generation_id,
                parent_agent_id=excluded.parent_agent_id,
                child_generation_id=excluded.child_generation_id,
                candidate_id=excluded.candidate_id,
                source_backend=excluded.source_backend,
                source_model_revision=excluded.source_model_revision,
                source_model_checksum=excluded.source_model_checksum,
                feature_schema_id=excluded.feature_schema_id,
                feature_mask_id=excluded.feature_mask_id,
                path=excluded.path,
                status='running',
                finished_ts=NULL,
                report_json='{}'
            """,
            (
                batch_id,
                str(source_generation.get("root_agent_id") or ""),
                str(source_generation.get("generation_id") or ""),
                str(row["parent_agent_id"]),
                str((child_generation or {}).get("generation_id") or ""),
                str(row["candidate_id"]),
                now,
                TinyMLPBackend.BACKEND,
                str(backend.model_revision),
                str(raw.get("model_checksum") or ""),
                str(mask.schema_id),
                str(mask.mask_id),
                "incremental_fine_tune",
                "running",
            ),
        )
        c.execute(
            "DELETE FROM tiny_mlp_correct_samples WHERE batch_id=?",
            (batch_id,),
        )
    return batch_id


def _save_sample_audit(manager, batch_id, rows):
    with manager.store.lock, manager.store.conn() as c:
        for row in rows:
            c.execute(
                """
                INSERT OR REPLACE INTO tiny_mlp_correct_samples
                    (batch_id,label_id,sample_ts,original_decision,desired,
                     observation_ts,observation_json,usable,unusable_reason)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(batch_id),
                    row.get("label_id"),
                    float(row.get("sample_ts") or 0.0),
                    row.get("original_decision"),
                    float(row.get("desired") or 0.0),
                    (
                        float((row.get("observation") or {}).get("timestamp"))
                        if (row.get("observation") or {}).get("timestamp") is not None
                        else None
                    ),
                    (
                        json.dumps(
                            row.get("observation"),
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        if row.get("observation") is not None
                        else None
                    ),
                    1 if row.get("usable") else 0,
                    row.get("unusable_reason"),
                ),
            )


def _finish_batch(manager, batch_id, status, report):
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """
            UPDATE tiny_mlp_correct_batches
            SET finished_ts=?,status=?,report_json=? WHERE batch_id=?
            """,
            (
                time.time(),
                str(status),
                json.dumps(report or {}, sort_keys=True, separators=(",", ":"), allow_nan=False),
                str(batch_id),
            ),
        )


def _escalate_rebuild(manager, row, reason, detail, original_start):
    path = _set_path(
        manager,
        row,
        path="rebuild",
        rebuild_reason=str(reason),
        detail=str(detail)[:400],
        source_backend=TinyMLPBackend.BACKEND,
    )
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """
            UPDATE agent_candidates
            SET reason='schema_upgrade_rebuild',dirty=1,queued_ts=?,updated_ts=?
            WHERE parent_agent_id=? AND candidate_id=?
            """,
            (now, now, str(row["parent_agent_id"]), str(row["candidate_id"])),
        )
    manager.store.event(
        row["parent_agent_id"],
        "warning",
        "tiny_mlp_correct_rebuild_required",
        "Neural Correct escalated to full Rebuild because the source model structure is incompatible",
        {
            "candidate_id": row["candidate_id"],
            "rebuild_reason": str(reason),
            "detail": str(detail)[:400],
            "correct_path": path,
        },
    )
    fresh = manager._candidate_row(row["parent_agent_id"]) or {
        **row,
        "reason": "schema_upgrade_rebuild",
    }
    return original_start(fresh)


def _gate_payload(report, dataset, batch_id):
    before = report.get("correction_fit_before") or {}
    after = report.get("correction_fit_after") or {}
    holdout = report.get("unaffected_holdout") or {}
    parent_eval = holdout.get("parent") or {}
    child_eval = holdout.get("child") or {}
    return {
        "contract": "tiny_mlp_manual_correct_offline_gate_v1",
        "status": (
            "passed"
            if report.get("passed")
            else (
                "insufficient_evidence"
                if report.get("status") == "insufficient_evidence"
                else "regression_failed"
            )
        ),
        "passed": bool(report.get("passed")),
        "reasons": list(report.get("reasons") or ()),
        "correct_path": "incremental_fine_tune",
        "policy_backend": TinyMLPBackend.BACKEND,
        "batch_id": str(batch_id),
        "teach_fit_before": before.get("score"),
        "teach_fit_after": after.get("score"),
        "teach_fit_before_count": before.get("correct"),
        "teach_fit_after_count": after.get("correct"),
        "teach_fit_total": after.get("samples"),
        "historical_parent_score": parent_eval.get("score"),
        "historical_candidate_score": child_eval.get("score"),
        "regression_delta": holdout.get("score_delta"),
        "benchmark_samples": holdout.get("samples"),
        "parent_child_agreement": holdout.get("parent_child_agreement"),
        "regression_count": holdout.get("regression_count"),
        "regression_fraction": holdout.get("regression_fraction"),
        "nearby_context_agreement": (report.get("nearby_context") or {}).get("agreement"),
        "parent_relative_l2": (report.get("parent_distance") or {}).get("relative_l2"),
        "correction_samples_usable": len(dataset.get("corrections") or ()),
        "correction_samples_unusable": sum(
            1 for row in dataset.get("sample_audit") or () if not row.get("usable")
        ),
        "replay_training_samples": len(dataset.get("replay") or ()),
        "prior_correct_replay_samples": len(dataset.get("prior_correct_replay") or ()),
        "tracker": dataset.get("tracker") or {},
        "automatic_physical_switch": False,
        "physical_authority": False,
        "online_reward_updates": False,
    }


def install(manager):
    if getattr(manager, "_candidate_neural_correct_installed", False):
        return manager

    _ensure_schema(manager.store)
    # Candidate workers are started by the base runtime before the final HTTP workflow
    # layer is attached. Materialize the generic Correct-operation table now so restart
    # recovery cannot race schema creation.
    from agent_workflow_actions import ensure_workflow_tables
    ensure_workflow_tables(manager.store)
    original_start = manager._start_build
    original_status = manager.status
    original_list_status = manager.list_status

    def start_build(row):
        reason = str(row.get("reason") or "feedback")
        if reason not in conservative._CORRECT_REASONS:
            return original_start(row)

        source = _source_neural_record(manager, row["parent_agent_id"])
        if source is None:
            return original_start(row)
        source_generation, source_record = source
        parent = manager.store.get_agent_config(str(row["parent_agent_id"]))
        candidate = manager.store.get_agent_config(str(row["candidate_id"]))
        if not parent or not candidate:
            manager._fail(row, "candidate or neural parent generation disappeared")
            return True

        try:
            parent_policy, mask, parent_backend = _validate_source(
                manager, parent, source_record
            )
        except StructuralRebuildRequired as exc:
            return _escalate_rebuild(
                manager, row, exc.reason, exc.detail, original_start
            )

        owner = "candidate_neural_correct"
        if not HEAVY_JOBS.acquire(owner):
            conservative._set_candidate_work(
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
        build_revision = int(row.get("feedback_revision") or 0)
        previous_child_record = load_training_record(manager.store, candidate["id"])
        batch_id = None
        published = False
        try:
            now = time.time()
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """
                    UPDATE agent_candidates
                    SET state='building',build_revision=?,dirty=0,
                        build_started_ts=?,build_finished_ts=NULL,
                        comparison_started_ts=NULL,comparison_json='{}',
                        offline_gate_json='{}',last_error=NULL,updated_ts=?
                    WHERE parent_agent_id=? AND candidate_id=?
                    """,
                    (
                        build_revision,
                        now,
                        now,
                        str(row["parent_agent_id"]),
                        str(row["candidate_id"]),
                    ),
                )
            _set_path(
                manager,
                row,
                path="incremental_fine_tune",
                rebuild_reason=None,
                source_backend=TinyMLPBackend.BACKEND,
                source_generation_id=source_generation.get("generation_id"),
                source_model_revision=parent_backend.model_revision,
                source_model_checksum=parent_backend.serialize().get("model_checksum"),
            )
            conservative._set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="neural_snapshot",
                progress=0.05,
                started_ts=now,
                blocked_by=None,
                policy_backend=TinyMLPBackend.BACKEND,
            )
            manager.store.event(
                parent["id"],
                "info",
                "tiny_mlp_correct_started",
                "Candidate Correct started incremental supervised Tiny MLP fine-tune",
                {
                    "candidate_id": candidate["id"],
                    "generation": row.get("generation"),
                    "source_generation_id": source_generation.get("generation_id"),
                    "build_revision": build_revision,
                    "correct_path": "incremental_fine_tune",
                },
            )

            # Ridge shadow snapshot remains available as lifecycle/fallback metadata, but
            # the neural child always starts from the exact selected neural parent model.
            candidate = conservative._copy_parent_snapshot(
                manager, parent["id"], candidate["id"]
            )
            # Keep the exact selected parent generation as the source of truth
            # for Manual Correct labels. _sync_feedback still mirrors them to the child
            # for the established Ridge/fallback lifecycle, but child label row IDs are
            # not used as provenance for the neural operation.
            labels = conservative._teach_rows(manager.engine.rl_teaching, parent)
            operation = _correct_operation(manager, source_generation, row)
            current_label_ids = (
                list(operation.get("label_ids") or ())
                if operation is not None
                else [int(label["id"]) for label in labels if label.get("id") is not None]
            )
            synced = manager._sync_feedback(parent, candidate)
            candidate = manager.store.get_agent_config(candidate["id"]) or candidate
            batch_id = _create_batch(
                manager,
                row,
                source_generation,
                parent_backend,
                mask,
                batch_id=(operation or {}).get("operation_id"),
            )

            conservative._set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="reconstructing_correct_context",
                progress=0.18,
                labels_total=len(labels),
                policy_backend=TinyMLPBackend.BACKEND,
            )
            dataset = _reconstruct_dataset(
                manager,
                parent,
                candidate,
                mask,
                labels,
                parent_backend.actions,
                current_label_ids=current_label_ids,
            )
            _save_sample_audit(
                manager, batch_id, dataset.get("sample_audit") or ()
            )
            if not dataset.get("corrections"):
                report = {
                    "contract": "tiny_mlp_manual_correct_incremental_v1",
                    "passed": False,
                    "status": "insufficient_evidence",
                    "reasons": ["no_usable_correct_observations"],
                    "correction_fit_before": {"samples": 0, "correct": 0, "score": None},
                    "correction_fit_after": {"samples": 0, "correct": 0, "score": None},
                    "unaffected_holdout": {
                        "samples": len(dataset.get("holdout") or ()),
                        "parent": {},
                        "child": {},
                    },
                    "parent_distance": {"relative_l2": 0.0},
                    "nearby_context": {"samples": len(dataset.get("nearby") or ()), "agreement": None},
                    "online_reward_updates": False,
                    "physical_authority": False,
                }
                gate = _gate_payload(report, dataset, batch_id)
                conservative._persist_gate(manager, row, gate)
                _finish_batch(manager, batch_id, "blocked", report)
                _set_path(
                    manager,
                    row,
                    path="incremental_fine_tune",
                    status="blocked",
                    batch_id=batch_id,
                    rebuild_reason=None,
                    source_backend=TinyMLPBackend.BACKEND,
                    reasons=report["reasons"],
                )
                return True

            conservative._set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="neural_fine_tuning",
                progress=0.48,
                labels_done=len(dataset["corrections"]),
                labels_total=len(labels),
                replay_samples=len(dataset.get("replay") or ()),
                holdout_samples=len(dataset.get("holdout") or ()),
                policy_backend=TinyMLPBackend.BACKEND,
            )

            child_backend, report = incremental_correct_finetune(
                parent_backend,
                agent=candidate,
                correction_samples=dataset["corrections"],
                replay_samples=dataset.get("replay") or (),
                holdout_samples=dataset.get("holdout") or (),
                nearby_observations=dataset.get("nearby") or (),
                correction_fraction=float(
                    OPTIONS.get("tiny_mlp_correct_fraction", 0.25) or 0.25
                ),
                max_samples=int(
                    OPTIONS.get("tiny_mlp_correct_max_samples", 256) or 256
                ),
                max_epochs=int(
                    OPTIONS.get("tiny_mlp_correct_max_epochs", 6) or 6
                ),
                batch_size=int(
                    OPTIONS.get("tiny_mlp_correct_batch_size", 16) or 16
                ),
                learning_rate=float(
                    OPTIONS.get("tiny_mlp_correct_learning_rate", 0.003) or 0.003
                ),
                l2=float(OPTIONS.get("tiny_mlp_correct_l2", 0.0001) or 0.0001),
                gradient_clip=float(
                    OPTIONS.get("tiny_mlp_correct_gradient_clip", 0.5) or 0.5
                ),
                early_stop_patience=int(
                    OPTIONS.get("tiny_mlp_correct_early_stop_patience", 2) or 2
                ),
                early_stop_min_delta=float(
                    OPTIONS.get("tiny_mlp_correct_early_stop_min_delta", 0.0005)
                    or 0.0005
                ),
                minimum_holdout_samples=int(
                    OPTIONS.get("tiny_mlp_correct_min_holdout_samples", 12) or 12
                ),
                max_accuracy_regression=float(
                    OPTIONS.get(
                        "tiny_mlp_correct_max_accuracy_regression",
                        OPTIONS.get("agent_candidate_max_accuracy_regression", 0.03),
                    )
                    or 0.03
                ),
                min_parent_agreement=float(
                    OPTIONS.get("tiny_mlp_correct_min_parent_agreement", 0.85) or 0.85
                ),
                min_nearby_agreement=float(
                    OPTIONS.get("tiny_mlp_correct_min_nearby_agreement", 0.75) or 0.75
                ),
                max_regression_fraction=float(
                    OPTIONS.get("tiny_mlp_correct_max_regression_fraction", 0.10)
                    or 0.10
                ),
                max_parent_relative_l2=float(
                    OPTIONS.get("tiny_mlp_correct_max_parent_relative_l2", 0.20)
                    or 0.20
                ),
                min_correction_fit=float(
                    OPTIONS.get("tiny_mlp_correct_min_correction_fit", 0.95) or 0.95
                ),
                checkpoint=lambda name, force=False: TRAINING_BUDGET.checkpoint(
                    name,
                    force=force,
                    thread_name=_BUDGET_THREAD_NAME,
                ),
            )

            fresh = manager._candidate_row(parent["id"]) or row
            if (
                int(fresh.get("feedback_revision") or 0) > build_revision
                or int(fresh.get("dirty") or 0)
            ):
                with manager.store.lock, manager.store.conn() as c:
                    c.execute(
                        """
                        UPDATE agent_candidates
                        SET state='queued',dirty=1,updated_ts=?
                        WHERE parent_agent_id=? AND candidate_id=?
                        """,
                        (
                            time.time(),
                            str(parent["id"]),
                            str(candidate["id"]),
                        ),
                    )
                _finish_batch(manager, batch_id, "stale_requeued", report)
                _set_path(
                    manager,
                    row,
                    path="incremental_fine_tune",
                    status="requeued",
                    batch_id=batch_id,
                    rebuild_reason=None,
                    source_backend=TinyMLPBackend.BACKEND,
                    reasons=["new_feedback_arrived"],
                )
                manager.wake_event.set()
                return True

            conservative._set_candidate_work(
                manager,
                parent["id"],
                state="active",
                phase="neural_offline_gate",
                progress=0.90,
                policy_backend=TinyMLPBackend.BACKEND,
            )
            gate = _gate_payload(report, dataset, batch_id)
            child_policy = _source_policy(manager, candidate)
            tournament = {
                "contract": "manual_correct_regression_gate_v1",
                "passed": bool(report.get("passed")),
                "selected_backend": (
                    TinyMLPBackend.BACKEND
                    if report.get("passed")
                    else "diagonal_linucb"
                ),
                "reason": (
                    "manual_correct_incremental_gate_passed"
                    if report.get("passed")
                    else "manual_correct_incremental_gate_blocked"
                ),
                "samples": int(
                    (report.get("unaffected_holdout") or {}).get("samples") or 0
                ),
                "mlp_score": (
                    (report.get("unaffected_holdout") or {}).get("child") or {}
                ).get("score"),
                "ridge_score": None,
                "source_parent_backend": TinyMLPBackend.BACKEND,
                "automatic_physical_switch": False,
            }
            artifact = build_training_artifact(
                agent=candidate,
                policy=child_policy,
                mask=mask,
                backend=child_backend,
                trainer={
                    "contract": "tiny_mlp_manual_correct_incremental_v1",
                    "manual_correct": report,
                    "batch_id": batch_id,
                    "dataset": {
                        "corrections": len(dataset.get("corrections") or ()),
                        "correct_operation_id": batch_id,
                        "current_operation_label_ids": list(current_label_ids),
                        "prior_correct_replay": len(dataset.get("prior_correct_replay") or ()),
                        "historical_replay": len(dataset.get("historical_replay") or ()),
                        "replay": len(dataset.get("replay") or ()),
                        "holdout": len(dataset.get("holdout") or ()),
                        "nearby": len(dataset.get("nearby") or ()),
                    },
                },
                tournament=tournament,
            )
            publish_training_artifact(manager.store, artifact)
            published = True
            service = getattr(manager.engine, "tiny_mlp_shadow", None)
            if service is not None and callable(getattr(service, "invalidate", None)):
                service.invalidate(candidate["id"])

            conservative._persist_gate(manager, fresh, gate)
            _finish_batch(
                manager,
                batch_id,
                "passed" if report.get("passed") else "blocked",
                report,
            )
            path = _set_path(
                manager,
                fresh,
                path="incremental_fine_tune",
                status="passed" if report.get("passed") else "blocked",
                batch_id=batch_id,
                rebuild_reason=None,
                source_backend=TinyMLPBackend.BACKEND,
                candidate_model_revision=child_backend.model_revision,
                candidate_model_checksum=child_backend.serialize().get("model_checksum"),
                reasons=list(report.get("reasons") or ()),
                parent_relative_l2=(report.get("parent_distance") or {}).get("relative_l2"),
                parent_child_agreement=(report.get("unaffected_holdout") or {}).get(
                    "parent_child_agreement"
                ),
            )
            conservative._set_candidate_work(
                manager,
                parent["id"],
                state="done",
                phase="complete",
                progress=1.0,
                finished_ts=time.time(),
                labels_done=len(dataset.get("corrections") or ()),
                labels_total=len(labels),
                policy_backend=TinyMLPBackend.BACKEND,
            )
            manager.store.event(
                parent["id"],
                "info" if report.get("passed") else "warning",
                "tiny_mlp_correct_complete",
                (
                    "Neural Correct produced a gated incremental Candidate for Shadow"
                    if report.get("passed")
                    else "Neural Correct Candidate was blocked by regression protection"
                ),
                {
                    "candidate_id": candidate["id"],
                    "batch_id": batch_id,
                    "feedback": synced,
                    "offline_gate": gate,
                    "correct_path": path,
                },
            )
            try:
                lineage._refresh_generation_metadata(
                    manager.store,
                    candidate["id"],
                    lifecycle_state=(
                        "comparing"
                        if report.get("passed")
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
        except StructuralRebuildRequired as exc:
            if batch_id:
                _finish_batch(
                    manager,
                    batch_id,
                    "rebuild_required",
                    {"reason": exc.reason, "detail": exc.detail},
                )
            if published:
                restore_training_record(
                    manager.store, candidate["id"], previous_child_record
                )
            return _escalate_rebuild(
                manager, row, exc.reason, exc.detail, original_start
            )
        except Exception as exc:
            if published:
                restore_training_record(
                    manager.store, candidate["id"], previous_child_record
                )
            if batch_id:
                _finish_batch(
                    manager,
                    batch_id,
                    "failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
            conservative._set_candidate_work(
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
        parent_id = result.get("parent_agent_id")
        row = None
        if candidate_id:
            row = _candidate_edge_by_child(manager, candidate_id)
        if row is None and parent_id:
            row = manager._candidate_row(parent_id)
        path = _json((row or {}).get(_PATH_COLUMN), {})
        if path:
            result["correct_learning"] = path
            result["correct_path"] = path.get("path")
            result["correct_rebuild_reason"] = path.get("rebuild_reason")
        return result

    def status(parent_id):
        return enrich(original_status(parent_id))

    def list_status(*args, **kwargs):
        return [enrich(dict(row)) for row in original_list_status(*args, **kwargs)]

    manager._start_build = start_build
    manager.status = status
    manager.list_status = list_status
    manager._candidate_neural_correct_installed = True
    manager.candidate_neural_correct_contract = (
        "selected_neural_parent_exact_asof_correct_batch_incremental_finetune_"
        "unaffected_holdout_nearby_parent_distance_gate_then_shadow"
    )
    manager.candidate_correct_contract = (
        str(getattr(manager, "candidate_correct_contract", "conservative_correct"))
        + "+tiny_mlp_incremental_manual_correct"
    )
    return manager
