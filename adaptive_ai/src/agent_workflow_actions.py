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
import threading
import time
import uuid
from sqlite_background import connection_session, is_sqlite_busy
from urllib.parse import parse_qs, unquote, urlsplit

from context import target_value
from manual_context_learning import observe as observe_manual_context
from manual_feedback import UI_USER_ID, _manual_value
from policy_backend import verify_model_checksum
from teaching_rl import fingerprint as rl_fingerprint

from agent_candidate_lineage import (
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

            CREATE TABLE IF NOT EXISTS agent_correct_operations (
                operation_id TEXT PRIMARY KEY,
                root_agent_id TEXT NOT NULL,
                parent_generation_id TEXT NOT NULL,
                parent_agent_id TEXT NOT NULL,
                child_generation_id TEXT,
                candidate_id TEXT,
                label_ids_json TEXT NOT NULL,
                created_ts REAL NOT NULL,
                committed_ts REAL,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_correct_operations_parent
                ON agent_correct_operations(parent_generation_id,created_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_correct_operations_child
                ON agent_correct_operations(child_generation_id,created_ts DESC);
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
    if generation.get("lifecycle_state") in {"discarded", "pruned", "promoted"}:
        raise ValueError("Generation model is no longer retained")
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
        changed = c.execute(
            """UPDATE agent_candidates SET
                 feedback_revision=feedback_revision+1,dirty=1,state=?,reason=?,queued_ts=?,
                 comparison_json='{}',offline_gate_json='{}',last_error=NULL,
                 discard_requested=0,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=? AND discard_requested=0""",
            (
                state, str(reason), now, now,
                str(parent_generation["agent_id"]), str(child_generation["agent_id"]),
            ),
        ).rowcount
        if not changed:
            raise ValueError("Existing child is being discarded")
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
        # Production lineage guards may redirect Root feedback to the current lineage
        # tip. In that case enqueue returns lineage_status (generation_id/agent_id)
        # rather than the base Candidate status (candidate_id). Resolve both contracts
        # explicitly instead of creating the child and then crashing with KeyError.
        candidate_id = status.get("candidate_id") if isinstance(status, dict) else None
        generation_id = status.get("generation_id") if isinstance(status, dict) else None
        child = (
            lineage_row(manager.store, agent_id=candidate_id) if candidate_id else None
        ) or (
            lineage_row(manager.store, generation_id=generation_id) if generation_id else None
        )
        if not child:
            raise RuntimeError("Candidate child was created but its lineage identity is unavailable")
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
    read = getattr(manager, 'generation_decision_read', None)
    if callable(read):
        observed = read(generation['generation_id'], timestamp)
        if observed['pending']:
            # A known pending generation observation is a gap even for Live.
            # Do not fall back to an older row in the separate history journal.
            return None
        if observed['decision']:
            return observed['decision']
    elif hasattr(manager, "generation_decision_at"):
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


def _failed_correct_child(manager, generation):
    candidate_row = getattr(manager, "_candidate_row", None)
    if not callable(candidate_row):
        return False
    edge = candidate_row(generation["agent_id"]) or {}
    return (edge.get("reason") == CORRECT_REASON and edge.get("state") == "failed"
            and not edge.get("discard_requested"))


def _correct_label_counts(manager, generation, agent):
    """Read-only Correct inventory for the selected direct-parent generation.

    A Correct point never starts a build. Only points committed after the most
    recent successful Correct operation count as newly pending for a build.
    The full active label set remains available to cumulative Candidate learning.
    """
    fp = rl_fingerprint(agent)
    with manager.store.conn() as c:
        previous = c.execute(
            """SELECT COALESCE(json_extract(detail_json,'$.label_cutoff_ts'),committed_ts) AS label_cutoff
               FROM agent_correct_operations
               WHERE parent_generation_id=? AND status='committed'
               ORDER BY committed_ts DESC,created_ts DESC LIMIT 1""",
            (str(generation["generation_id"]),),
        ).fetchone()
        cutoff = (
            float(previous["label_cutoff"])
            if previous and previous["label_cutoff"] is not None else None
        )
        # Candidate models inherit the parent's Teach labels with historical
        # created_ts values. Inherited examples belong to cumulative training,
        # but they are NOT new Correct points selected on this Candidate.
        if generation.get("generation_type") == "candidate" and generation.get("created_ts") is not None:
            born = float(generation["created_ts"])
            cutoff = max(born, cutoff) if cutoff is not None else born
        counts = c.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN ? IS NULL OR created_ts>?
                                        THEN 1 ELSE 0 END), 0) AS pending
               FROM teaching_rl_labels
               WHERE agent_id=? AND fingerprint=? AND undone_ts IS NULL""",
            (cutoff, cutoff, str(agent["id"]), fp),
        ).fetchone()
    return {
        "correct_labels_total": int(counts["total"]),
        "correct_labels_pending": (
            int(counts["total"]) if _failed_correct_child(manager, generation)
            else int(counts["pending"])
        ),
    }


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

    def predict_with_teaching(active_manager, generation, state_map, event_ts, **kwargs):
        result = original(active_manager, generation, state_map, event_ts, **kwargs)
        if not result:
            return result
        teaching = active_manager.engine.teaching
        matcher = getattr(teaching, "match", None)
        if not callable(matcher):
            return result
        # Use the exact standard matcher's existing label-cache semantics. An
        # empty set cannot match any fingerprint, so no fresh agent read is
        # needed. Unknown/custom matchers retain their ordinary full path.
        from teaching import Teaching
        if (type(teaching) is Teaching and getattr(matcher, '__func__', None) is Teaching.match
                and not teaching.labels(str(generation.get('agent_id') or ''))):
            return result
        agent = active_manager.store.get_agent_config(str(generation.get("agent_id") or ""))
        if not agent:
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



def register_read_routes(registry, manager):
    """Bind final Workflow/Correct GET + static reads to ExplicitRouteRegistry."""

    def workflow_ui(http, _params):
        return http.static(
            "agent_workflow_ui.js", "application/javascript; charset=utf-8"
        )

    def status(http, params):
        try:
            return http.send_json(
                200, manager.workflow_subject(unquote(params["ref"]))
            )
        except (TypeError, ValueError) as exc:
            return http.send_json(404, {"error": str(exc)})

    def correct_history(http, params):
        try:
            query = parse_qs(urlsplit(http.path).query)
            now = time.time()
            start = float((query.get("start") or [now - 600])[0])
            end = float((query.get("end") or [now])[0])
            compact_raw = str((query.get("compact") or ["0"])[0]).strip().lower()
            compact = compact_raw in {"1", "true", "yes", "on"}
            with connection_session(getattr(manager, "store", None)):
                history = manager.workflow_correct_history(
                    unquote(params["ref"]), start, end, compact=compact
                )
            return http.send_json(200, history)
        except (TypeError, ValueError) as exc:
            return http.send_json(404, {"error": str(exc)})

    def correct_point(http, params):
        try:
            query = parse_qs(urlsplit(http.path).query)
            ts = float((query.get("ts") or [time.time()])[0])
            with connection_session(getattr(manager, "store", None)):
                point = manager.workflow_correct_point(unquote(params["ref"]), ts)
            return http.send_json(200, point)
        except (TypeError, ValueError) as exc:
            return http.send_json(404, {"error": str(exc)})

    registry.register(
        "GET",
        "workflow.static",
        r"^/agent_workflow_ui\.js$",
        workflow_ui,
        require_trusted=True,
        require_runtime=False,
        priority=220,
    )
    registry.register(
        "GET",
        "workflow.status",
        r"^/api/agent-workflow/(?P<ref>[^/]+)/status$",
        status,
        require_trusted=True,
        require_runtime=True,
        priority=220,
    )
    registry.register(
        "GET",
        "workflow.correct_history",
        r"^/api/agent-workflow/(?P<ref>[^/]+)/correct-history$",
        correct_history,
        require_trusted=True,
        require_runtime=True,
        priority=220,
    )
    registry.register(
        "GET",
        "workflow.correct_point",
        r"^/api/agent-workflow/(?P<ref>[^/]+)/correct-point$",
        correct_point,
        require_trusted=True,
        require_runtime=True,
        priority=220,
    )
    return registry


def register_mutation_routes(registry, manager):
    """Bind final non-durable Workflow mutations to ExplicitRouteRegistry.

    Durable Correct commit admission is intentionally owned by workflow_request_queue.
    """

    def mutation(http, params, action):
        ref = unquote(params["ref"])
        try:
            payload = http.read_json()
            payload = payload if isinstance(payload, dict) else {}
            if action == "autonomous":
                return http.send_json(202, manager.workflow_autonomous(ref))
            if action == "correct-label":
                with connection_session(getattr(manager, "store", None)):
                    label = manager.workflow_add_correct_label(
                        ref, payload.get("desired_value"), payload.get("sample_ts")
                    )
                return http.send_json(200, label)
            if action == "correct-undo":
                return http.send_json(200, manager.workflow_undo_correct_label(ref))
            if action == "change-decision":
                return http.send_json(
                    202,
                    manager.workflow_change_decision(
                        ref, payload.get("desired_value")
                    ),
                )
            return http.send_json(404, {"error": "unsupported workflow action"})
        except ValueError as exc:
            return http.send_json(409, {"error": str(exc)})
        except Exception as exc:
            if action == "correct-label" and is_sqlite_busy(exc):
                return http.send_json(503, {"error": "SQLite is busy; retry saving this Correct point"})
            return http.send_json(
                500,
                {
                    "error": (
                        "Generation workflow failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                },
            )

    for action in (
        "autonomous",
        "correct-label",
        "correct-undo",
        "change-decision",
    ):
        registry.register(
            "POST",
            f"workflow.{action.replace('-', '_')}",
            rf"^/api/agent-workflow/(?P<ref>[^/]+)/{action}$",
            lambda http, params, action=action: mutation(http, params, action),
            require_trusted=True,
            require_runtime=True,
            priority=220,
        )
    return registry

def install(manager, *, legacy_get=True, legacy_post=True):
    if getattr(manager, "_agent_workflow_actions_installed", False):
        return manager

    ensure_workflow_tables(manager.store)
    _install_candidate_teaching_overlay(manager)
    manager._correct_creation = threading.local()

    original_start = manager._start_build
    original_finish = manager._finish_build_if_ready
    handler = manager.core.Handler
    original_get = handler.do_GET if legacy_get else None
    original_post = handler.do_POST if legacy_post else None

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
            **_correct_label_counts(manager, generation, agent),
        }

    def workflow_autonomous(ref):
        generation, _ = _resolve_generation(manager, ref)
        _preflight_child(manager, generation, allow_coalesce=False)
        return _create_or_coalesce_child(
            manager, generation, AUTONOMOUS_REASON, "autonomous", allow_coalesce=False
        )

    def workflow_is_explicit_correct(row):
        if str(row.get("reason") or "") != CORRECT_REASON:
            return False
        with manager.store.conn() as c:
            operation = c.execute(
                """SELECT detail_json FROM agent_correct_operations
                   WHERE candidate_id=? AND status='committed'
                   ORDER BY created_ts DESC LIMIT 1""",
                (row["candidate_id"],),
            ).fetchone()
        if operation is None:
            return False
        detail = _json(operation["detail_json"])
        return (detail.get("parent_model_identity") is not None
                and detail.get("feedback_revision") == row.get("feedback_revision"))

    def workflow_correct_parent_token(ref):
        from agent_correct_generation_history import _resolve_history_generation
        generation, agent = _resolve_history_generation(manager, ref)
        if generation.get("lifecycle_state") in {"discarded", "pruned", "promoted"}:
            raise ValueError("Generation model is no longer retained")
        return parent_token(generation, agent)

    def parent_token(generation, agent):
        model = manager.store.get_model(agent["id"])
        if model is None:
            raise ValueError("Generation has no usable model")
        return {
            "generation_id": str(generation["generation_id"]),
            "generation_number": int(generation["generation_number"]),
            "parent_model_identity": model_metadata(model)["model_identity"],
            "parent_config_fingerprint": config_fingerprint(agent),
        }

    def workflow_correct_commit(ref, request_id=None, *, admitted_parent=None):
        generation, agent = _resolve_generation(manager, ref)
        fp = rl_fingerprint(agent)
        operation_id = str(request_id or uuid.uuid4())

        # A durable request may be replayed after a crash between Candidate creation
        # and workflow-request completion. Return the already committed operation
        # instead of requiring new labels or creating another generation action.
        if request_id is not None:
            with manager.store.conn() as c:
                existing_operation = c.execute(
                    "SELECT * FROM agent_correct_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            if existing_operation:
                existing_operation = dict(existing_operation)
                if str(existing_operation.get("parent_generation_id")) != str(generation["generation_id"]):
                    raise ValueError("Correct operation ID belongs to a different generation")
                if (
                    str(existing_operation.get("status") or "") == "committed"
                    and existing_operation.get("child_generation_id")
                ):
                    detail = _json(existing_operation.get("detail_json"), {})
                    child_generation_id = str(existing_operation["child_generation_id"])
                    child_status = (
                        manager.lineage_status(child_generation_id)
                        if hasattr(manager, "lineage_status") else None
                    )
                    return {
                        "ok": True,
                        "action": "correct",
                        "action_id": detail.get("action_id"),
                        "coalesced": bool(detail.get("coalesced")),
                        "parent_generation_id": str(generation["generation_id"]),
                        "child_generation_id": child_generation_id,
                        "child": child_status,
                        "correct_operation_id": operation_id,
                        "correct_label_ids": [
                            int(value) for value in json.loads(
                                existing_operation.get("label_ids_json") or "[]"
                            )
                        ],
                        "idempotent_replay": True,
                    }

        _preflight_child(manager, generation, allow_coalesce=True)
        retry_failed_child = _failed_correct_child(manager, generation)
        selected_parent = parent_token(generation, agent)
        if not verify_model_checksum(manager.store.get_model(agent["id"])):
            raise ValueError("NEEDS_RETRAIN: policy model checksum mismatch in Correct parent")
        if admitted_parent is not None and admitted_parent != selected_parent:
            raise ValueError("Correct parent changed after admission; retry on the current generation")

        # A Correct operation consists of labels created/edited since the preceding
        # committed Correct operation on this exact generation. The first post-upgrade
        # operation adopts existing active labels for backward compatibility.
        with manager.store.lock, manager.store.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            previous = c.execute(
                """SELECT COALESCE(json_extract(detail_json,'$.label_cutoff_ts'),committed_ts) AS label_cutoff
               FROM agent_correct_operations
                   WHERE parent_generation_id=? AND status='committed'
                   ORDER BY committed_ts DESC,created_ts DESC LIMIT 1""",
                (str(generation["generation_id"]),),
            ).fetchone()
            cutoff = float(previous["label_cutoff"]) if previous and previous["label_cutoff"] is not None else None
            if generation.get("generation_type") == "candidate" and not retry_failed_child:
                born = float(generation["created_ts"])
                cutoff = max(born, cutoff) if cutoff is not None else born
            if retry_failed_child:
                cutoff = None
            label_sql = """SELECT id FROM teaching_rl_labels
                           WHERE agent_id=? AND undone_ts IS NULL AND fingerprint=?
                             AND (? IS NULL OR created_ts>?) AND created_ts<=?
                           ORDER BY id"""
            label_ids = [int(row["id"]) for row in c.execute(
                label_sql, (str(agent["id"]), fp, cutoff, cutoff, now)
            )]
            if not label_ids and request_id is None:
                label_ids = [int(row["id"]) for row in c.execute(
                    label_sql, (str(agent["id"]), fp, None, None, now)
                )]
            if not label_ids:
                raise ValueError("Add at least one new Correct point before creating the child Candidate")
            c.execute(
                """INSERT OR REPLACE INTO agent_correct_operations
                   (operation_id,root_agent_id,parent_generation_id,parent_agent_id,
                    child_generation_id,candidate_id,label_ids_json,created_ts,
                    committed_ts,status,detail_json)
                   VALUES(?,?,?,?,NULL,NULL,?,?,NULL,'prepared',?)""",
                (
                    operation_id,
                    str(generation["root_agent_id"]),
                    str(generation["generation_id"]),
                    str(agent["id"]),
                    json.dumps(label_ids, separators=(",", ":")),
                    now,
                    json.dumps(selected_parent),
                ),
            )
        try:
            with manager.lock:
                manager._correct_creation.defer_snapshot = True
                try:
                    result = _create_or_coalesce_child(
                        manager, generation, CORRECT_REASON, "correct", allow_coalesce=True
                    )
                finally:
                    del manager._correct_creation.defer_snapshot
                feedback_revision = (manager._candidate_row(agent["id"]) or {}).get("feedback_revision")
            child_generation_id = (result or {}).get("child_generation_id")
            child_generation = (
                lineage_row(manager.store, generation_id=str(child_generation_id))
                if child_generation_id else None
            )
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_correct_operations
                       SET child_generation_id=?,candidate_id=?,committed_ts=?,
                           status='committed',detail_json=?
                       WHERE operation_id=?""",
                    (
                        str(child_generation_id or ""),
                        str((child_generation or {}).get("agent_id") or ""),
                        time.time(),
                        json.dumps({
                            "label_count": len(label_ids),
                            "feedback_revision": feedback_revision,
                            "coalesced": bool((result or {}).get("coalesced")),
                            "action_id": (result or {}).get("action_id"),
                            "label_cutoff_ts": now,
                            **selected_parent,
                        }, separators=(",", ":")),
                        operation_id,
                    ),
                )
            result = dict(result or {})
            result["correct_operation_id"] = operation_id
            result["correct_label_ids"] = list(label_ids)
            manager.wake_event.set()
            return result
        except Exception as exc:
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_correct_operations
                       SET committed_ts=?,status='failed',detail_json=?
                       WHERE operation_id=?""",
                    (
                        time.time(),
                        json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
                        operation_id,
                    ),
                )
            raise

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
        reason = str(row.get("reason") or "")
        if reason == CORRECT_REASON:
            with manager.store.conn() as c:
                prepared = c.execute(
                    """SELECT 1 FROM agent_correct_operations
                       WHERE parent_agent_id=? AND status='prepared' LIMIT 1""",
                    (row["parent_agent_id"],),
                ).fetchone()
            if prepared:
                return False
            fresh = manager._candidate_row(row["parent_agent_id"])
            if not fresh or fresh["candidate_id"] != row["candidate_id"] or fresh["state"] != "queued":
                return False
            row = fresh
        if reason != AUTONOMOUS_REASON:
            return original_start(row)
        queue = manager._queue()
        if queue is None:
            return False
        candidate = manager.store.get_agent_config(row["candidate_id"])
        parent = manager.store.get_agent_config(row["parent_agent_id"])
        if not candidate or not parent:
            manager._fail(row, "candidate or parent generation disappeared")
            return True
        existing = queue.status_for(candidate["id"])
        if existing and str(existing.get("state") or "") == "active":
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
            queued, queue_claim = manager._claim_candidate_training_job(
                candidate["id"], rebuild=False, reason="autonomous_continuation"
            )
            if queued is None:
                return False
            manager.store.event(
                row["parent_agent_id"], "info", "agent_autonomous_continuation_started",
                "Autonomous child continues from the parent snapshot without clear_learning",
                {
                    "candidate_id": candidate["id"], "queue": queued,
                    "queue_claim": queue_claim,
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
    manager.workflow_correct_inventory = lambda generation, agent: _correct_label_counts(manager, generation, agent)
    manager.workflow_autonomous = workflow_autonomous
    manager.workflow_is_explicit_correct = workflow_is_explicit_correct
    manager.workflow_correct_parent_token = workflow_correct_parent_token
    manager.workflow_correct_commit = workflow_correct_commit
    manager.workflow_change_decision = workflow_change_decision
    manager.workflow_correct_history = lambda ref, start, end, compact=False: _correct_history(
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
    if legacy_get:
        handler.do_GET = do_get
    if legacy_post:
        handler.do_POST = do_post
    manager._agent_workflow_actions_installed = True
    manager.agent_workflow_contract = "autonomous_correct_change_decision_generation_children_parent_model_immutable"
    manager.agent_workflow_coalescing_contract = "same_parent_generation_only"
    manager.agent_autonomous_contract = "exact_parent_snapshot_resume_history_no_clear_learning_schema_preserving"
    manager.workflow_read_http_contract = (
        "explicit_registry_final_composition_legacy_get_optional"
    )
    manager.workflow_mutation_http_contract = (
        "explicit_registry_final_composition_legacy_post_optional_"
        "durable_correct_owned_by_workflow_request_queue"
    )
    return manager
