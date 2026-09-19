"""Observed Shadow runtime and paired future evidence for Candidate generations.

Candidate generations are policy-only observers. Whenever a Root Live agent is processed,
all retained post-training generations on its active lineage infer from the same current
state-map snapshot and the same inference timestamp. No Candidate path constructs an
ActionIntent, calls Executor, or invokes a Home Assistant service.

Historical Desired is an observed fact, not a replay: rows in
``candidate_generation_decisions`` are written from predictions that actually ran at that
time. Missing runtime coverage remains a gap. Parent-vs-child comparison consumes one
shared prediction event and one shared future outcome, so paired wins/losses cannot be
assembled from mismatched contexts.
"""
import json
import math
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from context import target_value
from fast_runtime import is_fast_target
from settings import parse_ts


DECISION_STALE_SECONDS = 95.0
DECISION_HEARTBEAT_SECONDS = 30.0
SHADOW_STATES = {"comparing", "ready", "offline_blocked", "insufficient_evidence", "parent"}
COMPARISON_STATES = {"comparing", "ready"}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def ensure_shadow_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_generation_decisions (
                root_agent_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                ts REAL NOT NULL,
                current REAL,
                desired REAL,
                confidence REAL,
                model_revision TEXT,
                schema_revision TEXT,
                PRIMARY KEY(generation_id,ts)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_generation_decisions_root_ts
                ON candidate_generation_decisions(root_agent_id,ts);
            CREATE INDEX IF NOT EXISTS idx_candidate_generation_decisions_event
                ON candidate_generation_decisions(event_id,generation_id);

            CREATE TABLE IF NOT EXISTS candidate_generation_pairs (
                root_agent_id TEXT NOT NULL,
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                prediction_event_id TEXT NOT NULL,
                prediction_ts REAL NOT NULL,
                outcome_ts REAL NOT NULL,
                outcome REAL NOT NULL,
                parent_prediction REAL NOT NULL,
                child_prediction REAL NOT NULL,
                parent_confidence REAL,
                child_confidence REAL,
                parent_correct INTEGER NOT NULL,
                child_correct INTEGER NOT NULL,
                paired_result TEXT NOT NULL,
                parent_lead_seconds REAL,
                child_lead_seconds REAL,
                lead_gain_seconds REAL,
                PRIMARY KEY(parent_generation_id,child_generation_id,outcome_ts)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_generation_pairs_root_outcome
                ON candidate_generation_pairs(root_agent_id,outcome_ts);

            CREATE TABLE IF NOT EXISTS candidate_generation_comparisons (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                root_agent_id TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id)
            );
            """
        )


def _generation(store, *, generation_id=None, agent_id=None):
    with store.conn() as c:
        if generation_id is not None:
            row = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE generation_id=?",
                (str(generation_id),),
            ).fetchone()
        elif agent_id is not None:
            row = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
                (str(agent_id),),
            ).fetchone()
        else:
            return None
    return dict(row) if row else None


def _root_generation(store, root_agent_id):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE root_agent_id=? AND generation_type='live'
               ORDER BY generation_number DESC,created_ts DESC LIMIT 1""",
            (str(root_agent_id),),
        ).fetchone()
    return dict(row) if row else None


def _shadow_generations(store, root_agent_id):
    with store.conn() as c:
        rows = c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE root_agent_id=? AND generation_type='candidate'
                 AND model_retained=1 AND agent_id IS NOT NULL
                 AND lifecycle_state IN ('comparing','ready','offline_blocked','insufficient_evidence','parent')
               ORDER BY generation_number,created_ts""",
            (str(root_agent_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def _schema_revision(policy, generation):
    selection = dict(getattr(policy, "selection_meta", None) or {})
    if selection.get("schema_revision") is not None:
        return str(selection.get("schema_revision"))
    schema = getattr(policy, "schema", None)
    version = getattr(schema, "version", None)
    if version is not None:
        return str(version)
    return generation.get("schema_revision")


def _model_revision(policy, generation):
    value = getattr(policy, "model_revision", None)
    return str(value) if value is not None else generation.get("model_revision")


def _predict_candidate(manager, generation, state_map, event_ts):
    agent_id = generation.get("agent_id")
    if not agent_id:
        return None
    try:
        policy = manager.engine.models.get(str(agent_id))
        if policy is None:
            agent = manager.store.get_agent_config(str(agent_id))
            if not agent or manager.store.get_model(str(agent_id)) is None:
                return None
            policy = manager.engine.policy(agent)
        features, _, _ = policy.features(state_map, manager.engine.temporal_history, at_ts=event_ts)
        result = policy.predict(features)
        chosen = result[0]
        confidence = result[1] if len(result) > 1 else None
        desired = float(chosen["value"])
        confidence = None if confidence is None else float(confidence)
        if not math.isfinite(desired) or (confidence is not None and not math.isfinite(confidence)):
            return None
        return {
            "generation_id": generation["generation_id"],
            "agent_id": str(agent_id),
            "desired": desired,
            "confidence": confidence,
            "model_revision": _model_revision(policy, generation),
            "schema_revision": _schema_revision(policy, generation),
        }
    except Exception as exc:
        manager.store.event(
            generation["root_agent_id"], "warning", "candidate_shadow_inference_gap",
            "Candidate Shadow inference failed; history keeps a gap instead of replaying a prediction",
            {"generation_id": generation["generation_id"], "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


def _root_observation(manager, root_agent, generation):
    rt = manager.engine.runtime.get(str(root_agent["id"])) or {}
    desired = rt.get("last_prediction")
    if desired is None:
        return None
    try:
        desired = float(desired)
        confidence = rt.get("last_confidence")
        confidence = None if confidence is None else float(confidence)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(desired) or (confidence is not None and not math.isfinite(confidence)):
        return None
    policy = manager.engine.models.get(str(root_agent["id"]))
    if policy is not None:
        model_revision = _model_revision(policy, generation)
        schema_revision = _schema_revision(policy, generation)
    else:
        model = manager.store.get_model(str(root_agent["id"])) or {}
        model_revision = model.get("model_revision") or generation.get("model_revision")
        schema_revision = generation.get("schema_revision")
    return {
        "generation_id": generation["generation_id"],
        "agent_id": str(root_agent["id"]),
        "desired": desired,
        "confidence": confidence,
        "model_revision": model_revision,
        "schema_revision": schema_revision,
    }


def _same_value(agent, left, right):
    if left is None or right is None:
        return False
    if str(agent.get("target_property") or "") == "power":
        return (1.0 if float(left) >= .5 else 0.0) == (1.0 if float(right) >= .5 else 0.0)
    tolerance = max(
        float(agent.get("deadband") or 0.0),
        (float(agent.get("max_value") or 0.0) - float(agent.get("min_value") or 0.0)) * .03,
        .01,
    )
    return abs(float(left) - float(right)) <= tolerance


def _active_comparison_edge(manager, root_id):
    with manager.store.conn() as c:
        row = c.execute(
            """SELECT e.*, child.generation_id AS child_generation_id,
                      child.parent_generation_id AS parent_generation_id,
                      parent.agent_id AS comparison_parent_agent_id
               FROM agent_candidates e
               JOIN agent_candidate_generations child ON child.agent_id=e.candidate_id
               JOIN agent_candidate_generations parent ON parent.generation_id=child.parent_generation_id
               WHERE child.root_agent_id=? AND e.state IN ('comparing','ready')
                 AND child.lifecycle_state NOT IN ('discarded','pruned','promoted')
               ORDER BY child.generation_number DESC,child.created_ts DESC LIMIT 1""",
            (str(root_id),),
        ).fetchone()
    if not row:
        return None
    edge = dict(row)
    gate = _json(edge.get("offline_gate_json"), {})
    if gate and not gate.get("passed"):
        return None
    return edge


def _outcome_ts(target_state):
    try:
        parsed = parse_ts((target_state or {}).get("last_updated") or (target_state or {}).get("last_changed"))
        if parsed:
            return float(parsed)
    except Exception:
        pass
    return time.time()


def _blank_summary():
    return {
        "samples": 0,
        "live_correct": 0,
        "candidate_correct": 0,
        "parent_correct": 0,
        "child_correct": 0,
        "candidate_wins": 0,
        "live_wins": 0,
        "child_wins": 0,
        "parent_wins": 0,
        "both_correct": 0,
        "both_wrong": 0,
        "per_action": {},
        "on_events": 0,
        "off_events": 0,
        "live_on_lead_sum": 0.0,
        "candidate_on_lead_sum": 0.0,
        "live_off_lead_sum": 0.0,
        "candidate_off_lead_sum": 0.0,
        "live_false_early": 0,
        "candidate_false_early": 0,
        "updated_ts": None,
    }


def _comparison_row(store, parent_generation_id, child_generation_id):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_generation_comparisons
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_generation_id), str(child_generation_id)),
        ).fetchone()
    return dict(row) if row else None


def _persist_summary(manager, edge, summary):
    summary["updated_ts"] = time.time()
    parent_gid = edge["parent_generation_id"]
    child_gid = edge["child_generation_id"]
    root_id = str(_generation(manager.store, generation_id=child_gid)["root_agent_id"])
    raw = json.dumps(summary, separators=(",", ":"))
    row = manager._candidate_row(edge["parent_agent_id"]) or edge
    derived = manager._comparison_summary({**row, "comparison_json": raw})
    state = "ready" if derived.get("promotable") else "comparing"
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """INSERT INTO candidate_generation_comparisons
               (parent_generation_id,child_generation_id,root_agent_id,summary_json,updated_ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 summary_json=excluded.summary_json,updated_ts=excluded.updated_ts""",
            (parent_gid, child_gid, root_id, raw, time.time()),
        )
        c.execute(
            """UPDATE agent_candidates SET comparison_json=?,state=?,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=?""",
            (raw, state, time.time(), edge["parent_agent_id"], edge["candidate_id"]),
        )
        c.execute(
            """UPDATE agent_candidate_generations SET comparison_json=?,lifecycle_state=?,updated_ts=?
               WHERE generation_id=?""",
            (raw, state, time.time(), child_gid),
        )
    return derived


def _rebuild_summary(manager, edge):
    parent_gid = edge["parent_generation_id"]
    child_gid = edge["child_generation_id"]
    with manager.store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=? ORDER BY outcome_ts""",
            (parent_gid, child_gid),
        ).fetchall()]
    existing = _comparison_row(manager.store, parent_gid, child_gid)
    old = _json((existing or {}).get("summary_json"), _blank_summary())
    summary = _blank_summary()
    summary["live_false_early"] = int(old.get("live_false_early") or 0)
    summary["candidate_false_early"] = int(old.get("candidate_false_early") or 0)
    for pair in rows:
        p_ok = bool(pair["parent_correct"])
        c_ok = bool(pair["child_correct"])
        outcome = float(pair["outcome"])
        summary["samples"] += 1
        summary["live_correct"] += int(p_ok)
        summary["candidate_correct"] += int(c_ok)
        summary["parent_correct"] += int(p_ok)
        summary["child_correct"] += int(c_ok)
        if c_ok and not p_ok:
            summary["candidate_wins"] += 1
            summary["child_wins"] += 1
        elif p_ok and not c_ok:
            summary["live_wins"] += 1
            summary["parent_wins"] += 1
        elif p_ok and c_ok:
            summary["both_correct"] += 1
        else:
            summary["both_wrong"] += 1
        key = str(float(outcome))
        slot = summary["per_action"].setdefault(key, {"samples": 0, "live_correct": 0, "candidate_correct": 0})
        slot["samples"] += 1
        slot["live_correct"] += int(p_ok)
        slot["candidate_correct"] += int(c_ok)
        if pair.get("parent_lead_seconds") is not None or pair.get("child_lead_seconds") is not None:
            if outcome >= .5:
                summary["on_events"] += 1
                summary["live_on_lead_sum"] += float(pair.get("parent_lead_seconds") or 0.0)
                summary["candidate_on_lead_sum"] += float(pair.get("child_lead_seconds") or 0.0)
            else:
                summary["off_events"] += 1
                summary["live_off_lead_sum"] += float(pair.get("parent_lead_seconds") or 0.0)
                summary["candidate_off_lead_sum"] += float(pair.get("child_lead_seconds") or 0.0)
    return _persist_summary(manager, edge, summary)


def _apply_pair_to_summary(manager, edge, pair):
    """Update comparison summary from one newly inserted paired outcome in O(1).

    _rebuild_summary remains available for migration/recovery, but live outcome handling
    must never rescan the complete pair table as evidence grows.
    """
    existing = _comparison_row(
        manager.store, edge["parent_generation_id"], edge["child_generation_id"]
    )
    summary = {
        **_blank_summary(),
        **_json((existing or {}).get("summary_json"), _blank_summary()),
    }
    p_ok = bool(pair["parent_correct"])
    c_ok = bool(pair["child_correct"])
    outcome = float(pair["outcome"])
    summary["samples"] = int(summary.get("samples") or 0) + 1
    summary["live_correct"] = int(summary.get("live_correct") or 0) + int(p_ok)
    summary["candidate_correct"] = int(summary.get("candidate_correct") or 0) + int(c_ok)
    summary["parent_correct"] = int(summary.get("parent_correct") or 0) + int(p_ok)
    summary["child_correct"] = int(summary.get("child_correct") or 0) + int(c_ok)
    if c_ok and not p_ok:
        summary["candidate_wins"] = int(summary.get("candidate_wins") or 0) + 1
        summary["child_wins"] = int(summary.get("child_wins") or 0) + 1
    elif p_ok and not c_ok:
        summary["live_wins"] = int(summary.get("live_wins") or 0) + 1
        summary["parent_wins"] = int(summary.get("parent_wins") or 0) + 1
    elif p_ok and c_ok:
        summary["both_correct"] = int(summary.get("both_correct") or 0) + 1
    else:
        summary["both_wrong"] = int(summary.get("both_wrong") or 0) + 1

    key = str(float(outcome))
    per_action = dict(summary.get("per_action") or {})
    slot = {
        "samples": 0, "live_correct": 0, "candidate_correct": 0,
        **dict(per_action.get(key) or {}),
    }
    slot["samples"] = int(slot.get("samples") or 0) + 1
    slot["live_correct"] = int(slot.get("live_correct") or 0) + int(p_ok)
    slot["candidate_correct"] = int(slot.get("candidate_correct") or 0) + int(c_ok)
    per_action[key] = slot
    summary["per_action"] = per_action

    parent_lead = pair.get("parent_lead_seconds")
    child_lead = pair.get("child_lead_seconds")
    if parent_lead is not None or child_lead is not None:
        if outcome >= .5:
            summary["on_events"] = int(summary.get("on_events") or 0) + 1
            summary["live_on_lead_sum"] = float(summary.get("live_on_lead_sum") or 0.0) + float(parent_lead or 0.0)
            summary["candidate_on_lead_sum"] = float(summary.get("candidate_on_lead_sum") or 0.0) + float(child_lead or 0.0)
        else:
            summary["off_events"] = int(summary.get("off_events") or 0) + 1
            summary["live_off_lead_sum"] = float(summary.get("live_off_lead_sum") or 0.0) + float(parent_lead or 0.0)
            summary["candidate_off_lead_sum"] = float(summary.get("candidate_off_lead_sum") or 0.0) + float(child_lead or 0.0)
    return _persist_summary(manager, edge, summary)


def install(manager):
    if getattr(manager, "_candidate_shadow_runtime_installed", False):
        return manager
    ensure_shadow_tables(manager.store)

    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)
    original_maintenance = manager._maintenance
    handler = manager.core.Handler
    original_get = handler.do_GET

    shadow_runtime = {}
    active_candidate_parents = set()
    candidate_dependency_roots = {}
    passive_pending = {}
    original_on_state_changed = getattr(manager.engine, "on_state_changed", None)

    def _refresh_active_candidate_parents():
        # Candidate lifecycle mutations are rare. Rebuild one compact in-memory set there,
        # instead of probing agent_candidates from every before/after live inference.
        try:
            with manager.store.conn() as db:
                rows = db.execute(
                    "SELECT DISTINCT parent_agent_id FROM agent_candidates"
                ).fetchall()
            values = {str(row[0]) for row in rows if row and row[0]}
        except Exception:
            values = set(active_candidate_parents)
        active_candidate_parents.clear()
        active_candidate_parents.update(values)
        return len(active_candidate_parents)

    def _schema_entities(agent_id):
        try:
            raw = manager.store.get_model(str(agent_id)) or {}
        except Exception:
            return set()
        schema = dict(raw.get("schema") or {}) if isinstance(raw, dict) else {}
        return {str(eid) for eid in (schema.get("entities") or ()) if eid}

    def _rebuild_candidate_dependency_index():
        mapping = {}
        for root_id in tuple(active_candidate_parents):
            root = manager.store.get_agent_config(str(root_id))
            if not root:
                continue
            deps = {str(root.get("target_entity") or "")}
            deps.update(
                str(eid) for eid in (root.get("input_entities") or ())
                if isinstance(eid, str) and eid and eid != "*"
            )
            deps.update(_schema_entities(root_id))
            try:
                area = manager.engine.context.area_for(root.get("target_entity"))
                if area:
                    deps.update(
                        str(eid)
                        for eid in getattr(manager.engine.context.home, "area_sources", {}).get(area, ())
                    )
            except Exception:
                pass
            for generation in _shadow_generations(manager.store, root_id):
                candidate_id = generation.get("agent_id")
                if not candidate_id:
                    continue
                candidate = manager.store.get_agent_config(str(candidate_id)) or {}
                deps.update(
                    str(eid) for eid in (candidate.get("input_entities") or ())
                    if isinstance(eid, str) and eid and eid != "*"
                )
                deps.update(_schema_entities(candidate_id))
            deps.discard("")
            for entity_id in deps:
                mapping.setdefault(entity_id, set()).add(str(root_id))
        candidate_dependency_roots.clear()
        candidate_dependency_roots.update(mapping)
        return sum(len(v) for v in mapping.values())

    def candidate_hot_active(agent_id):
        return str(agent_id) in active_candidate_parents

    _refresh_active_candidate_parents()
    _rebuild_candidate_dependency_index()

    def _root_runtime(root_id):
        return shadow_runtime.setdefault(str(root_id), {
            "previous_current": None,
            "bundle": None,
            "last_persist": 0.0,
            "persisted": {},
            "generation_state": {},
            "generation_cache_at": 0.0,
            "shadow_generations": None,
            "root_generation": None,
        })

    def invalidate_generation_cache(*_args, **_kwargs):
        # Candidate lineage mutations are rare. Invalidate all root caches immediately
        # instead of polling SQLite from every realtime inference.
        manager.store._provenance_generation_revision = int(
            getattr(manager.store, "_provenance_generation_revision", 0)
        ) + 1
        _refresh_active_candidate_parents()
        _rebuild_candidate_dependency_index()
        for runtime in shadow_runtime.values():
            runtime["generation_cache_at"] = 0.0
            runtime["shadow_generations"] = None
            runtime["root_generation"] = None

    def _cached_generations(root_id):
        root_rt = _root_runtime(root_id)
        # Candidate lineage mutations are already wrapped below and invalidate this cache
        # synchronously. Do not poll the generation tables every 30 s for every ordinary
        # live agent merely to rediscover the common empty-Candidate state.
        if root_rt.get("shadow_generations") is not None:
            return root_rt.get("root_generation"), root_rt.get("shadow_generations") or []
        generations = _shadow_generations(manager.store, root_id)
        root_generation = _root_generation(manager.store, root_id) if generations else None
        root_rt["generation_cache_at"] = time.monotonic()
        root_rt["shadow_generations"] = generations
        root_rt["root_generation"] = root_generation
        return root_generation, generations

    def _decorate_result(root_rt, result, event_ts, current):
        state = root_rt["generation_state"].setdefault(result["generation_id"], {})
        previous_desired = state.get("desired")
        previous_current = state.get("current")
        if previous_desired is None or not _same_value({"target_property": "power", "deadband": .01,
                                                        "min_value": 0, "max_value": 1},
                                                       previous_desired, result["desired"]):
            state["desired_since_ts"] = event_ts
        result["desired_since_ts"] = float(state.get("desired_since_ts") or event_ts)
        state.update({"desired": result["desired"], "confidence": result.get("confidence"), "current": current,
                      "event_ts": event_ts, "previous_current": previous_current})
        return result

    def _bundle(root_agent, state_map, *, include_parent=True, event_id=None, event_ts=None):
        root_gen, shadow_generations = _cached_generations(root_agent["id"])
        # No Candidate means no A/B observation work at all. Live runtime telemetry is
        # already stored by Engine; candidate_generation_decisions exist only to compare
        # an actual alternative policy against its parent.
        if not root_gen or not shadow_generations:
            return None
        current = target_value((state_map or {}).get(root_agent["target_entity"]), root_agent["target_property"])
        try:
            current = float(current)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(current):
            return None
        event_ts = time.time() if event_ts is None else float(event_ts)
        event_id = str(uuid.uuid4()) if event_id is None else str(event_id)
        root_rt = _root_runtime(root_agent["id"])
        results = {}
        if include_parent:
            observed_root = _root_observation(manager, root_agent, root_gen)
            if observed_root is not None:
                results[root_gen["generation_id"]] = _decorate_result(root_rt, observed_root, event_ts, current)
        for generation in shadow_generations:
            observed = _predict_candidate(manager, generation, state_map, event_ts)
            if observed is not None:
                results[generation["generation_id"]] = _decorate_result(root_rt, observed, event_ts, current)
        return {
            "root_agent_id": str(root_agent["id"]), "event_id": event_id, "ts": event_ts,
            "current": current, "results": results,
        }

    def _persist_bundle(bundle, force=False):
        if not bundle:
            return False
        root_rt = _root_runtime(bundle["root_agent_id"])
        now = float(bundle["ts"])
        should = bool(force or now - float(root_rt.get("last_persist") or 0.0) >= DECISION_HEARTBEAT_SECONDS)
        previous = root_rt.get("persisted") or {}
        if not should:
            for gid, result in bundle["results"].items():
                old = previous.get(gid)
                if old is None or old.get("desired") != result.get("desired") or old.get("current") != bundle.get("current"):
                    should = True
                    break
                old_conf, new_conf = old.get("confidence"), result.get("confidence")
                if old_conf is None or new_conf is None or abs(float(old_conf) - float(new_conf)) >= .02:
                    should = True
                    break
                if old.get("model_revision") != result.get("model_revision") or old.get("schema_revision") != result.get("schema_revision"):
                    should = True
                    break
        if not should:
            return False
        with manager.store.lock, manager.store.conn() as c:
            for gid, result in bundle["results"].items():
                c.execute(
                    """INSERT OR REPLACE INTO candidate_generation_decisions
                       (root_agent_id,generation_id,event_id,ts,current,desired,confidence,model_revision,schema_revision)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        bundle["root_agent_id"], gid, bundle["event_id"], float(bundle["ts"]),
                        float(bundle["current"]), float(result["desired"]), result.get("confidence"),
                        result.get("model_revision"), result.get("schema_revision"),
                    ),
                )
        root_rt["last_persist"] = now
        root_rt["persisted"] = {
            gid: {"current": bundle["current"], "desired": result.get("desired"), "confidence": result.get("confidence"),
                  "model_revision": result.get("model_revision"), "schema_revision": result.get("schema_revision")}
            for gid, result in bundle["results"].items()
        }
        return True

    def _increment_false_early(root_id, previous_bundle, current_bundle):
        if not previous_bundle or not current_bundle or previous_bundle.get("current") != current_bundle.get("current"):
            return
        edge = _active_comparison_edge(manager, root_id)
        if not edge:
            return
        parent_gid, child_gid = edge["parent_generation_id"], edge["child_generation_id"]
        prev_parent = previous_bundle["results"].get(parent_gid)
        prev_child = previous_bundle["results"].get(child_gid)
        now_parent = current_bundle["results"].get(parent_gid)
        now_child = current_bundle["results"].get(child_gid)
        if not prev_parent or not prev_child or not now_parent or not now_child:
            return
        agent = manager.store.get_agent_config(edge["parent_agent_id"]) or manager.store.get_agent_config(root_id)
        current = current_bundle["current"]
        parent_false = (not _same_value(agent, prev_parent["desired"], current) and _same_value(agent, now_parent["desired"], current))
        child_false = (not _same_value(agent, prev_child["desired"], current) and _same_value(agent, now_child["desired"], current))
        if not parent_false and not child_false:
            return
        existing = _comparison_row(manager.store, parent_gid, child_gid)
        summary = {**_blank_summary(), **_json((existing or {}).get("summary_json"), _blank_summary())}
        summary["live_false_early"] = int(summary.get("live_false_early") or 0) + int(parent_false)
        summary["candidate_false_early"] = int(summary.get("candidate_false_early") or 0) + int(child_false)
        _persist_summary(manager, edge, summary)

    def after_live_process(agent, state_map):
        # This extension is authoritative for Candidate Shadow runtime; the older Candidate
        # wrapper is intentionally not called, avoiding duplicate policy inference and
        # duplicate A/B samples. _bundle() is the only generation lookup on this path.
        root_rt = _root_runtime(agent["id"])
        previous_bundle = root_rt.get("bundle")
        bundle = _bundle(agent, state_map)
        if not bundle:
            return None
        _increment_false_early(agent["id"], previous_bundle, bundle)
        root_rt["bundle"] = bundle
        root_rt["previous_current"] = bundle["current"]
        try:
            observed_revision = int(getattr(manager.engine._inference_tls, "state_revision"))
        except Exception:
            observed_revision = int(getattr(manager.engine, "state_revision", 0) or 0)
        root_rt["last_candidate_observed_revision"] = observed_revision
        root_rt["last_candidate_observed_monotonic"] = time.monotonic()
        _persist_bundle(bundle, force=False)
        return bundle

    def before_live_process(agent, state_map):
        root_rt = _root_runtime(agent["id"])
        previous_current = root_rt.get("previous_current")
        if previous_current is None:
            return None
        target_state = (state_map or {}).get(agent["target_entity"])
        current = target_value(target_state, agent["target_property"])
        try:
            current = float(current)
        except (TypeError, ValueError):
            return None
        threshold = max(.01, float(agent.get("deadband") or 0.0) * .05)
        if abs(float(previous_current) - current) <= threshold:
            return None
        try:
            if manager.engine.own_command_echo(agent, target_state, current):
                return None
        except Exception:
            pass
        edge = _active_comparison_edge(manager, agent["id"])
        bundle = root_rt.get("bundle")
        if not edge or not bundle:
            return None
        parent = bundle["results"].get(edge["parent_generation_id"])
        child = bundle["results"].get(edge["child_generation_id"])
        if not parent or not child:
            # One side did not actually run for this context. Missing evidence stays a gap.
            return None
        _persist_bundle(bundle, force=True)
        outcome_ts = _outcome_ts(target_state)
        outcome = 1.0 if str(agent.get("target_property")) == "power" and current >= .5 else 0.0 if str(agent.get("target_property")) == "power" else current
        p_ok = _same_value(agent, parent["desired"], outcome)
        c_ok = _same_value(agent, child["desired"], outcome)
        if c_ok and not p_ok:
            paired_result = "child_win"
        elif p_ok and not c_ok:
            paired_result = "parent_win"
        elif p_ok and c_ok:
            paired_result = "both_correct"
        else:
            paired_result = "both_wrong"
        parent_lead = child_lead = lead_gain = None
        if is_fast_target(agent):
            if p_ok:
                parent_lead = min(30.0, max(0.0, outcome_ts - float(parent.get("desired_since_ts") or bundle["ts"])))
            if c_ok:
                child_lead = min(30.0, max(0.0, outcome_ts - float(child.get("desired_since_ts") or bundle["ts"])))
            if parent_lead is not None and child_lead is not None:
                lead_gain = child_lead - parent_lead
        inserted = False
        with manager.store.lock, manager.store.conn() as c:
            before = c.total_changes
            c.execute(
                """INSERT OR IGNORE INTO candidate_generation_pairs
                   (root_agent_id,parent_generation_id,child_generation_id,prediction_event_id,prediction_ts,
                    outcome_ts,outcome,parent_prediction,child_prediction,parent_confidence,child_confidence,
                    parent_correct,child_correct,paired_result,parent_lead_seconds,child_lead_seconds,lead_gain_seconds)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(agent["id"]), edge["parent_generation_id"], edge["child_generation_id"],
                    bundle["event_id"], float(bundle["ts"]), float(outcome_ts), float(outcome),
                    float(parent["desired"]), float(child["desired"]), parent.get("confidence"), child.get("confidence"),
                    int(p_ok), int(c_ok), paired_result, parent_lead, child_lead, lead_gain,
                ),
            )
            inserted = c.total_changes > before
        if inserted:
            _apply_pair_to_summary(
                manager,
                edge,
                {
                    "parent_correct": int(p_ok),
                    "child_correct": int(c_ok),
                    "outcome": float(outcome),
                    "parent_lead_seconds": parent_lead,
                    "child_lead_seconds": child_lead,
                },
            )
        return inserted

    def generation_history(ref, start, end):
        generation = _generation(manager.store, generation_id=ref) or _generation(manager.store, agent_id=ref)
        if not generation:
            raise ValueError("Candidate generation not found")
        start, end = float(start), float(end)
        if end < start:
            start, end = end, start
        with manager.store.conn() as c:
            rows = [dict(r) for r in c.execute(
                """SELECT root_agent_id,generation_id,event_id,ts,current,desired,confidence,model_revision,schema_revision
                   FROM candidate_generation_decisions WHERE generation_id=? AND ts>=? AND ts<=? ORDER BY ts""",
                (generation["generation_id"], start, end),
            ).fetchall()]
        gaps = []
        cursor = start
        for row in rows:
            ts = float(row["ts"])
            if ts - cursor > DECISION_STALE_SECONDS:
                gaps.append({"start": cursor, "end": ts, "reason": "generation_not_observed"})
            cursor = max(cursor, ts)
        if end - cursor > DECISION_STALE_SECONDS:
            gaps.append({"start": cursor, "end": end, "reason": "generation_not_observed"})
        if not rows and end > start:
            gaps = [{"start": start, "end": end, "reason": "generation_not_observed"}]
        return {
            "root_agent_id": generation["root_agent_id"],
            "generation_id": generation["generation_id"],
            "start": start, "end": end, "points": rows, "gaps": gaps,
            "desired_source": "observed_candidate_generation_shadow_runtime",
            "desired_semantics": "prediction actually observed from this exact generation at that timestamp",
            "policy_replay_used": False,
            "stale_after_seconds": DECISION_STALE_SECONDS,
        }

    def generation_decision_at(ref, timestamp):
        generation = _generation(manager.store, generation_id=ref) or _generation(manager.store, agent_id=ref)
        if not generation:
            return None
        timestamp = float(timestamp)
        with manager.store.conn() as c:
            row = c.execute(
                """SELECT * FROM candidate_generation_decisions
                   WHERE generation_id=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                (generation["generation_id"], timestamp),
            ).fetchone()
        if not row or timestamp - float(row["ts"]) > DECISION_STALE_SECONDS:
            return None
        return dict(row)

    def generation_comparison(ref):
        child = _generation(manager.store, generation_id=ref) or _generation(manager.store, agent_id=ref)
        if not child or not child.get("parent_generation_id"):
            return None
        row = _comparison_row(manager.store, child["parent_generation_id"], child["generation_id"])
        summary = _json((row or {}).get("summary_json"), _blank_summary())
        with manager.store.conn() as c:
            pairs = int(c.execute(
                """SELECT COUNT(*) FROM candidate_generation_pairs
                   WHERE parent_generation_id=? AND child_generation_id=?""",
                (child["parent_generation_id"], child["generation_id"]),
            ).fetchone()[0])
        return {
            "root_agent_id": child["root_agent_id"], "parent_generation_id": child["parent_generation_id"],
            "child_generation_id": child["generation_id"], "pairs": pairs, "summary": summary,
            "contract": "same_prediction_event_same_future_outcome",
        }

    def _latest_shadow(generation_id):
        with manager.store.conn() as c:
            row = c.execute(
                """SELECT * FROM candidate_generation_decisions
                   WHERE generation_id=? ORDER BY ts DESC LIMIT 1""",
                (str(generation_id),),
            ).fetchone()
        if not row:
            return None
        row = dict(row)
        if time.time() - float(row["ts"]) > DECISION_STALE_SECONDS:
            return None
        return row

    def _decorate_status(result):
        if not result:
            return result
        gid = result.get("generation_id")
        latest = _latest_shadow(gid) if gid else None
        result["shadow_active"] = latest is not None
        result["shadow_timestamp"] = latest.get("ts") if latest else None
        result["shadow_current"] = latest.get("current") if latest else None
        result["candidate_desired"] = latest.get("desired") if latest else None
        result["candidate_confidence"] = latest.get("confidence") if latest else None
        result["shadow_model_revision"] = latest.get("model_revision") if latest else None
        result["shadow_schema_revision"] = latest.get("schema_revision") if latest else None
        if gid:
            comp = generation_comparison(gid)
            if comp:
                result["paired_comparison"] = comp
        return result

    def status(parent_id):
        return _decorate_status(original_status(parent_id))

    def list_status():
        return [_decorate_status(dict(item)) for item in (original_list_status() or []) if item]

    def lineage_status(ref):
        result = original_lineage_status(ref) if original_lineage_status is not None else None
        return _decorate_status(result)

    def _queue_passive_root(root_id, revision, *, delay_seconds=0.35, wake=True):
        root_id = str(root_id)
        with manager.lock:
            previous = passive_pending.get(root_id) or {}
            passive_pending[root_id] = {
                "revision": max(int(previous.get("revision") or 0), int(revision or 0)),
                "due": min(
                    float(previous.get("due") or (time.monotonic() + delay_seconds)),
                    time.monotonic() + max(0.0, float(delay_seconds)),
                ),
            }
        if wake:
            manager.wake_event.set()

    def _observe_passive_root(root_id, revision):
        root_id = str(root_id)
        if root_id not in active_candidate_parents:
            return False
        root_rt = _root_runtime(root_id)
        if int(root_rt.get("last_candidate_observed_revision") or 0) >= int(revision or 0):
            return False
        root_rt["last_candidate_attempt_monotonic"] = time.monotonic()
        root = manager.store.get_agent_config(root_id)
        if not root:
            return False
        with manager.engine.lock:
            state_map = dict(manager.engine.state_map)
            current_revision = int(getattr(manager.engine, "state_revision", 0) or 0)
        event_id = f"candidate-passive:{uuid.uuid4()}"
        bundle = _bundle(
            root, state_map, include_parent=False,
            event_id=event_id, event_ts=time.time(),
        )
        if not bundle or not bundle.get("results"):
            return False
        _persist_bundle(bundle, force=True)
        root_rt["last_candidate_observed_revision"] = max(int(revision or 0), current_revision)
        root_rt["last_candidate_observed_monotonic"] = time.monotonic()
        root_rt["last_passive_bundle"] = bundle
        manager.store.event(
            root_id, "info", "candidate_shadow_passive_observation",
            "Candidate Shadow observed a state revision independently of Parent inference",
            {
                "revision": int(root_rt["last_candidate_observed_revision"]),
                "generations": len(bundle.get("results") or {}),
                "event_id": event_id,
            },
        )
        return True

    def drain_candidate_shadow_events(*, force=False, max_roots=2):
        now = time.monotonic()
        # A 30 s heartbeat keeps persistent Candidate Shadow fresh even while Parent is
        # paused or no relevant HA entity changes. Normal Parent inference updates the
        # same monotonic timestamp and therefore suppresses this fallback.
        current_revision = int(getattr(manager.engine, "state_revision", 0) or 0)
        for root_id in tuple(active_candidate_parents):
            rt = _root_runtime(root_id)
            last_activity = max(
                float(rt.get("last_candidate_observed_monotonic") or 0.0),
                float(rt.get("last_candidate_attempt_monotonic") or 0.0),
            )
            if now - last_activity >= DECISION_HEARTBEAT_SECONDS:
                # We are already on the Candidate worker. Do not self-signal the wake
                # event or an untrained/temporarily unavailable Candidate could spin.
                _queue_passive_root(root_id, current_revision, delay_seconds=0.0, wake=False)

        ready = []
        with manager.lock:
            for root_id, pending in list(passive_pending.items()):
                if force or float(pending.get("due") or 0.0) <= now:
                    ready.append((root_id, int(pending.get("revision") or 0)))
                    passive_pending.pop(root_id, None)
                    if len(ready) >= max(1, int(max_roots)):
                        break
        completed = 0
        for root_id, revision in ready:
            completed += int(_observe_passive_root(root_id, revision))
        return completed

    def candidate_state_changed(data):
        result = original_on_state_changed(data)
        entity_id = str((data or {}).get("entity_id") or "")
        if not entity_id:
            return result
        with manager.engine.lock:
            revision = int(getattr(manager.engine, "state_revision", 0) or 0)
        for root_id in tuple(candidate_dependency_roots.get(entity_id, ())):
            _queue_passive_root(root_id, revision)
        return result

    def maintenance():
        # Runtime evidence is never synthesized from history. Passive observations use
        # only the live websocket-backed state map and run on the Candidate worker, never
        # on the websocket callback or HTTP thread.
        drain_candidate_shadow_events()
        return original_maintenance()

    def do_get(http):
        parts = urlsplit(http.path)
        path = parts.path
        tokens = path.strip("/").split("/")
        if len(tokens) == 4 and tokens[0] == "api" and tokens[1] == "candidate-generations" and tokens[3] in ("history", "comparison"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            ref = unquote(tokens[2])
            try:
                if tokens[3] == "comparison":
                    result = generation_comparison(ref)
                    if result is None:
                        return http.send_json(404, {"error": "Candidate generation comparison not found"})
                    return http.send_json(200, result)
                query = parse_qs(parts.query)
                now = time.time()
                start = float((query.get("start") or [now - 3600.0])[0])
                end = float((query.get("end") or [now])[0])
                return http.send_json(200, generation_history(ref, start, end))
            except (TypeError, ValueError) as exc:
                return http.send_json(404, {"error": str(exc)})
        return original_get(http)

    # Keep generation discovery event-invalidated. This avoids both stale Candidate
    # visibility and a generation-table query on every live inference.
    for method_name in (
        "enqueue", "spawn_child", "discard", "promote",
        "_create_candidate", "_delete_candidate", "_finish_build_if_ready",
    ):
        original = getattr(manager, method_name, None)
        if not callable(original):
            continue
        def wrapped(*args, __original=original, **kwargs):
            result = __original(*args, **kwargs)
            invalidate_generation_cache()
            return result
        setattr(manager, method_name, wrapped)
    manager.invalidate_candidate_shadow_cache = invalidate_generation_cache
    manager.candidate_hot_active = candidate_hot_active
    manager.candidate_hot_active_count = lambda: len(active_candidate_parents)

    manager.before_live_process = before_live_process
    manager.after_live_process = after_live_process
    manager.drain_candidate_shadow_events = drain_candidate_shadow_events
    manager.candidate_dependency_roots = candidate_dependency_roots
    manager.rebuild_candidate_dependency_index = _rebuild_candidate_dependency_index
    if callable(original_on_state_changed):
        manager.engine.on_state_changed = candidate_state_changed
    manager.generation_history = generation_history
    manager.generation_decision_at = generation_decision_at
    manager.generation_comparison = generation_comparison
    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    manager._maintenance = maintenance
    handler.do_GET = do_get
    manager._candidate_shadow_runtime_installed = True
    manager.candidate_shadow_contract = "observed_generation_predictions_no_executor_plus_passive_event_fallback"
    manager.candidate_event_contract = "state_changed_dependency_index_to_candidate_worker_with_parent_path_dedup"
    manager.candidate_decision_history_contract = "observed_only_no_policy_replay_gaps_preserved"
    manager.candidate_pair_contract = "same_prediction_event_same_future_outcome"
    return manager
