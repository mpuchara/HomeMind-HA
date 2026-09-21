"""Bounded read-only diagnostics for Candidate Correct learning.

The endpoint added here is intentionally observational. It never mutates a model, schema,
feedback record, Candidate state or Home Assistant device. The expensive part (historical
feature reconstruction) is opt-in with detail=full and remains bounded by explicit
label/entity/row limits so debugging cannot become another Raspberry Pi background job.
"""
from __future__ import annotations

import json
import math
import time
from urllib.parse import parse_qs, unquote, urlsplit

from context import archived_state
from manual_context_learning import manual_scores
from policy import MultiHorizonPolicy


CONTRACT_VERSION = 1
MAX_LABELS = 256
MAX_DEBUG_ENTITIES = 96
MAX_RAW_ROWS_PER_LABEL = 768
DEFAULT_LABELS = 64
DEFAULT_WINDOW_SECONDS = 120.0


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone())


def _json(raw, default=None):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except Exception:
        return {} if default is None else default


def _bounded_int(raw, default, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(lo), min(int(hi), value))


def _bounded_float(raw, default, lo, hi):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(float(lo), min(float(hi), value))


def _safe_agent(agent):
    if not agent:
        return None
    return {
        key: agent.get(key) for key in (
            "id", "name", "target_entity", "target_property", "mode", "training_state",
            "benchmark_score", "benchmark_samples", "input_entities", "enabled",
        )
    }


def _generation_rows(store, ref):
    """Resolve root from either a Live agent, Candidate surrogate or generation id."""
    with store.conn() as c:
        if not _table_exists(c, "agent_candidate_generations"):
            return str(ref), []
        row = c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE generation_id=? OR agent_id=? OR root_agent_id=?
               ORDER BY CASE WHEN agent_id=? THEN 0 ELSE 1 END,generation_number DESC LIMIT 1""",
            (str(ref), str(ref), str(ref), str(ref)),
        ).fetchone()
        root = str(row["root_agent_id"]) if row else str(ref)
        rows = [dict(x) for x in c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE root_agent_id=? ORDER BY generation_number,created_ts""",
            (root,),
        ).fetchall()]
    return root, rows


def _model_summary(store, agent_id):
    raw = store.get_model(str(agent_id)) if agent_id else None
    if not isinstance(raw, dict):
        return {"present": False}
    schema = dict(raw.get("schema") or {})
    selection = dict(raw.get("selection_meta") or {})
    return {
        "present": True,
        "version": raw.get("version"),
        "model_revision": raw.get("model_revision"),
        "tournament_revision": raw.get("tournament_revision"),
        "schema_version": schema.get("version"),
        "schema_entities": list(schema.get("entities") or []),
        "selection_meta": selection,
        "heads": sorted(str(k) for k in (raw.get("heads") or {})),
    }


def _lineage_snapshot(store, rows):
    out = []
    for row in rows:
        item = dict(row)
        item["comparison"] = _json(item.pop("comparison_json", "{}"), {})
        aid = item.get("agent_id")
        item["agent"] = _safe_agent(store.get_agent_config(str(aid))) if aid else None
        item["model"] = _model_summary(store, aid)
        out.append(item)
    return out


def _candidate_builds(store, root, generation_agent_ids):
    with store.conn() as c:
        if not _table_exists(c, "agent_candidates"):
            return []
        ids = sorted(set(str(x) for x in generation_agent_ids if x))
        clauses = ["parent_agent_id=?"]
        args = [str(root)]
        if ids:
            marks = ",".join("?" for _ in ids)
            clauses += [f"parent_agent_id IN ({marks})", f"candidate_id IN ({marks})"]
            args += ids + ids
        rows = [dict(r) for r in c.execute(
            f"""SELECT * FROM agent_candidates WHERE {' OR '.join(clauses)}
                ORDER BY updated_ts""",
            args,
        ).fetchall()]
    for row in rows:
        row["comparison"] = _json(row.pop("comparison_json", "{}"), {})
        if "offline_gate_json" in row:
            row["offline_gate"] = _json(row.pop("offline_gate_json", "{}"), {})
    return rows


def _correction_labels(store, agent_ids, limit):
    ids = sorted(set(str(x) for x in agent_ids if x))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with store.conn() as c:
        if not _table_exists(c, "teaching_rl_labels"):
            return []
        rows = [dict(r) for r in c.execute(
            f"""SELECT * FROM teaching_rl_labels
                WHERE agent_id IN ({marks})
                ORDER BY sample_ts,id LIMIT ?""",
            (*ids, int(limit)),
        ).fetchall()]
    return rows


def _feedback_journal(store, root, limit):
    with store.conn() as c:
        if not _table_exists(c, "manual_feedback_journal"):
            return []
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM manual_feedback_journal
               WHERE COALESCE(root_agent_id,agent_id)=?
               ORDER BY created_ts DESC LIMIT ?""",
            (str(root), int(limit)),
        ).fetchall()]
    for row in rows:
        for key in (
            "context_signature_json", "immediate_effect_json", "learning_effect_json",
            "conflict_json",
        ):
            if key in row:
                row[key[:-5]] = _json(row.pop(key), [] if key == "conflict_json" else {})
    return rows


def _manual_context_snapshot(store, agent_ids):
    result = {}
    with store.conn() as c:
        exists = _table_exists(c, "manual_context_feedback")
        for aid in sorted(set(str(x) for x in agent_ids if x)):
            count = 0
            if exists:
                count = int(c.execute(
                    "SELECT COUNT(*) FROM manual_context_feedback WHERE agent_id=?", (aid,)
                ).fetchone()[0])
            try:
                scores = manual_scores(store, aid) if exists else {}
            except Exception as exc:
                scores = {"_error": f"{type(exc).__name__}: {exc}"}
            result[aid] = {
                "observations": count,
                "top_scores": dict(sorted(
                    ((k, v) for k, v in scores.items() if k != "_error"),
                    key=lambda kv: (-float(kv[1]), kv[0]),
                )[:20]),
                "error": scores.get("_error") if isinstance(scores, dict) else None,
            }
    return result


def _generation_by_agent(rows):
    return {
        str(row.get("agent_id")): row for row in rows if row.get("agent_id")
    }


def _base_room_forecast_read_only(context, entity_id, ts):
    """Mirror RoomBeliefModel.forecast without mutating values/live hysteresis."""
    ts = float(ts)
    area = context.area_for(entity_id)
    home = context.home
    with context.lock, home.lock:
        if not area:
            belief = {
                "occupancy": .5, "uncertainty": 1.0, "observability": 0.0,
                "known": False, "evidence_sources": [], "direct_active": [],
            }
            arrivals, support, hypotheses = [0.0, 0.0, 0.0], 0.0, []
            departure = 0.0
        else:
            belief = home._fuse_room(area, ts)
            arrivals, support, hypotheses = home._arrival_forecast(area, ts)
            departure = home._departure_forecast(area, ts, float(belief["occupancy"]))
        now = float(belief["occupancy"])
        occupancy_h = [
            max(0.0, min(1.0, now * (1.0 - departure * horizon / 5.0) +
                         (1.0 - now) * arrival))
            for horizon, arrival in zip((1, 3, 5), arrivals)
        ]
        trajectory_confidence = support / (support + 8.0)
        base = {
            "occupancy_now": now,
            "occupancy_in_1s": occupancy_h[0],
            "occupancy_in_3s": occupancy_h[1],
            "occupancy_in_5s": occupancy_h[2],
            "arrival_probability": arrivals[-1],
            "departure_probability": departure,
            "trajectory_confidence": trajectory_confidence,
            "area_id": area,
            "known": bool(belief["known"]),
            "support": support,
            "uncertainty": belief["uncertainty"],
            "observability": belief["observability"],
            "evidence_sources": belief["evidence_sources"],
            "arrival_probability_by_horizon": {
                "1s": arrivals[0], "3s": arrivals[1], "5s": arrivals[2],
            },
            "movement_hypotheses": [
                {
                    "path": list(h["path"]), "mass": float(h["mass"]),
                    "age_seconds": max(0.0, ts - float(h["ts"])),
                }
                for h in sorted(hypotheses, key=lambda row: -float(row["mass"]))[:home.MAX_HYPOTHESES]
            ],
            "model_version": home.VERSION,
        }
        # AdaptivePresence.evaluate is stateful. Use an exported clone so diagnostics can
        # reproduce the probability transform without altering the production hysteresis,
        # false-ON budget or metrics.
        presence_clone = type(context.adaptive_presence)(context.adaptive_presence.export())
        return context.augment_home_forecast(
            home, area, base, ts, presence_model=presence_clone, cache={}
        )


class _ReadOnlyContextProxy:
    def __init__(self, context):
        self._context = context
        self.excluded = set(getattr(context, "excluded", set()) or ())

    def forecast(self, entity_id, ts):
        return _base_room_forecast_read_only(self._context, entity_id, ts)


def _policy_for_model(engine, agent, raw):
    with engine.lock:
        states = dict(engine.state_map)
        registry = dict(engine.entity_registry)
    relevance = dict((getattr(engine, "context_relevance", {}) or {}).get(agent["id"]) or {})
    return MultiHorizonPolicy(
        agent, states, registry, set(), model=raw, relevance_scores=relevance,
        context_engine=_ReadOnlyContextProxy(engine.context),
    )


def _feature_rows(policy, features, labels):
    rows = []
    for index in range(int(policy.dims)):
        parts = labels.get(index) or []
        value = float(features.get(index, 0.0))
        if parts or abs(value) > 1e-12 or index >= int(policy.dims) - 7:
            rows.append({
                "index": int(index),
                "label": " / ".join(str(x) for x in parts) if parts else None,
                "value": value,
            })
    return rows


def _context_at_label(engine, store, agent, label):
    """Reconstruct historical sensor context and expose the shared RoomBelief tail."""
    raw = store.get_model(str(agent["id"]))
    if not isinstance(raw, dict):
        return {"error": "model missing"}
    try:
        policy = _policy_for_model(engine, agent, raw)
        ts = float(label["sample_ts"])
        states, temporal, _ = engine.teaching.point_context(
            engine, agent, ts, policy=policy,
        )
        features, labels, meta = policy.features(states, temporal, at_ts=ts)
        chosen, confidence, arms, horizon, support, novelty = policy.predict(features)

        with engine.lock:
            live_states = dict(engine.state_map)
            registry = dict(engine.entity_registry)
        historical_only = MultiHorizonPolicy(
            agent, live_states, registry, set(), model=raw,
            relevance_scores=dict((getattr(engine, "context_relevance", {}) or {}).get(agent["id"]) or {}),
            context_engine=None,
        )
        h_features, h_labels, h_meta = historical_only.features(states, temporal, at_ts=ts)

        home_start = max(0, int(policy.dims) - 7)
        home_diff = []
        for index in range(home_start, int(policy.dims)):
            left = float(features.get(index, 0.0))
            right = float(h_features.get(index, 0.0))
            if abs(left - right) > 1e-12:
                home_diff.append({
                    "index": index,
                    "label": " / ".join(str(x) for x in (labels.get(index) or [])) or None,
                    "with_context_engine": left,
                    "historical_sensor_only": right,
                    "delta": left - right,
                })

        target_state = states.get(agent["target_entity"]) or {}
        return {
            "current": target_state.get("state"),
            "selected_entity_states": {
                eid: (states.get(eid) or {}).get("state") for eid in policy.schema.entities
            },
            "prediction": {
                "value": chosen.get("value"), "index": chosen.get("index"),
                "confidence": confidence, "horizon": horizon,
                "support": support, "novelty": novelty,
                "arms": arms,
            },
            "feature_vector": _feature_rows(policy, features, labels),
            "feature_meta": meta,
            "historical_sensor_only_feature_vector": _feature_rows(
                historical_only, h_features, h_labels
            ),
            "historical_sensor_only_meta": h_meta,
            "home_tail_difference": home_diff,
            "home_context_contract": (
                "with_context_engine is a read-only reconstruction of shared RoomBelief "
                "at sample_ts using a cloned AdaptivePresence state; historical_sensor_only "
                "excludes the home provider entirely"
            ),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _debug_entities(engine, root_agent, lineage):
    target = root_agent.get("target_entity")
    area = engine.context.area_for(target)
    selected = []
    for row in lineage:
        selected.extend((row.get("model") or {}).get("schema_entities") or [])

    with engine.lock:
        states = dict(engine.state_map)
    same_area = [
        eid for eid in states
        if eid != target and area and engine.context.area_for(eid) == area
    ]
    relevance = dict((getattr(engine, "context_relevance", {}) or {}).get(root_agent["id"]) or {})
    ranked = [eid for eid, _ in sorted(
        relevance.items(), key=lambda kv: (-float(kv[1]), kv[0])
    )[:32]]

    tournament = getattr(engine, "context_tournament", None)
    challengers = []
    if tournament is not None:
        try:
            state = tournament.state(root_agent["id"])
            challengers = list(state.get("challenger_features") or [])
        except Exception:
            pass

    selected_set = set(selected)

    def priority(eid):
        text = str(eid).lower()
        return (
            0 if eid in selected_set else
            1 if any(token in text for token in (
                "presence", "occup", "motion", "radar", "humidity", "wilgot",
                "door", "drzwi", "co2", "temperature",
            )) else
            2 if eid in challengers else
            3 if eid in ranked else 4,
            eid,
        )

    ordered = []
    for eid in sorted(set(selected + same_area + challengers + ranked), key=priority):
        if eid not in ordered:
            ordered.append(eid)
        if len(ordered) >= MAX_DEBUG_ENTITIES:
            break
    return area, ordered


def _raw_window(store, entities, sample_ts, seconds, row_limit):
    if not entities:
        return []
    rows = []
    start = float(sample_ts) - float(seconds)
    end = float(sample_ts) + float(seconds)
    for row in store.archive_iter(start, end, set(entities), chunk_size=256):
        state = archived_state(row)
        rows.append({
            "entity_id": row.get("entity_id"),
            "ts": row.get("ts"),
            "state": state.get("state"),
            "source": row.get("source"),
            "user_id": row.get("user_id"),
        })
        if len(rows) >= int(row_limit):
            break
    return rows


def _cross_generation_predictions(engine, store, generations, sample_ts):
    out = []
    for generation in generations:
        aid = generation.get("agent_id")
        if not aid or not generation.get("model_retained", 1):
            continue
        agent = store.get_agent_config(str(aid))
        raw = store.get_model(str(aid))
        if not agent or not isinstance(raw, dict):
            continue
        try:
            policy = _policy_for_model(engine, agent, raw)
            states, temporal, _ = engine.teaching.point_context(
                engine, agent, float(sample_ts), policy=policy,
            )
            features, _, meta = policy.features(states, temporal, at_ts=float(sample_ts))
            chosen, confidence, _, horizon, support, novelty = policy.predict(features)
            out.append({
                "generation_id": generation.get("generation_id"),
                "generation_number": generation.get("generation_number"),
                "generation_type": generation.get("generation_type"),
                "agent_id": aid,
                "desired": chosen.get("value"),
                "confidence": confidence,
                "horizon": horizon,
                "support": support,
                "novelty": novelty,
                "home_forecast": (meta or {}).get("home_forecast"),
            })
        except Exception as exc:
            out.append({
                "generation_id": generation.get("generation_id"),
                "generation_number": generation.get("generation_number"),
                "agent_id": aid,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return out


def _current_context(engine, root_agent, debug_entities):
    target = root_agent["target_entity"]
    now = time.time()
    with engine.lock:
        states = dict(engine.state_map)
    area = engine.context.area_for(target)
    try:
        forecast = _base_room_forecast_read_only(engine.context, target, now)
    except Exception as exc:
        forecast = {"error": f"{type(exc).__name__}: {exc}"}
    sources = []
    for eid in debug_entities:
        if not area or engine.context.area_for(eid) != area:
            continue
        source = engine.context.evidence_metadata(eid)
        state = states.get(eid) or {}
        sources.append({
            "entity_id": eid,
            "state": state.get("state"),
            "last_changed": state.get("last_changed"),
            "role": source.get("role"),
            "value_semantics": source.get("value_semantics"),
            "occupancy_authority": source.get("occupancy_authority"),
            "selected_presence_source": bool(source.get("selected")),
        })
    return {
        "ts": now,
        "area_id": area,
        "forecast": forecast,
        "sources": sources,
    }


class CorrectLearningDebugService:
    def __init__(self, core, manager):
        self.core = core
        self.manager = manager
        self.store = core.STORE
        self.engine = core.ENGINE

    def export(self, ref, *, detail="summary", label_limit=DEFAULT_LABELS,
               window_seconds=DEFAULT_WINDOW_SECONDS,
               raw_rows_per_label=MAX_RAW_ROWS_PER_LABEL):
        detail = "full" if str(detail).lower() == "full" else "summary"
        label_limit = _bounded_int(label_limit, DEFAULT_LABELS, 1, MAX_LABELS)
        window_seconds = _bounded_float(window_seconds, DEFAULT_WINDOW_SECONDS, 5.0, 600.0)
        raw_rows_per_label = _bounded_int(
            raw_rows_per_label, MAX_RAW_ROWS_PER_LABEL, 0, MAX_RAW_ROWS_PER_LABEL
        )

        root, rows = _generation_rows(self.store, ref)
        root_agent = self.store.get_agent_config(root)
        if not root_agent:
            raise ValueError("agent not found")

        if not rows:
            rows = [{
                "generation_id": f"root:{root}", "root_agent_id": root,
                "agent_id": root, "parent_generation_id": None, "generation_number": 0,
                "generation_type": "live", "lifecycle_state": "live",
                "model_retained": 1, "comparison_json": "{}",
            }]

        lineage = _lineage_snapshot(self.store, rows)
        agent_ids = [row.get("agent_id") for row in rows if row.get("agent_id")]
        labels = _correction_labels(self.store, agent_ids, label_limit)
        builds = _candidate_builds(self.store, root, agent_ids)
        journal = _feedback_journal(self.store, root, label_limit)
        manual_context = _manual_context_snapshot(self.store, agent_ids)
        area, debug_entities = _debug_entities(self.engine, root_agent, lineage)
        generation_map = _generation_by_agent(rows)

        warnings = []
        active_labels = [x for x in labels if x.get("undone_ts") is None]
        if active_labels and sum(
            int((manual_context.get(str(aid)) or {}).get("observations") or 0)
            for aid in agent_ids
        ) == 0:
            warnings.append({
                "code": "correct_has_no_broad_manual_context",
                "message": (
                    "Correct labels exist but manual_context_feedback has no observations "
                    "for this lineage; missing sensors cannot be learned from these corrections."
                ),
            })

        result = {
            "contract": {
                "name": "correct_learning_debug",
                "version": CONTRACT_VERSION,
                "read_only": True,
                "bounded": True,
                "detail": detail,
                "max_labels": MAX_LABELS,
                "max_debug_entities": MAX_DEBUG_ENTITIES,
                "max_raw_rows_per_label": MAX_RAW_ROWS_PER_LABEL,
            },
            "generated_ts": time.time(),
            "root_agent": _safe_agent(root_agent),
            "root_agent_id": root,
            "area_id": area,
            "debug_entities": debug_entities,
            "lineage": lineage,
            "correct_labels": labels,
            "candidate_builds": builds,
            "manual_feedback_journal": journal,
            "manual_context": manual_context,
            "context_relevance": {
                str(aid): dict((getattr(self.engine, "context_relevance", {}) or {}).get(str(aid)) or {})
                for aid in agent_ids
            },
            "current_context": _current_context(self.engine, root_agent, debug_entities),
            "warnings": warnings,
        }

        tournament = getattr(self.engine, "context_tournament", None)
        if tournament is not None:
            try:
                result["context_tournament"] = tournament.state(root_agent["id"])
            except Exception as exc:
                result["context_tournament"] = {"error": f"{type(exc).__name__}: {exc}"}

        if detail == "full":
            diagnostics = []
            home_mismatch = False
            for label in labels:
                if label.get("undone_ts") is not None:
                    continue
                aid = str(label.get("agent_id") or "")
                agent = self.store.get_agent_config(aid)
                generation = generation_map.get(aid)
                row = {
                    "label": label,
                    "generation": None if generation is None else {
                        "generation_id": generation.get("generation_id"),
                        "generation_number": generation.get("generation_number"),
                        "generation_type": generation.get("generation_type"),
                        "lifecycle_state": generation.get("lifecycle_state"),
                    },
                }
                if agent:
                    row["context"] = _context_at_label(self.engine, self.store, agent, label)
                    if (row["context"] or {}).get("home_tail_difference"):
                        home_mismatch = True
                    row["cross_generation_predictions"] = _cross_generation_predictions(
                        self.engine, self.store, rows, label["sample_ts"]
                    )
                    if raw_rows_per_label > 0:
                        row["raw_context_window"] = _raw_window(
                            self.store, debug_entities, label["sample_ts"],
                            window_seconds, raw_rows_per_label,
                        )
                diagnostics.append(row)
            result["label_diagnostics"] = diagnostics
            if home_mismatch:
                result["warnings"].append({
                    "code": "historical_correct_home_tail_depends_on_shared_context_engine",
                    "message": (
                        "At least one Correct point changes when the shared ContextEngine "
                        "RoomBelief tail is removed. Inspect home_tail_difference before "
                        "changing learning weights."
                    ),
                })
        return result


def register_correct_learning_debug_route(registry, core, manager):
    service = CorrectLearningDebugService(core, manager)

    def handle(http, params):
        query = parse_qs(urlsplit(http.path).query)
        try:
            payload = service.export(
                unquote(params["agent_id"]),
                detail=(query.get("detail") or ["summary"])[0],
                label_limit=(query.get("label_limit") or [DEFAULT_LABELS])[0],
                window_seconds=(query.get("window_seconds") or [DEFAULT_WINDOW_SECONDS])[0],
                raw_rows_per_label=(query.get("raw_rows_per_label") or [MAX_RAW_ROWS_PER_LABEL])[0],
            )
            return http.send_json(200, payload)
        except ValueError as exc:
            return http.send_json(404, {"error": str(exc)})
        except Exception as exc:
            return http.send_json(500, {
                "error": f"Correct debug export failed: {type(exc).__name__}: {exc}"
            })

    registry.register(
        "GET",
        "debug.correct_learning",
        r"^/api/agents/(?P<agent_id>[^/]+)/debug/correct-learning$",
        handle,
        require_trusted=True,
        require_runtime=True,
        priority=250,
    )
    manager.correct_learning_debug = service
    manager.correct_learning_debug_contract = (
        "read_only_bounded_lineage_labels_features_room_context_and_raw_windows"
    )
    return service
