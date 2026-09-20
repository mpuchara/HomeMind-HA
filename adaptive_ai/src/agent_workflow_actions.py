"""Generation-aware agent workflow actions.

User-facing workflow:

* Autonomous: exact direct-parent snapshot + non-destructive historical continuation.
* Correct: historical observed-Desired labels + conservative child correction.
* Change decision: immediate contextual teaching + full-context observation + child correction.

No action mutates the parent policy in place. Correct/Change-decision bursts may coalesce
only when they target the same direct parent generation. Autonomous always requires a
fresh child. Candidate actions are policy-only and never call Home Assistant services.
"""
import json
import math
import time
from urllib.parse import parse_qs, unquote, urlsplit

from context import target_value
from manual_context_learning import observe as observe_manual_context
from manual_feedback import UI_USER_ID, _manual_value
from teaching_rl import fingerprint as rl_fingerprint

from agent_candidate_lineage import (
    _active_tip,
    _ensure_root,
    _generation_children,
    _refresh_generation_metadata,
    _row as lineage_row,
    config_fingerprint,
    model_metadata,
)
from agent_candidate_conservative_correct import (
    _benchmark_stats,
    _copy_parent_snapshot,
    _offline_gate,
    _persist_gate,
)


OBSERVED_STALE_SECONDS = 95.0
CORRECT_REASON = "teach_train"
CHANGE_REASON = "wrong_decision"
AUTONOMOUS_REASON = "autonomous"


def ensure_workflow_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_generation_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_agent_id TEXT NOT NULL,
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT,
                action TEXT NOT NULL,
                requested_ts REAL NOT NULL,
                started_ts REAL,
                finished_ts REAL,
                coalesced INTEGER NOT NULL DEFAULT 0,
                parent_model_identity TEXT,
                parent_model_revision TEXT,
                parent_schema_revision TEXT,
                child_model_identity TEXT,
                child_model_revision TEXT,
                child_schema_revision TEXT,
                schema_changed INTEGER,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_generation_actions_parent
                ON agent_generation_actions(parent_generation_id,id DESC);
            CREATE INDEX IF NOT EXISTS idx_generation_actions_child
                ON agent_generation_actions(child_generation_id,id DESC);
            """
        )


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _sync_live_generation_metadata(manager, generation):
    if not generation or generation.get("generation_type") != "live":
        return generation
    agent_id = str(generation["agent_id"])
    agent = manager.store.get_agent_config(agent_id)
    if not agent:
        return generation
    meta = model_metadata(manager.store.get_model(agent_id))
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE agent_candidate_generations
               SET generation_number=?,model_identity=?,model_revision=?,schema_revision=?,
                   config_fingerprint=?,lifecycle_state='live',updated_ts=?
               WHERE generation_id=?""",
            (
                int(manager._generation(agent_id)), meta.get("model_identity"),
                meta.get("model_revision"), meta.get("schema_revision"),
                config_fingerprint(agent), time.time(), generation["generation_id"],
            ),
        )
    return lineage_row(manager.store, generation_id=generation["generation_id"])


def _resolve_generation(manager, ref):
    ref = str(ref)
    generation = lineage_row(manager.store, generation_id=ref) or lineage_row(manager.store, agent_id=ref)
    if generation is None:
        agent = manager.store.get_agent_config(ref)
        if agent:
            generation = _ensure_root(manager.store, ref, manager._generation(ref))
    generation = _sync_live_generation_metadata(manager, generation)
    if not generation:
        raise ValueError("Agent generation not found")
    agent_id = generation.get("agent_id")
    if not agent_id or not manager.store.get_agent_config(str(agent_id)):
        raise ValueError("Generation model is no longer retained")
    if manager.store.get_model(str(agent_id)) is None:
        raise ValueError("Generation has no usable model")
    return generation, manager.store.get_agent_config(str(agent_id))


def _active_children(manager, parent_generation):
    rows = _generation_children(manager.store, parent_generation["generation_id"], include_retired=True)
    return [
        row for row in rows
        if str(row.get("lifecycle_state") or "") not in ("discarded", "pruned", "promoted")
        and row.get("agent_id")
    ]


def _preflight_child(manager, parent_generation, *, allow_coalesce):
    children = _active_children(manager, parent_generation)
    if len(children) > 1:
        raise ValueError("Generation has multiple active children; resolve lineage first")
    if not children:
        return None
    child = children[0]
    if _active_children(manager, child):
        raise ValueError(
            "This parent is no longer the active correction edge; use the current Candidate generation"
        )
    if not allow_coalesce:
        raise ValueError("Autonomous requires a parent generation without an existing child")
    return child


def _effective_correct_parent(manager, selected_generation):
    """Resolve where new Correct feedback belongs on a single active lineage.

    Root Live feedback may continue updating G1 while G1 is still the leaf. Once G2+
    exists, the immutable lineage must advance from the current Candidate tip instead of
    trying to recreate a child from a pruned/root edge.
    """
    if selected_generation.get("generation_type") != "live":
        tip = _active_tip(manager.store, selected_generation["root_agent_id"])
        if tip and str(tip.get("generation_id")) != str(selected_generation.get("generation_id")):
            raise ValueError(
                "This Candidate generation is no longer the active correction edge; "
                "use the current Candidate generation"
            )
        return selected_generation

    tip = _active_tip(manager.store, selected_generation["root_agent_id"])
    if tip and int(tip.get("generation_number") or 0) >= 2:
        return tip
    return selected_generation


def _merge_correct_labels(manager, source_agent, target_agent):
    """Merge active historical Correct labels without mutating either parent policy.

    This is needed when a Correct entered from the Root Live card advances from a deep
    Candidate tip. The selected historical label lives on Root Live, while the next child
    is trained from the current tip. Copying the explicit labels to that tip preserves the
    user's instruction and lets the existing Candidate sync path remain authoritative.
    """
    if str(source_agent["id"]) == str(target_agent["id"]):
        return 0

    source_fp = str(rl_fingerprint(source_agent))
    target_fp = str(rl_fingerprint(target_agent))
    with manager.store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT created_ts,sample_ts,desired,previous_desired
               FROM teaching_rl_labels
               WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=?
               ORDER BY sample_ts,created_ts,id""",
            (str(source_agent["id"]), source_fp),
        ).fetchall()]
    if not rows:
        return 0

    merged = 0
    with manager.store.lock, manager.store.conn() as c:
        for row in rows:
            existing = c.execute(
                """SELECT id,created_ts FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=?
                     AND ABS(sample_ts-?)<0.001 AND ABS(desired-?)<0.000001
                   ORDER BY created_ts DESC,id DESC LIMIT 1""",
                (
                    str(target_agent["id"]), target_fp, float(row["sample_ts"]),
                    float(row["desired"]),
                ),
            ).fetchone()
            if existing:
                if float(row["created_ts"]) > float(existing["created_ts"] or 0.0):
                    c.execute(
                        """UPDATE teaching_rl_labels
                           SET created_ts=?,previous_desired=? WHERE id=?""",
                        (
                            float(row["created_ts"]), row.get("previous_desired"),
                            int(existing["id"]),
                        ),
                    )
                continue
            c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (
                    str(target_agent["id"]), float(row["created_ts"]),
                    float(row["sample_ts"]), float(row["desired"]),
                    row.get("previous_desired"), target_fp,
                ),
            )
            merged += 1
    return merged


def _edge_for(manager, parent_generation, child_generation):
    with manager.store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_candidates WHERE parent_agent_id=? AND candidate_id=?",
            (str(parent_generation["agent_id"]), str(child_generation["agent_id"])),
        ).fetchone()
    return dict(row) if row else None


def _record_action(manager, parent_generation, child_generation, action, *, coalesced=False, detail=None):
    pmeta = model_metadata(manager.store.get_model(parent_generation["agent_id"]))
    cmeta = model_metadata(manager.store.get_model(child_generation["agent_id"])) if child_generation else {}
    with manager.store.lock, manager.store.conn() as c:
        row = c.execute(
            """INSERT INTO agent_generation_actions
               (root_agent_id,parent_generation_id,child_generation_id,action,requested_ts,coalesced,
                parent_model_identity,parent_model_revision,parent_schema_revision,
                child_model_identity,child_model_revision,child_schema_revision,detail_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(parent_generation["root_agent_id"]), str(parent_generation["generation_id"]),
                str(child_generation["generation_id"]) if child_generation else None,
                str(action), time.time(), int(bool(coalesced)),
                pmeta.get("model_identity"), pmeta.get("model_revision"), pmeta.get("schema_revision"),
                cmeta.get("model_identity"), cmeta.get("model_revision"), cmeta.get("schema_revision"),
                json.dumps(detail or {}, separators=(",", ":"), default=str),
            ),
        )
        return int(row.lastrowid)


def _coalesce_child(manager, parent_generation, child_generation, reason):
    edge = _edge_for(manager, parent_generation, child_generation)
    if not edge:
        raise ValueError("Existing child edge is unavailable")
    now = time.time()
    state = "building" if str(edge.get("state") or "") == "building" else "queued"
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE agent_candidates SET
                 feedback_revision=feedback_revision+1,dirty=1,state=?,reason=?,queued_ts=?,
                 comparison_json='{}',offline_gate_json='{}',last_error=NULL,
                 discard_requested=0,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=?""",
            (
                state, str(reason), now, now,
                str(parent_generation["agent_id"]), str(child_generation["agent_id"]),
            ),
        )
        c.execute(
            """UPDATE agent_candidate_generations
               SET lifecycle_state=?,comparison_json='{}',updated_ts=? WHERE generation_id=?""",
            (state, now, str(child_generation["generation_id"])),
        )
    manager.runtime.pop(str(parent_generation["agent_id"]), None)
    manager.wake_event.set()
    return manager.lineage_status(child_generation["generation_id"])


def _create_or_coalesce_child(manager, parent_generation, reason, action, *, allow_coalesce=True):
    before = model_metadata(manager.store.get_model(parent_generation["agent_id"]))
    existing = _preflight_child(manager, parent_generation, allow_coalesce=allow_coalesce)
    coalesced = existing is not None
    if existing is not None:
        status = _coalesce_child(manager, parent_generation, existing, reason)
        child = existing
    elif parent_generation.get("generation_type") == "live":
        status = manager.enqueue(parent_generation["agent_id"], reason)
        child = lineage_row(manager.store, agent_id=status["candidate_id"])
    else:
        status = manager.spawn_child(parent_generation["generation_id"], reason)
        child = lineage_row(manager.store, generation_id=status["generation_id"])
        if action in ("correct", "change_decision"):
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET feedback_revision=feedback_revision+1,
                       queued_ts=?,updated_ts=? WHERE parent_agent_id=? AND candidate_id=?""",
                    (
                        time.time(), time.time(), str(parent_generation["agent_id"]),
                        str(child["agent_id"]),
                    ),
                )
            status = manager.lineage_status(child["generation_id"]) or status
    after = model_metadata(manager.store.get_model(parent_generation["agent_id"]))
    if before.get("model_identity") != after.get("model_identity"):
        raise RuntimeError("Parent model changed while creating a child generation")
    action_id = _record_action(
        manager, parent_generation, child, action, coalesced=coalesced,
        detail={"reason": reason, "parent_model_unchanged": True},
    )
    manager.store.event(
        parent_generation["root_agent_id"], "info", "agent_generation_action_queued",
        f"{action} queued a child Candidate from the selected parent generation",
        {
            "action_id": action_id, "action": action,
            "parent_generation_id": parent_generation["generation_id"],
            "child_generation_id": child["generation_id"], "coalesced": bool(coalesced),
        },
    )
    return {
        "ok": True, "action": action, "action_id": action_id,
        "coalesced": bool(coalesced),
        "parent_generation_id": parent_generation["generation_id"],
        "child_generation_id": child["generation_id"], "child": status,
    }


def _observed_live_decision(manager, agent_id, timestamp):
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT ts,current,desired FROM decision_history
               WHERE agent_id=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
            (str(agent_id), float(timestamp)),
        ).fetchone()
    if not row or float(timestamp) - float(row["ts"]) > OBSERVED_STALE_SECONDS:
        return None
    return dict(row)


def _observed_decision(manager, generation, timestamp):
    if hasattr(manager, "generation_decision_at"):
        row = manager.generation_decision_at(generation["generation_id"], timestamp)
        if row:
            return row
    if generation.get("generation_type") == "live":
        return _observed_live_decision(manager, generation["agent_id"], timestamp)
    return None


def _correct_point(manager, generation, agent, timestamp):
    timestamp = float(timestamp)
    base = manager.engine.rl_teaching.point(agent, timestamp)
    observed = _observed_decision(manager, generation, timestamp)
    base["desired"] = None if observed is None else observed.get("desired")
    base["confidence"] = None if observed is None else observed.get("confidence")
    base["observed_prediction"] = observed is not None
    base["desired_source"] = "observed_generation_runtime"
    base["generation_id"] = generation["generation_id"]
    base["policy_replay_used_for_desired"] = False
    if observed is None:
        base["context_complete"] = False
        base["gap"] = True
    else:
        base["gap"] = False
    return base


def _correct_history(manager, generation, agent, start, end):
    start, end = float(start), float(end)
    if end <= start or end - start > 31 * 86400:
        raise ValueError("Choose a history range from 1 second to 31 days")
    if generation.get("generation_type") == "candidate":
        history = manager.generation_history(generation["generation_id"], start, end)
    else:
        history = manager.engine.rl_teaching.history(agent, start, end)
        history["generation_id"] = generation["generation_id"]
    history["labels"] = [
        row for row in manager.engine.rl_teaching.labels(agent["id"])
        if str(row.get("fingerprint")) == str(rl_fingerprint(agent)) and row.get("undone_ts") is None
    ]
    history["agent"] = {
        "id": agent["id"], "name": agent.get("name"), "target_entity": agent["target_entity"],
        "target_property": agent["target_property"], "min_value": agent["min_value"],
        "max_value": agent["max_value"], "deadband": agent.get("deadband"),
    }
    history["desired_semantics"] = "prediction actually observed from this exact generation"
    history["policy_replay_used"] = False
    return history


def _store_correct_label(manager, generation, agent, desired, sample_ts):
    point = _correct_point(manager, generation, agent, sample_ts)
    if point.get("desired") is None:
        raise ValueError("This generation has no observed prediction at that moment; a gap cannot be corrected")
    if point.get("current") is None or not point.get("context_complete"):
        raise ValueError("Historical context is incomplete at that moment")
    states, _, _ = manager.engine.teaching.point_context(manager.engine, agent, float(sample_ts))
    target_state = states.get(agent["target_entity"])
    if not target_state:
        raise ValueError("Target state unavailable at the selected moment")
    desired = _manual_value(agent, target_state, desired)
    fp = rl_fingerprint(agent)
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        count = int(c.execute(
            "SELECT COUNT(*) FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL",
            (str(agent["id"]),),
        ).fetchone()[0])
        existing = c.execute(
            """SELECT id FROM teaching_rl_labels
               WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=? AND ABS(sample_ts-?)<0.001
               ORDER BY id DESC LIMIT 1""",
            (str(agent["id"]), fp, float(sample_ts)),
        ).fetchone()
        if existing:
            label_id = int(existing["id"])
            c.execute(
                "UPDATE teaching_rl_labels SET created_ts=?,desired=?,previous_desired=? WHERE id=?",
                (now, float(desired), float(point["desired"]), label_id),
            )
        else:
            if count >= int(manager.engine.rl_teaching.MAX_LABELS):
                raise ValueError("Correct label limit reached")
            row = c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (
                    str(agent["id"]), now, float(sample_ts), float(desired),
                    float(point["desired"]), fp,
                ),
            )
            label_id = int(row.lastrowid)
    manager.store.event(
        agent["id"], "info", "correct_label_added",
        "Correct label added without changing the parent model",
        {
            "generation_id": generation["generation_id"], "label_id": label_id,
            "sample_ts": float(sample_ts), "desired": float(desired),
            "previous_desired": point["desired"],
        },
    )
    return {
        "ok": True, "label_id": label_id, "sample_ts": float(sample_ts),
        "desired_value": float(desired), "previous_desired": point["desired"],
        "generation_id": generation["generation_id"],
    }


def _undo_correct_label(manager, generation, agent):
    with manager.store.lock, manager.store.conn() as c:
        row = c.execute(
            """SELECT id FROM teaching_rl_labels
               WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=? ORDER BY id DESC LIMIT 1""",
            (str(agent["id"]), rl_fingerprint(agent)),
        ).fetchone()
        if not row:
            raise ValueError("No Correct labels to undo")
        label_id = int(row["id"])
        c.execute("UPDATE teaching_rl_labels SET undone_ts=? WHERE id=?", (time.time(), label_id))
    return {"ok": True, "undone_id": label_id, "generation_id": generation["generation_id"]}


def _latest_prediction(manager, generation, agent):
    if generation.get("generation_type") == "live":
        rt = manager.engine.runtime.get(str(agent["id"])) or {}
        return rt.get("last_prediction"), rt.get("last_confidence")
    status = manager.lineage_status(generation["generation_id"]) if hasattr(manager, "lineage_status") else None
    if status and status.get("shadow_active"):
        return status.get("candidate_desired"), status.get("candidate_confidence")
    observed = _observed_decision(manager, generation, time.time())
    if observed:
        return observed.get("desired"), observed.get("confidence")
    return None, None


def _current_snapshot(manager, agent):
    with manager.engine.lock:
        state_map = dict(manager.engine.state_map)
    state = state_map.get(agent["target_entity"])
    current = target_value(state, agent["target_property"])
    if current is None:
        raise ValueError("Target state/value unavailable")
    current = float(current)
    if not math.isfinite(current):
        raise ValueError("Target state/value unavailable")
    return state_map, state, current


def _base_teach(engine, agent, desired):
    method = getattr(type(engine.teaching), "teach", None)
    if not callable(method):
        raise RuntimeError("Contextual teaching service unavailable")
    return method(engine.teaching, engine, agent, desired, None)


def _install_candidate_teaching_overlay(manager):
    try:
        import agent_candidate_shadow_runtime as shadow_module
    except Exception:
        return
    if getattr(shadow_module, "_workflow_teaching_overlay_installed", False):
        return
    original = shadow_module._predict_candidate

    def predict_with_teaching(active_manager, generation, state_map, event_ts):
        result = original(active_manager, generation, state_map, event_ts)
        if not result:
            return result
        agent = active_manager.store.get_agent_config(str(generation.get("agent_id") or ""))
        matcher = getattr(active_manager.engine.teaching, "match", None)
        if not agent or not callable(matcher):
            return result
        try:
            policy = active_manager.engine.models.get(str(agent["id"]))
            if policy is None:
                policy = active_manager.engine.policy(agent)
            label = matcher(agent, policy, state_map, active_manager.engine.temporal_history, float(event_ts))
            if label is not None:
                result = dict(result)
                result["desired"] = float(label["desired"])
                result["teaching_id"] = label.get("id")
                result["decision_source"] = "explicit_change_decision"
        except Exception as exc:
            active_manager.store.event(
                generation["root_agent_id"], "warning", "candidate_change_decision_overlay_gap",
                "Candidate contextual correction could not be applied to this Shadow event",
                {"generation_id": generation["generation_id"], "error": f"{type(exc).__name__}: {exc}"},
            )
        return result

    shadow_module._predict_candidate = predict_with_teaching
    shadow_module._workflow_teaching_overlay_installed = True


def install(manager):
    if getattr(manager, "_agent_workflow_actions_installed", False):
        return manager

    ensure_workflow_tables(manager.store)
    _install_candidate_teaching_overlay(manager)

    original_start = manager._start_build
    original_finish = manager._finish_build_if_ready
    handler = manager.core.Handler
    original_get = handler.do_GET
    original_post = handler.do_POST

    def workflow_subject(ref):
        generation, agent = _resolve_generation(manager, ref)
        state_map, _, current = _current_snapshot(manager, agent)
        predicted, confidence = _latest_prediction(manager, generation, agent)
        meta = model_metadata(manager.store.get_model(agent["id"]))
        return {
            "root_agent_id": generation["root_agent_id"],
            "generation_id": generation["generation_id"],
            "generation_number": int(generation["generation_number"]),
            "generation_type": generation["generation_type"],
            "agent_id": agent["id"], "name": agent.get("name"),
            "target_entity": agent["target_entity"], "target_property": agent["target_property"],
            "min_value": agent["min_value"], "max_value": agent["max_value"],
            "current": current, "observed_desired": predicted, "confidence": confidence,
            "model_revision": meta.get("model_revision"), "schema_revision": meta.get("schema_revision"),
            "settings_editable": generation.get("generation_type") == "live",
            "state_entities": len(state_map),
        }

    def workflow_autonomous(ref):
        generation, _ = _resolve_generation(manager, ref)
        _preflight_child(manager, generation, allow_coalesce=False)
        return _create_or_coalesce_child(
            manager, generation, AUTONOMOUS_REASON, "autonomous", allow_coalesce=False
        )

    def workflow_correct_commit(ref):
        selected_generation, selected_agent = _resolve_generation(manager, ref)
        with manager.store.conn() as c:
            count = int(c.execute(
                """SELECT COUNT(*) FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=?""",
                (str(selected_agent["id"]), rl_fingerprint(selected_agent)),
            ).fetchone()[0])
        if count <= 0:
            raise ValueError("Add at least one Correct point before creating the child Candidate")

        parent_generation = _effective_correct_parent(manager, selected_generation)
        parent_agent = manager.store.get_agent_config(str(parent_generation.get("agent_id") or ""))
        if not parent_agent:
            raise ValueError("Active correction parent is unavailable")
        _preflight_child(manager, parent_generation, allow_coalesce=True)

        merged_labels = _merge_correct_labels(manager, selected_agent, parent_agent)
        result = _create_or_coalesce_child(
            manager, parent_generation, CORRECT_REASON, "correct", allow_coalesce=True
        )
        result["selected_generation_id"] = selected_generation["generation_id"]
        result["effective_parent_generation_id"] = parent_generation["generation_id"]
        result["correct_labels_merged_to_parent"] = int(merged_labels)
        result["rebased_to_active_tip"] = (
            str(selected_generation["generation_id"]) != str(parent_generation["generation_id"])
        )
        return result

    def workflow_change_decision(ref, desired_value=None):
        generation, agent = _resolve_generation(manager, ref)
        _preflight_child(manager, generation, allow_coalesce=True)
        parent_before = model_metadata(manager.store.get_model(agent["id"]))
        state_map, _, current = _current_snapshot(manager, agent)
        predicted, _ = _latest_prediction(manager, generation, agent)
        if generation.get("generation_type") == "candidate" and predicted is None:
            raise ValueError("Candidate has no fresh observed Shadow decision to change")
        if generation.get("generation_type") == "candidate":
            manager.engine.runtime.setdefault(str(agent["id"]), {})["last_prediction"] = predicted

        taught = _base_teach(manager.engine, agent, desired_value)
        desired = float(taught["desired_value"])
        context_feedback = observe_manual_context(
            manager.core, agent, state_map, desired, rejected=predicted,
            source="workflow_change_decision", user_id=UI_USER_ID,
        )
        parent_after = model_metadata(manager.store.get_model(agent["id"]))
        if parent_before.get("model_identity") != parent_after.get("model_identity"):
            raise RuntimeError("Change decision modified the parent model in place")

        child = _create_or_coalesce_child(
            manager, generation, CHANGE_REASON, "change_decision", allow_coalesce=True
        )
        child.update({
            "current_value": current, "previous_desired": predicted, "desired_value": desired,
            "context_feedback": context_feedback, "physical_service": None,
            "parent_model_unchanged": True,
        })
        manager.store.event(
            generation["root_agent_id"], "info", "agent_change_decision",
            "Change decision updated contextual behavior and queued a child Candidate",
            {
                "generation_id": generation["generation_id"], "current": current,
                "previous_desired": predicted, "desired": desired,
                "child_generation_id": child["child_generation_id"], "physical_service": None,
            },
        )
        return child

    def start_build(row):
        if str(row.get("reason") or "") != AUTONOMOUS_REASON:
            return original_start(row)
        queue = manager._queue()
        if queue is None:
            return False
        candidate = manager.store.get_agent_config(row["candidate_id"])
        parent = manager.store.get_agent_config(row["parent_agent_id"])
        if not candidate or not parent:
            manager._fail(row, "candidate or parent generation disappeared")
            return True
        if queue.status_for(candidate["id"]):
            return False
        try:
            candidate = _copy_parent_snapshot(manager, parent["id"], candidate["id"])
            build_revision = int(row.get("feedback_revision") or 0)
            now = time.time()
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET state='building',build_revision=?,dirty=0,
                       build_started_ts=?,build_finished_ts=NULL,comparison_started_ts=NULL,
                       comparison_json='{}',offline_gate_json='{}',last_error=NULL,updated_ts=?
                       WHERE parent_agent_id=? AND candidate_id=?""",
                    (
                        build_revision, now, now,
                        str(row["parent_agent_id"]), str(row["candidate_id"]),
                    ),
                )
                c.execute(
                    """UPDATE agent_generation_actions SET started_ts=?
                       WHERE child_generation_id=(
                         SELECT generation_id FROM agent_candidate_generations WHERE agent_id=?
                       ) AND action='autonomous' AND started_ts IS NULL""",
                    (now, str(candidate["id"])),
                )
            queued = queue.enqueue(candidate["id"], rebuild=False, reason="autonomous_continuation")
            manager.store.event(
                row["parent_agent_id"], "info", "agent_autonomous_continuation_started",
                "Autonomous child continues from the parent snapshot without clear_learning",
                {
                    "candidate_id": candidate["id"], "queue": queued,
                    "training_mode": "resume_from_parent_cursor",
                    "schema_policy": "preserve_parent_schema",
                },
            )
            return True
        except Exception as exc:
            manager._fail(row, f"{type(exc).__name__}: {exc}")
            return True

    def finish_action_audit(row):
        child_gen = lineage_row(manager.store, agent_id=row.get("candidate_id"))
        if not child_gen:
            return
        child_meta = model_metadata(manager.store.get_model(row["candidate_id"]))
        with manager.store.conn() as c:
            audits = [dict(x) for x in c.execute(
                """SELECT * FROM agent_generation_actions
                   WHERE child_generation_id=? AND finished_ts IS NULL ORDER BY id""",
                (str(child_gen["generation_id"]),),
            ).fetchall()]
        now = time.time()
        with manager.store.lock, manager.store.conn() as c:
            for audit in audits:
                schema_changed = str(audit.get("parent_schema_revision")) != str(child_meta.get("schema_revision"))
                detail = _json(audit.get("detail_json"), {})
                detail.update({
                    "child_model_revision": child_meta.get("model_revision"),
                    "child_schema_revision": child_meta.get("schema_revision"),
                    "schema_changed": bool(schema_changed), "sensor_changes_auditable": True,
                })
                c.execute(
                    """UPDATE agent_generation_actions SET finished_ts=?,child_model_identity=?,
                       child_model_revision=?,child_schema_revision=?,schema_changed=?,detail_json=? WHERE id=?""",
                    (
                        now, child_meta.get("model_identity"), child_meta.get("model_revision"),
                        child_meta.get("schema_revision"), int(schema_changed),
                        json.dumps(detail, separators=(",", ":"), default=str), int(audit["id"]),
                    ),
                )

    def finish_build(row):
        result = original_finish(row)
        if not result:
            return result
        fresh = manager._candidate_row(row["parent_agent_id"]) or row
        reason = str(fresh.get("reason") or row.get("reason") or "")
        if reason == AUTONOMOUS_REASON and str(fresh.get("state") or "") in ("comparing", "ready"):
            parent = manager.store.get_agent(fresh["parent_agent_id"])
            child = manager.store.get_agent(fresh["candidate_id"])
            if parent and child and manager.store.get_model(child["id"]):
                pmeta = model_metadata(manager.store.get_model(parent["id"]))
                cmeta = model_metadata(manager.store.get_model(child["id"]))
                report = {
                    "mode": "autonomous_continuation",
                    "base_model_revision": pmeta.get("model_revision"),
                    "candidate_model_revision": cmeta.get("model_revision"),
                    "schema_changed": pmeta.get("schema_revision") != cmeta.get("schema_revision"),
                }
                gate = _offline_gate(parent, _benchmark_stats(parent), _benchmark_stats(child), report)
                state = _persist_gate(manager, fresh, gate)
                if lineage_row(manager.store, agent_id=child["id"]):
                    _refresh_generation_metadata(
                        manager.store, child["id"], lifecycle_state=state,
                        comparison_json=fresh.get("comparison_json") or "{}",
                    )
                manager.store.event(
                    fresh["parent_agent_id"], "info" if gate.get("passed") else "warning",
                    "agent_autonomous_continuation_finished",
                    "Autonomous continuation finished with an auditable offline regression gate",
                    {
                        "candidate_id": child["id"], "offline_gate": gate,
                        "schema_changed": report["schema_changed"],
                    },
                )
        finish_action_audit(fresh)
        return result

    def do_get(http):
        parsed = urlsplit(http.path)
        if parsed.path == "/agent_workflow_ui.js":
            if not http.require_trusted_client():
                return
            return http.static("agent_workflow_ui.js", "application/javascript; charset=utf-8")
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"]:
            if not http.require_trusted_client() or not http.require_runtime():
                return
            ref, action = unquote(tokens[2]), tokens[3]
            try:
                generation, agent = _resolve_generation(manager, ref)
                if action == "status":
                    return http.send_json(200, workflow_subject(ref))
                query = parse_qs(parsed.query)
                if action == "correct-history":
                    now = time.time()
                    start = float((query.get("start") or [now - 600])[0])
                    end = float((query.get("end") or [now])[0])
                    return http.send_json(200, _correct_history(manager, generation, agent, start, end))
                if action == "correct-point":
                    ts = float((query.get("ts") or [time.time()])[0])
                    return http.send_json(200, _correct_point(manager, generation, agent, ts))
            except (TypeError, ValueError) as exc:
                return http.send_json(404, {"error": str(exc)})
        return original_get(http)

    def do_post(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"]:
            if not http.require_trusted_client() or not http.require_runtime():
                return
            ref, action = unquote(tokens[2]), tokens[3]
            try:
                payload = http.read_json()
                payload = payload if isinstance(payload, dict) else {}
                generation, agent = _resolve_generation(manager, ref)
                if action == "autonomous":
                    return http.send_json(202, workflow_autonomous(ref))
                if action == "correct-label":
                    return http.send_json(200, _store_correct_label(
                        manager, generation, agent,
                        payload.get("desired_value"), payload.get("sample_ts"),
                    ))
                if action == "correct-undo":
                    return http.send_json(200, _undo_correct_label(manager, generation, agent))
                if action == "correct":
                    return http.send_json(202, workflow_correct_commit(ref))
                if action == "change-decision":
                    return http.send_json(202, workflow_change_decision(ref, payload.get("desired_value")))
            except ValueError as exc:
                return http.send_json(409, {"error": str(exc)})
            except Exception as exc:
                return http.send_json(500, {"error": f"Generation workflow failed: {type(exc).__name__}: {exc}"})
        return original_post(http)

    manager._start_build = start_build
    manager._finish_build_if_ready = finish_build
    manager.workflow_subject = workflow_subject
    manager.workflow_autonomous = workflow_autonomous
    manager.workflow_correct_commit = workflow_correct_commit
    manager.workflow_change_decision = workflow_change_decision
    manager.workflow_correct_history = lambda ref, start, end: _correct_history(
        manager, *_resolve_generation(manager, ref), start, end
    )
    manager.workflow_correct_point = lambda ref, ts: _correct_point(
        manager, *_resolve_generation(manager, ref), ts
    )
    manager.workflow_add_correct_label = lambda ref, desired, ts: _store_correct_label(
        manager, *_resolve_generation(manager, ref), desired, ts
    )
    manager.workflow_undo_correct_label = lambda ref: _undo_correct_label(
        manager, *_resolve_generation(manager, ref)
    )
    handler.do_GET = do_get
    handler.do_POST = do_post
    manager._agent_workflow_actions_installed = True
    manager.agent_workflow_contract = "autonomous_correct_change_decision_generation_children_parent_model_immutable"
    manager.agent_workflow_coalescing_contract = "same_parent_generation_only"
    manager.agent_autonomous_contract = "exact_parent_snapshot_resume_history_no_clear_learning_schema_preserving"
    return manager
