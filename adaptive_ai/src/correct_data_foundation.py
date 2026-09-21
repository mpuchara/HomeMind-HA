"""Stage-1 Correct data foundation.

This module makes explicit Correct supervision durable and auditable without changing the
realtime event->intent path or the active feature schema.  It is installed after the
existing Candidate/Correct stack is composed.

Contracts:
* one stable supervision_event_id follows a logical Correct through Candidate lineage;
* active training rows are deduplicated by that id;
* every explicit Correct/Teach row captures one bounded historical broad-context snapshot;
* broad snapshots preserve semantic source roles and existing actuator/electrical exclusions;
* Candidate Correct reports residuals of the frozen schema, but this stage does not promote
  challengers or change policy math.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import time


CONTRACT_VERSION = 1
ROLE_LOCAL = "LOCAL_EVIDENCE"
ROLE_BOUNDARY = "BOUNDARY_ARRIVAL_PRECURSOR"
ROLE_TRAJECTORY = "TRAJECTORY_CONTEXT"
ROLE_RELIABILITY = "RELIABILITY_CONTEXT"
ROLE_OTHER = "OTHER_CONTEXT"
MAX_BROAD_ENTITIES = 256


def supervision_event_id(fingerprint_value, sample_ts, desired):
    """Generation-independent identity for one explicit supervision fact."""
    raw = "%s|%.6f|%.8f" % (
        str(fingerprint_value or ""), float(sample_ts), float(desired)
    )
    return "correct-v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _table_columns(conn, table):
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(table),)
    ).fetchone())


def _ensure_schema(store):
    # manual_context_learning owns the base table; extend it additively here.
    import manual_context_learning as manual_context
    manual_context._ensure_table(store)
    with store.lock, store.conn() as c:
        label_cols = _table_columns(c, "teaching_rl_labels")
        if "supervision_event_id" not in label_cols:
            c.execute("ALTER TABLE teaching_rl_labels ADD COLUMN supervision_event_id TEXT")
        context_cols = _table_columns(c, "manual_context_feedback")
        if "sample_ts" not in context_cols:
            c.execute("ALTER TABLE manual_context_feedback ADD COLUMN sample_ts REAL")
        if "supervision_event_id" not in context_cols:
            c.execute("ALTER TABLE manual_context_feedback ADD COLUMN supervision_event_id TEXT")
        if "metadata_json" not in context_cols:
            c.execute("ALTER TABLE manual_context_feedback ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        rows = c.execute(
            """SELECT id,fingerprint,sample_ts,desired FROM teaching_rl_labels
               WHERE supervision_event_id IS NULL OR supervision_event_id=''"""
        ).fetchall()
        c.executemany(
            "UPDATE teaching_rl_labels SET supervision_event_id=? WHERE id=?",
            [
                (
                    supervision_event_id(
                        row["fingerprint"], row["sample_ts"], row["desired"]
                    ),
                    int(row["id"]),
                )
                for row in rows
            ],
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_teaching_rl_supervision_event
               ON teaching_rl_labels(agent_id,supervision_event_id,id)"""
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_manual_context_supervision_event
               ON manual_context_feedback(agent_id,supervision_event_id,id)"""
        )


def deduplicate_supervision_rows(rows):
    """Return one active row per logical event; newest physical row wins."""
    latest = {}
    for raw in rows or ():
        row = dict(raw)
        if row.get("undone_ts") is not None:
            continue
        event_id = row.get("supervision_event_id") or supervision_event_id(
            row.get("fingerprint"), row.get("sample_ts"), row.get("desired")
        )
        row["supervision_event_id"] = event_id
        marker = (float(row.get("created_ts") or 0.0), int(row.get("id") or 0))
        previous = latest.get(event_id)
        if previous is None or marker >= previous[0]:
            latest[event_id] = (marker, row)
    return sorted(
        (item[1] for item in latest.values()),
        key=lambda row: (float(row.get("sample_ts") or 0.0), int(row.get("id") or 0)),
    )


def _backfill_agent_event_ids(store, agent_id):
    with store.lock, store.conn() as c:
        rows = c.execute(
            """SELECT id,fingerprint,sample_ts,desired FROM teaching_rl_labels
               WHERE agent_id=? AND (supervision_event_id IS NULL OR supervision_event_id='')""",
            (str(agent_id),),
        ).fetchall()
        c.executemany(
            "UPDATE teaching_rl_labels SET supervision_event_id=? WHERE id=?",
            [
                (
                    supervision_event_id(
                        row["fingerprint"], row["sample_ts"], row["desired"]
                    ),
                    int(row["id"]),
                )
                for row in rows
            ],
        )


def _normalize_text(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    return (
        "%s %s %s" % (
            str(entity_id or ""),
            str(attrs.get("friendly_name") or ""),
            str(attrs.get("device_class") or ""),
        )
    ).lower().replace("_", " ")


def semantic_source_role(engine, agent, entity_id, state, registry=None):
    """Assign a role without converting remote activity into local occupancy truth."""
    registry = registry or {}
    attrs = (state or {}).get("attributes") or {}
    text = _normalize_text(entity_id, state)
    device_class = str(attrs.get("device_class") or "").strip().lower()
    target = str(agent.get("target_entity") or "")
    context = getattr(engine, "context", None)
    target_area = context.area_for(target) if context is not None else None
    area = context.area_for(entity_id) if context is not None else None
    source_meta = context.evidence_metadata(entity_id) if context is not None else {}
    source_role = str((source_meta or {}).get("role") or "").lower()

    reliability_terms = (
        "humidity", "wilgot", "temperature", "temperatura", "dew point",
        "punkt rosy", "moisture",
    )
    if device_class in {"humidity", "temperature", "moisture"} or any(
        term in text for term in reliability_terms
    ):
        return ROLE_RELIABILITY

    reg = registry.get(entity_id) or {}
    boundary_for = (
        reg.get("boundary_for")
        or attrs.get("boundary_for")
        or attrs.get("arrival_precursor_for")
    )
    explicit_boundary = bool(
        boundary_for
        and str(boundary_for) in {str(target_area or ""), target}
    )
    door_like = (
        source_role in {"door", "opening", "boundary"}
        or device_class in {"door", "opening"}
        or any(term in text for term in (" door", "drzwi", "opening contact"))
    )
    if explicit_boundary or (door_like and target_area and area == target_area):
        return ROLE_BOUNDARY

    local_terms = (
        "presence", "occupancy", "motion", "pir", "radar",
        "stationary energy", "still energy", "moving energy", "move energy",
        "target energy", "target distance", "camera score", "aidetection",
        "ai detection", "detection score",
    )
    activity_like = (
        source_role in {"presence", "occupancy", "radar_activity", "motion"}
        or device_class in {"occupancy", "motion", "presence"}
        or any(term in text for term in local_terms)
    )
    if target_area and area == target_area and activity_like:
        return ROLE_LOCAL
    if target_area and area and area != target_area and activity_like:
        return ROLE_TRAJECTORY
    return ROLE_OTHER


def _eligible_entities(engine, agent):
    from context import (
        controllable_context_exclusions,
        electrical_context_exclusions,
        is_context_candidate_entity,
    )

    with engine.lock:
        states = dict(engine.state_map)
        registry = dict(engine.entity_registry)
    control, _ = controllable_context_exclusions(states, registry)
    electrical, _ = electrical_context_exclusions(states, registry)
    excluded = control | electrical | {str(agent.get("target_entity") or "")}
    candidates = []
    for entity_id, state in states.items():
        if entity_id in excluded:
            continue
        if not is_context_candidate_entity(entity_id, state, excluded):
            continue
        role = semantic_source_role(engine, agent, entity_id, state, registry)
        priority = {
            ROLE_LOCAL: 0,
            ROLE_BOUNDARY: 1,
            ROLE_RELIABILITY: 2,
            ROLE_TRAJECTORY: 3,
            ROLE_OTHER: 4,
        }.get(role, 5)
        candidates.append((priority, str(entity_id)))
    candidates.sort()
    limit = min(
        MAX_BROAD_ENTITIES,
        max(32, int(getattr(engine, "options", {}).get(
            "manual_context_observer_max_entities", MAX_BROAD_ENTITIES
        ))),
    )
    return [entity_id for _, entity_id in candidates[:limit]], states, registry


def _historical_states(store, entity_ids, sample_ts):
    """Indexed, bounded as-of lookup: at most two rows per candidate entity."""
    from context import archived_state

    out = {}
    with store.conn() as c:
        for entity_id in entity_ids:
            rows = c.execute(
                """SELECT * FROM entity_history
                   WHERE entity_id=? AND ts<=?
                   ORDER BY ts DESC,id DESC LIMIT 2""",
                (str(entity_id), float(sample_ts)),
            ).fetchall()
            if not rows:
                continue
            current_row = dict(rows[0])
            current = archived_state(current_row)
            current["entity_id"] = str(entity_id)
            current["_history_ts"] = float(current_row["ts"])
            previous = None
            if len(rows) > 1:
                previous_row = dict(rows[1])
                previous = archived_state(previous_row)
                previous["entity_id"] = str(entity_id)
                previous["_history_ts"] = float(previous_row["ts"])
            out[str(entity_id)] = (current, previous, current_row.get("source"))
    return out


def _historical_room_belief(engine, target_entity, historical_states, sample_ts):
    """Rebuild a fresh RoomBelief snapshot from the same bounded as-of sensor state."""
    try:
        from context_engine import ContextEngine

        states = {entity_id: pair[0] for entity_id, pair in historical_states.items()}
        with engine.context.lock:
            entities = dict(engine.context.entities)
            devices = list(engine.context.devices.values())
            areas = list(engine.context.areas.values())
            options = engine.context.options
        context = ContextEngine(options, store=None)
        context.configure(states, entities=entities, devices=devices, areas=areas)
        ordered = sorted(
            (
                (float(state.get("_history_ts") or sample_ts), entity_id, state)
                for entity_id, (state, _previous, _source) in historical_states.items()
            ),
            key=lambda item: (item[0], item[1]),
        )
        for ts, entity_id, state in ordered:
            context.observe(entity_id, state, ts, learn=False)
        result = context.forecast(str(target_entity), float(sample_ts))
        result = dict(result or {})
        result["snapshot_contract"] = "fresh_bounded_asof_source_replay"
        return result
    except Exception as exc:
        return {
            "snapshot_contract": "unavailable",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def _baseline_metadata(agent):
    try:
        from ha import AUTOMATION_KNOWLEDGE
        _hints, infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
    except Exception:
        infos = []
    rows = []
    for info in infos or ():
        rows.append({
            "entity_id": info.get("entity_id"),
            "name": info.get("name"),
            "enabled": bool(info.get("enabled")),
            "context_entities": list(info.get("context_entities") or []),
            "baseline_rules": list(info.get("baseline_rules") or []),
            "action_services": list(info.get("action_services") or []),
            "config_status": info.get("config_status"),
        })
    return {
        "contract": "automation_is_structural_baseline_not_ground_truth",
        "target_entity": agent.get("target_entity"),
        "automations": rows,
    }


def capture_broad_context(core, agent, *, sample_ts, desired, rejected, source,
                          supervision_id, feedback_id=None, generation_id=None):
    """Persist one broad historical snapshot after explicit feedback.

    This function is never called from Engine.process_agent/process_state_event.  Its
    potentially wider SQLite work is bounded to explicit user feedback.
    """
    from context import context_scalar
    import manual_context_learning as manual_context

    entity_ids, live_states, registry = _eligible_entities(core.ENGINE, agent)
    historical = _historical_states(core.STORE, entity_ids, sample_ts)
    snapshot = {}
    roles = Counter()
    for entity_id in entity_ids:
        pair = historical.get(entity_id)
        if pair is None:
            continue
        state, previous, history_source = pair
        value = context_scalar(entity_id, state, agent)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        role = semantic_source_role(core.ENGINE, agent, entity_id, state, registry)
        roles[role] += 1
        previous_value = context_scalar(entity_id, previous, agent) if previous else None
        try:
            previous_value = None if previous_value is None else float(previous_value)
        except (TypeError, ValueError):
            previous_value = None
        ts = float(state.get("_history_ts") or sample_ts)
        previous_ts = (
            float(previous.get("_history_ts")) if previous and previous.get("_history_ts") is not None
            else None
        )
        source_meta = core.ENGINE.context.evidence_metadata(entity_id)
        snapshot[entity_id] = {
            "v": round(value, 8),
            "age": round(max(0.0, float(sample_ts) - ts), 3),
            "role": role,
            "area_id": core.ENGINE.context.area_for(entity_id),
            "source_role": (source_meta or {}).get("role"),
            "history_source": history_source,
            "recent_delta": (
                None if previous_value is None else round(value - previous_value, 8)
            ),
            "previous_age": (
                None if previous_ts is None
                else round(max(0.0, float(sample_ts) - previous_ts), 3)
            ),
        }

    room = _historical_room_belief(
        core.ENGINE, agent["target_entity"], historical, sample_ts
    )
    metadata = {
        "contract_version": CONTRACT_VERSION,
        "supervision_event_id": supervision_id,
        "sample_ts": float(sample_ts),
        "target_entity": agent.get("target_entity"),
        "target_area_id": core.ENGINE.context.area_for(agent.get("target_entity")),
        "entity_count": len(snapshot),
        "role_counts": dict(sorted(roles.items())),
        "room_belief": room,
        "baseline": _baseline_metadata(agent),
        "feedback_id": feedback_id,
        "generation_id": generation_id,
        "capture_path": "explicit_feedback_only_not_event_intent_hot_path",
    }
    limit = max(16, int(getattr(core, "OPTIONS", {}).get(
        "manual_context_max_snapshots", 256
    ))) if hasattr(core, "OPTIONS") else 256
    with core.STORE.lock, core.STORE.conn() as c:
        c.execute(
            """INSERT INTO manual_context_feedback
               (agent_id,created_ts,desired_value,rejected_value,source,user_id,snapshot_json,
                sample_ts,supervision_event_id,metadata_json)
               VALUES(?,?,?,?,?,?,?, ?,?,?)""",
            (
                str(agent["id"]), time.time(), float(desired),
                None if rejected is None else float(rejected), str(source or "manual"),
                None, json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
                float(sample_ts), str(supervision_id),
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
            ),
        )
        c.execute(
            """DELETE FROM manual_context_feedback
               WHERE agent_id=? AND id NOT IN (
                   SELECT id FROM manual_context_feedback
                   WHERE agent_id=? ORDER BY id DESC LIMIT ?
               )""",
            (str(agent["id"]), str(agent["id"]), int(limit)),
        )
    manual_context._SCORE_CACHE.pop(str(agent["id"]), None)
    manual_context._OBSERVATION_CACHE.pop(str(agent["id"]), None)
    return {
        "recorded": True,
        "supervision_event_id": supervision_id,
        "candidates": len(snapshot),
        "role_counts": dict(sorted(roles.items())),
        "room_belief": room,
    }


def _copy_manual_context(store, parent_id, candidate_id):
    """Carry broad evidence with active supervision events, not with DB row identity."""
    with store.lock, store.conn() as c:
        if not _table_exists(c, "manual_context_feedback"):
            return 0
        active = {
            str(row[0])
            for row in c.execute(
                """SELECT supervision_event_id FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL
                     AND supervision_event_id IS NOT NULL
                     AND supervision_event_id!=''""",
                (str(candidate_id),),
            ).fetchall()
            if row[0]
        }
        c.execute("DELETE FROM manual_context_feedback WHERE agent_id=?", (str(candidate_id),))
        if not active:
            return 0
        marks = ",".join("?" for _ in active)
        rows = c.execute(
            f"""SELECT * FROM manual_context_feedback
                WHERE agent_id=? AND supervision_event_id IN ({marks})
                ORDER BY created_ts,id""",
            (str(parent_id), *sorted(active)),
        ).fetchall()
        latest = {}
        for row in rows:
            event_id = str(row["supervision_event_id"] or "")
            marker = (float(row["created_ts"] or 0.0), int(row["id"]))
            if event_id and (event_id not in latest or marker >= latest[event_id][0]):
                latest[event_id] = (marker, row)
        for _event_id, (_marker, row) in sorted(latest.items()):
            c.execute(
                """INSERT INTO manual_context_feedback
                   (agent_id,created_ts,desired_value,rejected_value,source,user_id,snapshot_json,
                    sample_ts,supervision_event_id,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(candidate_id), row["created_ts"], row["desired_value"],
                    row["rejected_value"], row["source"], row["user_id"], row["snapshot_json"],
                    row["sample_ts"], row["supervision_event_id"], row["metadata_json"],
                ),
            )
    return len(latest)


def _residual_diagnostics(policy, samples):
    unresolved = []
    desired_counts = Counter()
    for sample in samples or ():
        predicted_idx, predicted_value = __import__(
            "agent_candidate_conservative_correct"
        )._prediction_index(policy, sample["features"])
        desired_idx = int(sample["desired_idx"])
        if int(predicted_idx) == desired_idx:
            continue
        label = dict(sample.get("label") or {})
        event_id = label.get("supervision_event_id") or supervision_event_id(
            label.get("fingerprint"), label.get("sample_ts"), label.get("desired")
        )
        desired_counts[str(desired_idx)] += 1
        unresolved.append({
            "label_id": label.get("id"),
            "supervision_event_id": event_id,
            "sample_ts": label.get("sample_ts"),
            "desired_idx": desired_idx,
            "predicted_idx": int(predicted_idx),
            "predicted_value": float(predicted_value),
        })
    total = len(samples or ())
    return {
        "current_schema_fit_count": total - len(unresolved),
        "current_schema_fit_total": total,
        "current_schema_fit": (
            (total - len(unresolved)) / total if total else None
        ),
        "unresolved_correct_count": len(unresolved),
        "unresolved_correct_ids": [
            row["supervision_event_id"] for row in unresolved
        ],
        "residual_class_distribution": dict(sorted(desired_counts.items())),
        "residuals": unresolved,
    }


def _install_teaching(core):
    service = getattr(core.ENGINE, "rl_teaching", None)
    if service is None or getattr(service, "_correct_data_foundation", False):
        return
    _ensure_schema(core.STORE)
    original_labels = service.labels
    original_insert = service._insert_label

    def labels(agent_id, include_undone=False):
        rows = original_labels(agent_id, include_undone=include_undone)
        if include_undone:
            return rows
        _backfill_agent_event_ids(core.STORE, agent_id)
        # Re-read only when the migration had a chance to fill a legacy NULL id.
        if any(not row.get("supervision_event_id") for row in rows):
            rows = original_labels(agent_id, include_undone=False)
        return deduplicate_supervision_rows(rows)

    def insert_label(agent, desired, timestamp, previous, **kwargs):
        result = original_insert(agent, desired, timestamp, previous, **kwargs)
        label_id = result.get("label_id") if isinstance(result, dict) else None
        if label_id is None:
            return result
        with core.STORE.lock, core.STORE.conn() as c:
            row = c.execute(
                """SELECT id,fingerprint,sample_ts,desired FROM teaching_rl_labels
                   WHERE id=?""",
                (int(label_id),),
            ).fetchone()
            if not row:
                return result
            event_id = supervision_event_id(
                row["fingerprint"], row["sample_ts"], row["desired"]
            )
            c.execute(
                "UPDATE teaching_rl_labels SET supervision_event_id=? WHERE id=?",
                (event_id, int(label_id)),
            )
        source = str(kwargs.get("source") or "manual")
        try:
            broad = capture_broad_context(
                core,
                agent,
                sample_ts=float(timestamp),
                desired=float(result.get("desired_value", desired)),
                rejected=previous,
                source=source,
                supervision_id=event_id,
                feedback_id=result.get("feedback_id"),
                generation_id=kwargs.get("generation_id"),
            )
        except Exception as exc:
            # The supervision fact is already durable.  A diagnostic/context failure must
            # not strand the UI after accepting the user's Correct or prevent the normal
            # Candidate listener from running.  Surface it explicitly for repair instead.
            broad = {
                "recorded": False,
                "supervision_event_id": event_id,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }
            core.STORE.event(
                agent["id"], "warning", "correct_broad_context_failed",
                "Correct was recorded but broad context capture failed",
                {"supervision_event_id": event_id, "error": broad["error"]},
            )
        result["supervision_event_id"] = event_id
        result["manual_context"] = broad
        return result

    service.labels = labels
    service._insert_label = insert_label
    service._correct_data_foundation = True


def _install_candidate_sync(core, manager):
    if getattr(manager, "_correct_data_foundation_sync", False):
        return
    original = manager._sync_feedback

    def sync_feedback(parent, candidate):
        result = original(parent, candidate)
        _backfill_agent_event_ids(core.STORE, parent["id"])
        _backfill_agent_event_ids(core.STORE, candidate["id"])
        with core.STORE.lock, core.STORE.conn() as c:
            rows = c.execute(
                """SELECT * FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL
                   ORDER BY sample_ts,created_ts,id""",
                (str(candidate["id"]),),
            ).fetchall()
            unique = deduplicate_supervision_rows(rows)
            keep = {int(row["id"]) for row in unique}
            stale = [int(row["id"]) for row in rows if int(row["id"]) not in keep]
            if stale:
                c.executemany(
                    "DELETE FROM teaching_rl_labels WHERE id=?",
                    [(row_id,) for row_id in stale],
                )
        copied_context = _copy_manual_context(
            core.STORE, parent["id"], candidate["id"]
        )
        result = dict(result or {})
        result["supervision_events"] = len(unique)
        result["supervision_rows_deduplicated"] = len(stale)
        result["manual_context_copied"] = int(copied_context)
        return result

    manager._sync_feedback = sync_feedback
    manager._correct_data_foundation_sync = True


def _install_residual_diagnostics(manager):
    if getattr(manager, "_correct_residual_diagnostics", False):
        return
    import agent_candidate_conservative_correct as conservative
    import agent_candidate_hard_correct as hard

    original_fine_tune = conservative._conservative_fine_tune
    original_gate = conservative._offline_gate

    def fine_tune(owner, candidate):
        report = dict(original_fine_tune(owner, candidate) or {})
        policy = owner.engine.models.get(candidate["id"])
        if policy is None:
            owner.engine.models.pop(candidate["id"], None)
            policy = owner.engine.policy(candidate)
        all_samples = hard._collect_explicit_corrections(owner, candidate, policy)
        usable, conflicts = hard._partition_conflicts(all_samples)
        diagnostics = _residual_diagnostics(policy, usable)
        report.update(diagnostics)
        report["residual_diagnostics_contract"] = (
            "frozen_current_schema_non_conflicting_correct_only"
        )
        report["residual_conflict_groups"] = conflicts
        return report

    def offline_gate(parent, parent_stats, candidate_stats, teach_report=None):
        report = dict(teach_report or {})
        gate = dict(original_gate(parent, parent_stats, candidate_stats, report) or {})
        for key in (
            "current_schema_fit_count", "current_schema_fit_total", "current_schema_fit",
            "unresolved_correct_count", "unresolved_correct_ids",
            "residual_class_distribution", "residuals",
            "residual_diagnostics_contract", "residual_conflict_groups",
        ):
            if key in report:
                gate[key] = report[key]
        return gate

    conservative._conservative_fine_tune = fine_tune
    conservative._offline_gate = offline_gate
    manager._correct_residual_diagnostics = True


def install(core, manager):
    """Install Stage-1 data contracts after the established Candidate stack."""
    if getattr(manager, "_correct_data_foundation_installed", False):
        return manager
    _ensure_schema(core.STORE)
    _install_teaching(core)
    _install_candidate_sync(core, manager)
    _install_residual_diagnostics(manager)
    manager._correct_data_foundation_installed = True
    manager.correct_data_foundation_contract = (
        "stable_supervision_event+broad_historical_context+semantic_roles+residual_diagnostics"
    )
    core.STORE.event(
        None,
        "info",
        "correct_data_foundation_ready",
        "Correct supervision identity and broad residual context are active",
        {
            "contract_version": CONTRACT_VERSION,
            "max_broad_entities": MAX_BROAD_ENTITIES,
            "hot_path": False,
            "roles": [
                ROLE_LOCAL, ROLE_BOUNDARY, ROLE_TRAJECTORY, ROLE_RELIABILITY, ROLE_OTHER,
            ],
        },
    )
    return manager
