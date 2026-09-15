"""Generation-aware Explore built on the existing Experiments and Sensor Tournament.

This module is orchestration only, never a second learning subsystem:

* Free exploration configures the already-installed ``engine.experiments`` residual
  learner. A physical micro-probe can still be created only by the normal Live policy
  path and dispatched only by Executor after the existing confidence, support, novelty,
  device, interval, budget and observation guards.
* Targeted sensor pins a user-selected HA entity into the already-installed Sensor
  Tournament challenger set for the child Candidate. Manual selection changes priority,
  never evidence: prequential future samples, availability/quality, predictive gain and
  promotion safety remain authoritative.

Each Explore request owns a direct child generation. The selected parent is immutable.
Candidate Shadow never creates an ActionIntent and never calls Executor/HA services.
"""
from __future__ import annotations

import json
import time
import uuid
from urllib.parse import unquote, urlsplit

from agent_candidate_conservative_correct import (
    _benchmark_stats,
    _copy_parent_snapshot,
    _offline_gate,
    _persist_gate,
)
from agent_candidate_lineage import _refresh_generation_metadata, _row as lineage_row
from agent_workflow_actions import (
    _create_or_coalesce_child,
    _preflight_child,
    _resolve_generation,
)
from context_tournament_promotion import tournament_config
from settings import OPTIONS


FREE_MODE = "free"
TARGETED_MODE = "targeted_sensor"
FREE_REASON = "explore_free"
FREE_TRAIN_REASON = "explore_free_training"
TARGETED_REASON = "explore_targeted_sensor"
TARGETED_ACTIVE_STATES = {"evaluating"}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _table_exists(store, name):
    try:
        with store.conn() as c:
            return bool(c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
            ).fetchone())
    except Exception:
        return False


def ensure_explore_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_explore_sessions (
                session_id TEXT PRIMARY KEY,
                root_agent_id TEXT NOT NULL,
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL UNIQUE,
                live_owner_agent_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                targeted_sensor TEXT,
                status TEXT NOT NULL,
                requested_config_json TEXT NOT NULL DEFAULT '{}',
                previous_config_json TEXT NOT NULL DEFAULT '{}',
                outcome_count INTEGER NOT NULL DEFAULT 0,
                measured_outcomes INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_explore_parent
                ON agent_explore_sessions(parent_generation_id,created_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_explore_root
                ON agent_explore_sessions(root_agent_id,created_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_explore_live_owner
                ON agent_explore_sessions(live_owner_agent_id,status);
            """
        )


def _row(store, session_id):
    if not _table_exists(store, "agent_explore_sessions"):
        return None
    with store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_explore_sessions WHERE session_id=?", (str(session_id),)
        ).fetchone()
    return dict(row) if row else None


def _latest_for_parent(store, parent_generation_id):
    if not _table_exists(store, "agent_explore_sessions"):
        return None
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM agent_explore_sessions WHERE parent_generation_id=?
               ORDER BY created_ts DESC LIMIT 1""",
            (str(parent_generation_id),),
        ).fetchone()
    return dict(row) if row else None


def _session_for_child(store, child_generation_id):
    # Candidate Shadow is a process-global module and can be exercised in tests/runtime
    # instances where Explore was not installed. Absence of this additive table therefore
    # means simply "no Explore session", never a Shadow failure.
    if not _table_exists(store, "agent_explore_sessions"):
        return None
    with store.conn() as c:
        row = c.execute(
            "SELECT * FROM agent_explore_sessions WHERE child_generation_id=?",
            (str(child_generation_id),),
        ).fetchone()
    return dict(row) if row else None


def _session_for_child_agent(store, agent_id, statuses=None, mode=None):
    if not _table_exists(store, "agent_explore_sessions"):
        return None
    clauses = ["g.agent_id=?"]
    args = [str(agent_id)]
    if statuses:
        values = sorted(str(x) for x in statuses)
        clauses.append("s.status IN (%s)" % ",".join("?" for _ in values))
        args.extend(values)
    if mode:
        clauses.append("s.mode=?")
        args.append(str(mode))
    with store.conn() as c:
        row = c.execute(
            """SELECT s.* FROM agent_explore_sessions s
               JOIN agent_candidate_generations g ON g.generation_id=s.child_generation_id
               WHERE %s ORDER BY s.created_ts DESC LIMIT 1""" % " AND ".join(clauses),
            args,
        ).fetchone()
    return dict(row) if row else None


def _active_free_for_live(store, root_agent_id):
    if not _table_exists(store, "agent_explore_sessions"):
        return None
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM agent_explore_sessions
               WHERE live_owner_agent_id=? AND mode=? AND status='collecting'
               ORDER BY created_ts DESC LIMIT 1""",
            (str(root_agent_id), FREE_MODE),
        ).fetchone()
    return dict(row) if row else None


def _merge_result(store, session_id, *, status=None, patch=None, outcome_inc=0, measured_inc=0):
    session = _row(store, session_id)
    if not session:
        return None
    result = _json(session.get("result_json"), {})
    result.update(dict(patch or {}))
    now = time.time()
    with store.lock, store.conn() as c:
        c.execute(
            """UPDATE agent_explore_sessions SET status=?,outcome_count=outcome_count+?,
               measured_outcomes=measured_outcomes+?,result_json=?,updated_ts=? WHERE session_id=?""",
            (
                str(status or session["status"]), int(outcome_inc), int(measured_inc),
                json.dumps(result, separators=(",", ":"), sort_keys=True, default=str),
                now, str(session_id),
            ),
        )
    return _row(store, session_id)


def _edge(manager, child_generation_id):
    child = lineage_row(manager.store, generation_id=child_generation_id)
    if not child or not child.get("parent_generation_id") or not child.get("agent_id"):
        return None, None, None
    parent = lineage_row(manager.store, generation_id=child["parent_generation_id"])
    if not parent or not parent.get("agent_id"):
        return None, parent, child
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT * FROM agent_candidates WHERE parent_agent_id=? AND candidate_id=?""",
            (str(parent["agent_id"]), str(child["agent_id"])),
        ).fetchone()
    return (dict(row) if row else None), parent, child


def _mark_edge(manager, child_generation_id, state, *, reason=None, lineage_state=None, dirty=None):
    edge, parent, child = _edge(manager, child_generation_id)
    if not edge or not parent or not child:
        raise RuntimeError("Explore child edge disappeared")
    updates = ["state=?", "updated_ts=?"]
    args = [str(state), time.time()]
    if reason is not None:
        updates.append("reason=?")
        args.append(str(reason))
    if dirty is not None:
        updates.append("dirty=?")
        args.append(int(bool(dirty)))
    args.extend([str(parent["agent_id"]), str(child["agent_id"])])
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            "UPDATE agent_candidates SET %s WHERE parent_agent_id=? AND candidate_id=?" % ",".join(updates),
            args,
        )
        if state == "exploring":
            c.execute(
                """UPDATE agent_candidates SET build_revision=feedback_revision,dirty=0
                   WHERE parent_agent_id=? AND candidate_id=?""",
                (str(parent["agent_id"]), str(child["agent_id"])),
            )
        c.execute(
            """UPDATE agent_candidate_generations SET lifecycle_state=?,updated_ts=?
               WHERE generation_id=?""",
            (str(lineage_state or state), time.time(), str(child_generation_id)),
        )
    return manager.lineage_status(child_generation_id) if hasattr(manager, "lineage_status") else child


def _insert_session(manager, parent, child_generation_id, *, mode, sensor=None,
                    requested=None, previous=None, status=None):
    session_id = str(uuid.uuid4())
    now = time.time()
    status = status or ("collecting" if mode == FREE_MODE else "evaluating")
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """INSERT INTO agent_explore_sessions
               (session_id,root_agent_id,parent_generation_id,child_generation_id,
                live_owner_agent_id,mode,targeted_sensor,status,requested_config_json,
                previous_config_json,result_json,created_ts,updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, str(parent["root_agent_id"]), str(parent["generation_id"]),
                str(child_generation_id), str(parent["root_agent_id"]), str(mode),
                None if sensor is None else str(sensor), str(status),
                json.dumps(requested or {}, separators=(",", ":"), sort_keys=True),
                json.dumps(previous or {}, separators=(",", ":"), sort_keys=True),
                json.dumps({
                    "message": "collecting bounded Explore evidence" if mode == FREE_MODE
                    else "evaluating targeted sensor with future prequential evidence",
                    "candidate_dispatch": False,
                    "probe_owner": "live_executor_only" if mode == FREE_MODE else "passive_shadow",
                }, separators=(",", ":"), sort_keys=True),
                now, now,
            ),
        )
    return _row(manager.store, session_id)


def _update_action_detail(manager, action_id, values):
    if not action_id:
        return
    with manager.store.lock, manager.store.conn() as c:
        row = c.execute(
            "SELECT detail_json FROM agent_generation_actions WHERE id=?", (int(action_id),)
        ).fetchone()
        if not row:
            return
        detail = _json(row["detail_json"], {})
        detail.update(dict(values or {}))
        c.execute(
            "UPDATE agent_generation_actions SET detail_json=? WHERE id=?",
            (json.dumps(detail, separators=(",", ":"), sort_keys=True, default=str), int(action_id)),
        )


def _active_probe_available(root_live):
    return bool(
        root_live and root_live.get("enabled") and root_live.get("mode") == "control"
        and root_live.get("training_state") == "qualified"
    )


def _session_payload(manager, session):
    if not session:
        return None
    child = lineage_row(manager.store, generation_id=session["child_generation_id"])
    root = manager.store.get_agent_config(str(session["live_owner_agent_id"]))
    result = _json(session.get("result_json"), {})
    payload = {
        "session_id": session["session_id"], "mode": session["mode"],
        "status": session["status"], "root_agent_id": session["root_agent_id"],
        "parent_generation_id": session["parent_generation_id"],
        "child_generation_id": session["child_generation_id"],
        "child_generation_number": int(child["generation_number"]) if child else None,
        "targeted_sensor": session.get("targeted_sensor"),
        "outcome_count": int(session.get("outcome_count") or 0),
        "measured_outcomes": int(session.get("measured_outcomes") or 0),
        "requested_config": _json(session.get("requested_config_json"), {}),
        "result": result, "result_message": result.get("message"),
        "candidate_dispatch": False,
        "active_probe_available": _active_probe_available(root),
        "probe_owner": "live_executor_only" if session["mode"] == FREE_MODE else "passive_shadow",
        "created_ts": float(session["created_ts"]), "updated_ts": float(session["updated_ts"]),
    }
    if session["mode"] == FREE_MODE:
        payload["experiment_status"] = manager.engine.experiments.status(str(session["live_owner_agent_id"]))
    return payload


def _validate_target_sensor(manager, parent_agent, sensor):
    sensor = str(sensor or "").strip()
    if not sensor or "." not in sensor:
        raise ValueError("Choose a Home Assistant entity for Targeted sensor")
    with manager.engine.lock:
        state = dict(manager.engine.state_map).get(sensor)
    if not state:
        raise ValueError("Targeted sensor is not currently available in Home Assistant")
    if str(state.get("state") or "").strip().lower() in ("", "unknown", "unavailable", "none"):
        raise ValueError("Targeted sensor is currently unavailable")
    service = getattr(manager.engine, "context_tournament", None)
    if service is None:
        raise ValueError("Sensor Tournament is unavailable")
    try:
        policy = manager.engine.models.get(str(parent_agent["id"])) or manager.engine.policy(parent_agent)
        active = list(getattr(getattr(policy, "schema", None), "entities", []) or [])
    except Exception:
        active = list((manager.store.get_model(parent_agent["id"]) or {}).get("schema", {}).get("entities") or [])
    if sensor in active:
        raise ValueError("This sensor is already active in the selected parent schema")
    eligible = service._eligible_entities(parent_agent, active)
    if eligible is not None and sensor not in eligible:
        raise ValueError("The selected entity is not eligible context for this agent")
    return sensor


def _force_challenger_state(service, agent, sensor, state=None):
    """Pin a hypothesis into selection priority, never into evidence."""
    state = dict(state or service.state(agent["id"]))
    active = [str(x) for x in state.get("active_features") or []]
    if sensor in active:
        return state
    limit = max(1, int(OPTIONS.get("context_challenger_count", 4)))
    challengers = [str(sensor)] + [
        str(x) for x in state.get("challenger_features") or [] if str(x) != str(sensor)
    ]
    challengers = challengers[:limit]
    state["challenger_features"] = challengers
    state["last_evaluation"] = state.get("last_evaluation") or time.time()
    with service.store.lock, service.store.conn() as c:
        c.execute(
            """INSERT INTO context_tournament_state
               (agent_id,active_features_json,challenger_features_json,feature_scores_json,
                last_evaluation,schema_revision,previous_schema_json,updated_ts)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 challenger_features_json=excluded.challenger_features_json,
                 last_evaluation=excluded.last_evaluation,updated_ts=excluded.updated_ts""",
            (
                str(agent["id"]), json.dumps(active, separators=(",", ":")),
                json.dumps(challengers, separators=(",", ":")),
                json.dumps(state.get("feature_scores") or {}, separators=(",", ":"), sort_keys=True),
                state.get("last_evaluation"), int(state.get("schema_revision") or 0),
                json.dumps(state.get("previous_schema") or [], separators=(",", ":")), time.time(),
            ),
        )
    with service.lock:
        service._cache[str(agent["id"])] = dict(state)
    return state


def _queue_free_training(manager, session):
    edge, parent, child = _edge(manager, session["child_generation_id"])
    if not edge or not parent or not child:
        return _merge_result(manager.store, session["session_id"], status="failed",
                             patch={"message": "Explore child disappeared before continuation training"})
    now = time.time()
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """UPDATE agent_candidates SET state='queued',reason=?,dirty=1,
               feedback_revision=feedback_revision+1,queued_ts=?,last_error=NULL,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=?""",
            (FREE_TRAIN_REASON, now, now, str(parent["agent_id"]), str(child["agent_id"])),
        )
        c.execute(
            "UPDATE agent_candidate_generations SET lifecycle_state='queued',updated_ts=? WHERE generation_id=?",
            (now, str(child["generation_id"])),
        )
    updated = _merge_result(
        manager.store, session["session_id"], status="training",
        patch={"message": "Free exploration evidence collected; child continuation training queued",
               "training_mode": "resume_from_parent_cursor"},
    )
    manager.wake_event.set()
    return updated


def _restore_previous_experiment_config(manager, session):
    root = manager.store.get_agent_config(str(session["live_owner_agent_id"]))
    previous = _json(session.get("previous_config_json"), {})
    if not root or not previous:
        return
    try:
        manager.engine.experiments.configure(root, previous)
    except Exception as exc:
        manager.store.event(
            root["id"], "warning", "explore_restore_experiment_config_failed",
            "Explore completed but the previous Experiments configuration could not be restored",
            {"error": f"{type(exc).__name__}: {exc}"},
        )


def _finish_free_outcome(manager, session, reward, reason):
    updated = _merge_result(
        manager.store, session["session_id"], outcome_inc=1, measured_inc=int(reward is not None),
        patch={"last_outcome_reason": str(reason), "last_reward": reward,
               "message": "collecting bounded Explore evidence"},
    )
    if not updated:
        return
    budget = max(1, int(_json(updated.get("requested_config_json"), {}).get("daily_budget") or 1))
    if int(updated.get("outcome_count") or 0) < budget:
        return
    _restore_previous_experiment_config(manager, updated)
    if int(updated.get("measured_outcomes") or 0) <= 0:
        _mark_edge(manager, updated["child_generation_id"], "insufficient_evidence",
                   reason=FREE_REASON, lineage_state="insufficient_evidence", dirty=False)
        _merge_result(manager.store, updated["session_id"], status="no_evidence",
                      patch={"message": "Free exploration finished without a measurable labelled outcome"})
        return
    _queue_free_training(manager, updated)


def _start_free_training(manager, row):
    queue = manager._queue()
    if queue is None or queue.status_for(row["candidate_id"]):
        return False
    parent = manager.store.get_agent_config(str(row["parent_agent_id"]))
    candidate = manager.store.get_agent_config(str(row["candidate_id"]))
    if not parent or not candidate:
        manager._fail(row, "Explore parent or child disappeared")
        return True
    try:
        candidate = _copy_parent_snapshot(manager, parent["id"], candidate["id"])
        now = time.time()
        build_revision = int(row.get("feedback_revision") or 0)
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='building',build_revision=?,dirty=0,
                   build_started_ts=?,build_finished_ts=NULL,comparison_started_ts=NULL,
                   comparison_json='{}',offline_gate_json='{}',last_error=NULL,updated_ts=?
                   WHERE parent_agent_id=? AND candidate_id=?""",
                (build_revision, now, now, str(parent["id"]), str(candidate["id"])),
            )
            c.execute(
                "UPDATE agent_candidate_generations SET lifecycle_state='building',updated_ts=? WHERE agent_id=?",
                (now, str(candidate["id"])),
            )
        queued = queue.enqueue(candidate["id"], rebuild=False, reason="explore_free_continuation")
        manager.store.event(
            row["parent_agent_id"], "info", "agent_explore_free_training_started",
            "Free Explore child continues from the direct-parent cursor without clear_learning",
            {"candidate_id": candidate["id"], "queue": queued, "rebuild": False},
        )
        return True
    except Exception as exc:
        manager._fail(row, f"Explore continuation failed: {type(exc).__name__}: {exc}")
        return True


def _finish_free_training(manager, row):
    fresh = manager._candidate_row(row["parent_agent_id"]) or row
    if str(fresh.get("state") or "") not in ("comparing", "ready"):
        return
    child_generation = lineage_row(manager.store, agent_id=fresh.get("candidate_id"))
    session = _session_for_child(manager.store, (child_generation or {}).get("generation_id"))
    if not session or session.get("mode") != FREE_MODE:
        return
    parent = manager.store.get_agent_config(str(fresh["parent_agent_id"]))
    child = manager.store.get_agent_config(str(fresh["candidate_id"]))
    if not parent or not child or manager.store.get_model(child["id"]) is None:
        return
    try:
        report = {"mode": "free_exploration_continuation",
                  "measured_outcomes": int(session.get("measured_outcomes") or 0),
                  "outcome_count": int(session.get("outcome_count") or 0)}
        gate = _offline_gate(parent, _benchmark_stats(parent), _benchmark_stats(child), report)
        state = _persist_gate(manager, fresh, gate)
        _refresh_generation_metadata(manager.store, child["id"], lifecycle_state=state,
                                     comparison_json=fresh.get("comparison_json") or "{}")
        _merge_result(
            manager.store, session["session_id"],
            status="complete" if gate.get("passed") else "blocked",
            patch={"message": "Free exploration child is ready for paired comparison" if gate.get("passed")
                              else "Free exploration child did not pass the offline regression gate",
                   "offline_gate": gate},
        )
    except Exception as exc:
        _merge_result(manager.store, session["session_id"], status="failed",
                      patch={"message": f"Free exploration final gate failed: {type(exc).__name__}: {exc}"})


def _target_row(service, agent, sensor):
    status = service.shadow_status(agent)
    for row in status.get("challengers") or []:
        if str(row.get("entity_id")) == str(sensor):
            return dict(row), status
    return None, status


def _finalize_targeted_success(manager, session, evidence):
    edge, parent_generation, child_generation = _edge(manager, session["child_generation_id"])
    if not edge or not parent_generation or not child_generation:
        return
    parent = manager.store.get_agent_config(str(parent_generation["agent_id"]))
    child = manager.store.get_agent_config(str(child_generation["agent_id"]))
    if not parent or not child:
        return
    try:
        report = {"mode": "targeted_sensor", "sensor": session.get("targeted_sensor"),
                  "future_prequential": True, "predictive_gain": (evidence or {}).get("gain"),
                  "sensor_quality": (evidence or {}).get("sensor_quality")}
        gate = _offline_gate(parent, _benchmark_stats(parent), _benchmark_stats(child), report)
        state = _persist_gate(manager, edge, gate)
        _refresh_generation_metadata(manager.store, child["id"], lifecycle_state=state,
                                     comparison_json=edge.get("comparison_json") or "{}")
        _merge_result(
            manager.store, session["session_id"],
            status="complete" if gate.get("passed") else "blocked",
            patch={"message": "targeted sensor proved incremental value" if gate.get("passed")
                              else "sensor gain was proven but the child Candidate failed its offline regression gate",
                   "sensor": session.get("targeted_sensor"), "evidence": evidence or {}, "offline_gate": gate},
        )
    except Exception as exc:
        _merge_result(manager.store, session["session_id"], status="failed",
                      patch={"message": f"Targeted sensor final gate failed: {type(exc).__name__}: {exc}"})


def _evaluate_targeted(manager, session, agent, service):
    if not session or session.get("status") not in TARGETED_ACTIVE_STATES:
        return _session_payload(manager, session)
    sensor = str(session.get("targeted_sensor") or "")
    tournament_state = service.state(agent["id"])
    if sensor in set(str(x) for x in tournament_state.get("active_features") or []):
        evidence, _ = _target_row(service, agent, sensor)
        _finalize_targeted_success(manager, session, evidence or {})
        return _session_payload(manager, _row(manager.store, session["session_id"]))

    evidence, _ = _target_row(service, agent, sensor)
    if not evidence:
        _merge_result(manager.store, session["session_id"],
                      patch={"message": "targeted sensor is selected; waiting for future observations"})
        return _session_payload(manager, _row(manager.store, session["session_id"]))

    cfg = tournament_config()
    samples = int(evidence.get("samples") or 0)
    days = float(evidence.get("days_observed") or 0.0)
    gain = evidence.get("gain")
    patch = {"message": "evaluating targeted sensor with future paired evidence", "sensor": sensor,
             "evidence": evidence, "tournament_contract": "predict -> score -> learn",
             "manual_priority_is_evidence_override": False}
    _merge_result(manager.store, session["session_id"], patch=patch)
    enough = samples >= int(cfg["min_samples"]) and days >= float(cfg["min_days"])
    if enough and gain is not None and float(gain) < float(cfg["min_gain"]):
        _mark_edge(manager, session["child_generation_id"], "insufficient_evidence",
                   reason=TARGETED_REASON, lineage_state="insufficient_evidence", dirty=False)
        _merge_result(
            manager.store, session["session_id"], status="no_gain",
            patch={"message": "no measurable gain",
                   "reason": "predictive_gain_below_existing_tournament_threshold",
                   "evidence": evidence, "required_gain": float(cfg["min_gain"])},
        )
    elif enough and evidence.get("promotion_blocked_reason") == "sensor_quality":
        _mark_edge(manager, session["child_generation_id"], "insufficient_evidence",
                   reason=TARGETED_REASON, lineage_state="insufficient_evidence", dirty=False)
        _merge_result(
            manager.store, session["session_id"], status="quality_blocked",
            patch={"message": "targeted sensor failed the existing availability/quality safety gate",
                   "sensor_quality": evidence.get("sensor_quality"), "evidence": evidence},
        )
    return _session_payload(manager, _row(manager.store, session["session_id"]))


def _install_shadow_overlay():
    """Install one process-global trampoline; all runtime dependencies are resolved per call.

    Unit tests and restart simulations create several managers in one Python process. The
    previous closure-per-manager implementation stacked wrappers and leaked an old store or
    tournament into later runtimes. A single trampoline is both cheaper and correct.
    """
    try:
        import agent_candidate_shadow_runtime as shadow_module
    except Exception:
        return
    if getattr(shadow_module, "_explore_targeted_overlay_installed", False):
        return
    base_predict = shadow_module._predict_candidate

    def predict_candidate_with_targeted_explore(active_manager, generation, state_map, event_ts):
        result = base_predict(active_manager, generation, state_map, event_ts)
        if not result:
            return result
        session = _session_for_child(active_manager.store, generation.get("generation_id"))
        if not session or session.get("mode") != TARGETED_MODE or session.get("status") not in TARGETED_ACTIVE_STATES:
            return result
        service = getattr(active_manager.engine, "context_tournament", None)
        if service is None:
            return result
        agent = active_manager.store.get_agent_config(str(generation.get("agent_id") or ""))
        if not agent:
            return result
        try:
            rt = active_manager.engine.runtime.setdefault(str(agent["id"]), {})
            rt["last_prediction"] = float(result["desired"])
            rt["last_confidence"] = result.get("confidence")
            root_rt = active_manager.engine.runtime.get(str(generation["root_agent_id"])) or {}
            rt["last_change_origin"] = root_rt.get("last_change_origin")
            # Passive only: this updates Tournament prediction/evidence state and never
            # constructs an ActionIntent or invokes Executor.
            service.observe_shadow(agent, state_map, None)
            _evaluate_targeted(active_manager, session, agent, service)
        except Exception as exc:
            active_manager.store.event(
                generation["root_agent_id"], "warning", "targeted_explore_shadow_gap",
                "Targeted sensor Explore skipped one passive Candidate observation",
                {"generation_id": generation["generation_id"], "sensor": session.get("targeted_sensor"),
                 "error": f"{type(exc).__name__}: {exc}"},
            )
        return result

    shadow_module._predict_candidate = predict_candidate_with_targeted_explore
    shadow_module._explore_targeted_overlay_installed = True


def install(manager):
    if getattr(manager, "_agent_explore_installed", False):
        return manager
    ensure_explore_tables(manager.store)
    service = getattr(manager.engine, "context_tournament", None)
    experiments = getattr(manager.engine, "experiments", None)
    if service is None or experiments is None:
        manager.agent_explore_contract = "unavailable_existing_subsystems_required"
        return manager

    handler = manager.core.Handler
    original_get = handler.do_GET
    original_post = handler.do_POST
    original_start = manager._start_build
    original_finish_build = manager._finish_build_if_ready
    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)
    original_sync = service.sync_agent
    original_experiment_finish = experiments._finish

    def forced_sync(agent, **kwargs):
        state = original_sync(agent, **kwargs)
        session = _session_for_child_agent(
            manager.store, agent["id"], statuses=TARGETED_ACTIVE_STATES, mode=TARGETED_MODE
        )
        if not session:
            return state
        return _force_challenger_state(service, agent, str(session.get("targeted_sensor") or ""), state)

    service.sync_agent = forced_sync
    _install_shadow_overlay()

    def experiment_finish(aid, reward, reason):
        session = _active_free_for_live(manager.store, aid)
        result = original_experiment_finish(aid, reward, reason)
        if session:
            _finish_free_outcome(manager, session, reward, reason)
        return result

    experiments._finish = experiment_finish

    def start_build(row):
        reason = str(row.get("reason") or "")
        if reason in (FREE_REASON, TARGETED_REASON):
            generation = lineage_row(manager.store, agent_id=row.get("candidate_id"))
            if generation:
                _mark_edge(manager, generation["generation_id"], "exploring",
                           reason=reason, lineage_state="comparing", dirty=False)
            return True
        if reason == FREE_TRAIN_REASON:
            return _start_free_training(manager, row)
        return original_start(row)

    def finish_build(row):
        result = original_finish_build(row)
        if result and str(row.get("reason") or "") == FREE_TRAIN_REASON:
            _finish_free_training(manager, row)
        return result

    manager._start_build = start_build
    manager._finish_build_if_ready = finish_build

    def create_explore_child(parent_generation, reason, mode, *, sensor=None, requested=None, previous=None):
        created = _create_or_coalesce_child(
            manager, parent_generation, reason, "explore", allow_coalesce=False
        )
        child_gid = created["child_generation_id"]
        session = _insert_session(manager, parent_generation, child_gid, mode=mode, sensor=sensor,
                                  requested=requested, previous=previous)
        _mark_edge(manager, child_gid, "exploring", reason=reason,
                   lineage_state="comparing", dirty=False)
        _update_action_detail(manager, created.get("action_id"),
                              {"explore_mode": mode, "targeted_sensor": sensor,
                               "session_id": session["session_id"], "candidate_dispatch": False})
        return created, session

    def workflow_explore(ref, payload):
        generation, agent = _resolve_generation(manager, ref)
        _preflight_child(manager, generation, allow_coalesce=False)
        body = dict(payload or {})
        mode = str(body.get("mode") or "").strip()

        if mode == FREE_MODE:
            root = manager.store.get_agent_config(str(generation["root_agent_id"]))
            if not root:
                raise ValueError("Root Live agent is unavailable")
            previous = dict((experiments.status(root["id"]).get("config") or {}))
            requested = dict(previous)
            config = body.get("config") if isinstance(body.get("config"), dict) else body
            for key in ("focus", "intensity", "interval", "daily_budget", "observation_seconds", "max_step"):
                if key in config:
                    requested[key] = config[key]
            requested["enabled"] = True
            # Existing Experiments validation owns every bound and safety parameter.
            experiments.configure(root, requested)
            try:
                created, session = create_explore_child(
                    generation, FREE_REASON, FREE_MODE, requested=requested, previous=previous
                )
            except Exception:
                try:
                    experiments.configure(root, previous)
                finally:
                    raise
            manager.engine.wake_event.set()
            manager.store.event(
                generation["root_agent_id"], "info", "agent_explore_free_started",
                "Free Explore uses the existing residual learner; physical probes remain Live/Executor-only",
                {"parent_generation_id": generation["generation_id"],
                 "child_generation_id": created["child_generation_id"],
                 "session_id": session["session_id"], "config": requested,
                 "active_probe_available": _active_probe_available(root)},
            )
            return {"ok": True, "action": "explore", "mode": FREE_MODE,
                    "parent_generation_id": generation["generation_id"],
                    "child_generation_id": created["child_generation_id"],
                    "session": _session_payload(manager, session)}

        if mode == TARGETED_MODE:
            sensor = _validate_target_sensor(manager, agent, body.get("sensor_entity"))
            created, session = create_explore_child(
                generation, TARGETED_REASON, TARGETED_MODE, sensor=sensor,
                requested={"sensor_entity": sensor}, previous={}
            )
            child = lineage_row(manager.store, generation_id=created["child_generation_id"])
            child_agent = manager.store.get_agent_config(str((child or {}).get("agent_id") or ""))
            if not child_agent:
                raise RuntimeError("Targeted Explore child is unavailable")
            # Never inherit proof from another generation. Only the hypothesis is copied.
            with manager.store.lock, manager.store.conn() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_tournament_shadow'").fetchone():
                    c.execute("DELETE FROM context_tournament_shadow WHERE agent_id=? AND challenger_entity=?",
                              (str(child_agent["id"]), sensor))
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_tournament_sensor_quality'").fetchone():
                    c.execute("DELETE FROM context_tournament_sensor_quality WHERE agent_id=? AND entity_id=?",
                              (str(child_agent["id"]), sensor))
            service._shadow_models.pop((str(child_agent["id"]), sensor), None)
            policy = manager.engine.models.get(str(child_agent["id"])) or manager.engine.policy(child_agent)
            forced_sync(child_agent, policy=policy)
            manager.engine.wake_event.set()
            manager.store.event(
                generation["root_agent_id"], "info", "agent_explore_targeted_started",
                "Targeted sensor registered as a forced Candidate challenger without bypassing Tournament evidence gates",
                {"parent_generation_id": generation["generation_id"],
                 "child_generation_id": created["child_generation_id"], "sensor": sensor,
                 "candidate_dispatch": False},
            )
            return {"ok": True, "action": "explore", "mode": TARGETED_MODE,
                    "parent_generation_id": generation["generation_id"],
                    "child_generation_id": created["child_generation_id"],
                    "session": _session_payload(manager, session)}

        raise ValueError("Explore mode must be 'free' or 'targeted_sensor'")

    def workflow_explore_status(ref):
        generation, _ = _resolve_generation(manager, ref)
        session = _latest_for_parent(manager.store, generation["generation_id"])
        if session and session.get("mode") == TARGETED_MODE and session.get("status") in TARGETED_ACTIVE_STATES:
            child = lineage_row(manager.store, generation_id=session["child_generation_id"])
            child_agent = manager.store.get_agent_config(str((child or {}).get("agent_id") or ""))
            if child_agent:
                _evaluate_targeted(manager, session, child_agent, service)
                session = _row(manager.store, session["session_id"])
        root = manager.store.get_agent_config(str(generation["root_agent_id"]))
        exp = experiments.status(root["id"]) if root else {"config": {}}
        return {
            "root_agent_id": generation["root_agent_id"],
            "parent_generation_id": generation["generation_id"],
            "generation_number": int(generation["generation_number"]),
            "generation_type": generation["generation_type"],
            "free_config": dict(exp.get("config") or {}),
            "active_probe_available": _active_probe_available(root),
            "active_probe_contract": "existing_live_policy_to_action_intent_to_executor_only",
            "targeted_contract": "forced_priority_not_evidence_override_prequential_future_only",
            "session": _session_payload(manager, session),
        }

    def decorate_status(result):
        if not result:
            return result
        gid = result.get("generation_id")
        session = _session_for_child(manager.store, gid) if gid else None
        if session:
            result["explore"] = _session_payload(manager, session)
        return result

    def status(parent_id):
        return decorate_status(original_status(parent_id))

    def list_status():
        return [decorate_status(dict(item)) for item in (original_list_status() or []) if item]

    def lineage_status(ref):
        result = original_lineage_status(ref) if original_lineage_status is not None else None
        return decorate_status(result)

    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status

    def do_get(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"] and tokens[3] == "explore":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            try:
                return http.send_json(200, workflow_explore_status(unquote(tokens[2])))
            except ValueError as exc:
                return http.send_json(404, {"error": str(exc)})
        return original_get(http)

    def do_post(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"] and tokens[3] == "explore":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            try:
                payload = http.read_json()
                return http.send_json(202, workflow_explore(
                    unquote(tokens[2]), payload if isinstance(payload, dict) else {}
                ))
            except ValueError as exc:
                return http.send_json(409, {"error": str(exc)})
            except Exception as exc:
                return http.send_json(500, {"error": f"Explore failed: {type(exc).__name__}: {exc}"})
        return original_post(http)

    handler.do_GET = do_get
    handler.do_POST = do_post
    manager.workflow_explore = workflow_explore
    manager.workflow_explore_status = workflow_explore_status
    manager._agent_explore_installed = True
    manager.agent_explore_contract = "existing_experiments_plus_sensor_tournament_generation_child"
    manager.agent_explore_free_contract = "live_executor_only_residual_learner_then_child_continuation"
    manager.agent_explore_targeted_contract = "forced_challenger_priority_only_future_prequential_quality_gain_safety"
    return manager
