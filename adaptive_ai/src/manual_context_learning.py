"""Learn which *unselected* Home Assistant entities explain manual corrections.

Manual corrections are the strongest preference signal in HomeMind.  The live policy can
only update features already present in its compact schema, so this module observes a
broader candidate universe at every explicit/direct-device correction.  It persists a
bounded scalar snapshot of all eligible non-electrical, non-actuator context, estimates
which entities correlate with the demonstrated target value, and promotes strong manual
signals into the compact policy schema.

Schema promotion is online and conservative: existing feature weights are copied by
semantic label, new slots start at the ridge prior, fixed home-intelligence features are
preserved, and the correction that triggered the promotion is then learned using the new
schema.  Historical/raw archives are untouched.
"""
from collections import defaultdict
import json
import math
import time
import uuid


_PATCHED = False
_CORE = None
_BASE_SELECTOR = None
_SCORE_CACHE = {}
_OBSERVATION_CACHE = {}


def _option(name, default):
    from settings import OPTIONS
    try:
        return type(default)(OPTIONS.get(name, default))
    except Exception:
        return default


def _ensure_table(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS manual_context_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                created_ts REAL NOT NULL,
                desired_value REAL NOT NULL,
                rejected_value REAL,
                source TEXT NOT NULL,
                user_id TEXT,
                snapshot_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_manual_context_agent_id
                ON manual_context_feedback(agent_id, id DESC);
            """
        )


def _candidate_snapshot(agent, state_map, registry, now=None):
    """Return every eligible context scalar plus its state-change recency.

    This intentionally mirrors the broad candidate gate used by normal context selection:
    unusual ESPHome channels, phone/car/template sensors, camera scores and virtual
    entities are allowed.  Only the target itself, controllable inputs and explicit
    electrical-unit telemetry are excluded.
    """
    from context import (
        context_scalar, controllable_context_exclusions, electrical_context_exclusions,
        is_context_candidate_entity,
    )
    from settings import parse_ts

    now = time.time() if now is None else float(now)
    excluded_control, _ = controllable_context_exclusions(state_map, registry)
    excluded_electrical, _ = electrical_context_exclusions(state_map, registry)
    excluded = excluded_control | excluded_electrical
    target = str(agent.get("target_entity") or "")
    out = {}
    max_entities = max(32, int(_option("manual_context_observer_max_entities", 512)))
    for eid, state in (state_map or {}).items():
        if eid == target or eid in excluded:
            continue
        if not is_context_candidate_entity(eid, state, excluded):
            continue
        value = context_scalar(eid, state, agent)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        changed = parse_ts((state or {}).get("last_changed") or (state or {}).get("last_updated"))
        age = max(0.0, now - float(changed)) if changed else None
        out[eid] = {"v": round(value, 8), "age": None if age is None else round(age, 3)}
        if len(out) >= max_entities:
            break
    return out


def _insert_snapshot(store, agent_id, desired, rejected, source, user_id, snapshot, created_ts=None):
    created_ts = time.time() if created_ts is None else float(created_ts)
    limit = max(16, int(_option("manual_context_max_snapshots", 256)))
    with store.lock, store.conn() as c:
        c.execute(
            """INSERT INTO manual_context_feedback
               (agent_id,created_ts,desired_value,rejected_value,source,user_id,snapshot_json)
               VALUES(?,?,?,?,?,?,?)""",
            (str(agent_id), created_ts, float(desired),
             None if rejected is None else float(rejected), str(source or "manual"),
             None if user_id is None else str(user_id),
             json.dumps(snapshot, separators=(",", ":"), sort_keys=True)),
        )
        c.execute(
            """DELETE FROM manual_context_feedback
               WHERE agent_id=? AND id NOT IN (
                   SELECT id FROM manual_context_feedback WHERE agent_id=? ORDER BY id DESC LIMIT ?
               )""",
            (str(agent_id), str(agent_id), limit),
        )
    _SCORE_CACHE.pop(str(agent_id), None)
    _OBSERVATION_CACHE.pop(str(agent_id), None)


def _rows(store, agent_id):
    limit = max(16, int(_option("manual_context_max_snapshots", 256)))
    with store.conn() as c:
        rows = c.execute(
            """SELECT id,created_ts,desired_value,rejected_value,source,user_id,snapshot_json
               FROM manual_context_feedback WHERE agent_id=? ORDER BY id DESC LIMIT ?""",
            (str(agent_id), limit),
        ).fetchall()
    out = []
    for row in rows:
        try:
            snap = json.loads(row["snapshot_json"] or "{}")
        except Exception:
            snap = {}
        out.append({
            "id": int(row["id"]), "ts": float(row["created_ts"]),
            "desired": float(row["desired_value"]),
            "rejected": None if row["rejected_value"] is None else float(row["rejected_value"]),
            "source": row["source"], "user_id": row["user_id"], "snapshot": snap,
        })
    return out


def _weighted_corr(xs, ys, ws):
    total = sum(ws)
    if total <= 1e-12:
        return 0.0
    mx = sum(w*x for w, x in zip(ws, xs)) / total
    my = sum(w*y for w, y in zip(ws, ys)) / total
    vx = sum(w*(x-mx)*(x-mx) for w, x in zip(ws, xs)) / total
    vy = sum(w*(y-my)*(y-my) for w, y in zip(ws, ys)) / total
    if vx <= 1e-10 or vy <= 1e-10:
        return 0.0
    cov = sum(w*(x-mx)*(y-my) for w, x, y in zip(ws, xs, ys)) / total
    return max(-1.0, min(1.0, cov / math.sqrt(vx*vy)))


def manual_scores(store, agent_id, now=None):
    """Supervised relevance from full-context manual snapshots.

    A candidate needs repeated corrections *and* variation in the demonstrated target
    value before it can become a strong promotion signal.  This prevents one accidental
    correction from making every coincident sensor look causal.  Recency-to-correction is
    only a small bonus; the main term is cross-correction correlation.
    """
    aid = str(agent_id)
    cached = _SCORE_CACHE.get(aid)
    if cached is not None:
        return dict(cached)
    rows = _rows(store, aid)
    _OBSERVATION_CACHE[aid] = len(rows)
    min_samples = max(3, int(_option("manual_context_min_samples", 4)))
    half_life_days = max(1.0, float(_option("policy_half_life_days", 30)))
    now = time.time() if now is None else float(now)
    by_entity = defaultdict(lambda: ([], [], [], []))
    for row in rows:
        age_days = max(0.0, now - row["ts"]) / 86400.0
        row_weight = math.exp(-math.log(2.0) * age_days / half_life_days)
        for eid, item in (row["snapshot"] or {}).items():
            try:
                value = float(item.get("v") if isinstance(item, dict) else item)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            recency_age = item.get("age") if isinstance(item, dict) else None
            xs, ys, ws, ages = by_entity[eid]
            xs.append(value); ys.append(float(row["desired"])); ws.append(row_weight)
            ages.append(None if recency_age is None else max(0.0, float(recency_age)))

    scores = {}
    for eid, (xs, ys, ws, ages) in by_entity.items():
        n = len(xs)
        if n < min_samples:
            continue
        # At least two meaningfully different demonstrated values are required for a
        # candidate to displace an existing feature.  One-sided corrections are retained
        # and will become useful as soon as contrasting evidence arrives.
        if max(ys) - min(ys) <= 1e-6:
            continue
        corr = abs(_weighted_corr(xs, ys, ws))
        coverage = min(1.0, n / max(float(min_samples * 2), 1.0))
        known_ages = [a for a in ages if a is not None]
        recent = 0.0
        if known_ages:
            # A weak bonus for signals that actually changed near a correction.  It is
            # deliberately capped so high-rate telemetry cannot win without correlation.
            recent = sum(math.exp(-a / 12.0) for a in known_ages) / len(known_ages)
        score = min(1.0, corr * coverage + 0.15 * recent * coverage)
        if score > 0.01:
            scores[eid] = round(score, 6)
    _SCORE_CACHE[aid] = dict(scores)
    return scores


def _selection_limit(agent):
    from context import is_fast_reactive_agent
    from settings import OPTIONS
    dims = int(OPTIONS.get("feature_dimensions", 128))
    limit = min(int(OPTIONS.get("max_context_entities", 28)), max(4, (dims - 9) // 4))
    if is_fast_reactive_agent(agent):
        limit = min(limit, max(2, int(OPTIONS.get("fast_max_context_entities", 8))))
    return limit


def _promote_manual_entities(agent, state_map, selected, meta, scores):
    """Reserve a few schema slots for strongly proven manual-feedback candidates."""
    from context import context_scalar, controllable_context_exclusions, electrical_context_exclusions

    threshold = max(0.05, min(0.99, float(_option("manual_context_promote_score", 0.55))))
    reserve = max(0, int(_option("manual_context_reserve", 2)))
    if reserve <= 0 or not scores:
        meta = dict(meta or {})
        meta["manual_context_scores"] = {}
        meta["manual_context_observations"] = int(_OBSERVATION_CACHE.get(str(agent.get("id")), 0))
        return list(selected), meta

    excluded_control, _ = controllable_context_exclusions(state_map, _CORE.ENGINE.entity_registry if _CORE and _CORE.ENGINE else {})
    excluded_electrical, _ = electrical_context_exclusions(state_map, _CORE.ENGINE.entity_registry if _CORE and _CORE.ENGINE else {})
    excluded = excluded_control | excluded_electrical
    candidates = []
    for eid, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        if score < threshold or eid == agent.get("target_entity") or eid in excluded:
            continue
        state = (state_map or {}).get(eid)
        if state is None or context_scalar(eid, state, agent) is None:
            continue
        candidates.append((eid, float(score)))
        if len(candidates) >= reserve:
            break

    out = list(selected)
    limit = _selection_limit(agent)
    reasons = {k: list(v) for k, v in dict((meta or {}).get("selection_reasons") or {}).items()}
    protected = set((meta or {}).get("primary_local_sensors") or [])
    primary = (meta or {}).get("primary_occupancy_sensor")
    if primary:
        protected.add(primary)
    promoted = []
    for eid, score in candidates:
        if eid in out:
            reasons.setdefault(eid, [])
            if "manual-feedback" not in reasons[eid]:
                reasons[eid].append("manual-feedback")
            promoted.append(eid)
            continue
        if len(out) < limit:
            out.append(eid)
        else:
            replacement = None
            for idx in range(len(out)-1, -1, -1):
                old = out[idx]
                if old in protected:
                    continue
                old_manual = float(scores.get(old, 0.0))
                if old_manual < score:
                    replacement = idx
                    break
            if replacement is None:
                continue
            old = out[replacement]
            out[replacement] = eid
            reasons.pop(old, None)
        reasons.setdefault(eid, [])
        reasons[eid].append("manual-feedback")
        promoted.append(eid)

    meta = dict(meta or {})
    meta["selection_reasons"] = {eid: reasons.get(eid, []) for eid in out}
    meta["selected_entities"] = len(out)
    meta["manual_context_scores"] = {eid: round(float(score), 4) for eid, score in sorted(scores.items(), key=lambda kv: -kv[1])[:12]}
    meta["manual_context_promoted"] = promoted
    meta["manual_context_observations"] = int(_OBSERVATION_CACHE.get(str(agent.get("id")), 0))
    return out, meta


def _canonical_label(labels):
    text = " / ".join(labels if isinstance(labels, (list, tuple)) else [str(labels)])
    if text.startswith("interaction:") and "×" in text:
        body = text[len("interaction:"):]
        a, b = body.split("×", 1)
        text = "interaction:" + "×".join(sorted((a, b)))
    return text


def _migrate_schema(policy, new_entities, new_meta):
    """Change explicit entity slots while preserving evidence for matching features."""
    schema_type = type(policy.schema)
    new_schema = schema_type(policy.dims, new_entities)
    old_entities = list(policy.schema.entities)
    if old_entities == list(new_schema.entities):
        policy.selection_meta = dict(new_meta or policy.selection_meta or {})
        return {"changed": False, "added": [], "removed": []}

    old_labels = {_canonical_label(v): i for i, v in policy.schema.labels().items()}
    new_labels = {_canonical_label(v): i for i, v in new_schema.labels().items()}
    mapping = {new_i: old_labels[label] for label, new_i in new_labels.items() if label in old_labels}
    # Home-intelligence slots live at fixed tail indices and are not part of schema.labels().
    for idx in range(max(0, policy.dims - 7), policy.dims):
        mapping[idx] = idx

    for head in policy.heads.values():
        for arm in range(len(head.actions)):
            old_a, old_b = head.a[arm], head.b[arm]
            old_sum, old_sq = head.ctx_sum[arm], head.ctx_sq[arm]
            new_a = [1.0] * policy.dims
            new_b = [0.0] * policy.dims
            new_sum = [0.0] * policy.dims
            new_sq = [0.0] * policy.dims
            for new_idx, old_idx in mapping.items():
                if old_idx < len(old_a) and new_idx < policy.dims:
                    new_a[new_idx] = old_a[old_idx]
                    new_b[new_idx] = old_b[old_idx]
                    new_sum[new_idx] = old_sum[old_idx]
                    new_sq[new_idx] = old_sq[old_idx]
            head.a[arm] = new_a
            head.b[arm] = new_b
            head.ctx_sum[arm] = new_sum
            head.ctx_sq[arm] = new_sq
    policy.schema = new_schema
    policy.selection_meta = dict(new_meta or {})
    policy.model_revision = str(uuid.uuid4())
    return {
        "changed": True,
        "added": [x for x in new_schema.entities if x not in old_entities],
        "removed": [x for x in old_entities if x not in new_schema.entities],
    }


def _refresh_policy(core, agent, state_map, scores):
    if not scores or str(agent.get("training_state") or "") == "training":
        return {"changed": False, "added": [], "removed": []}
    engine = core.ENGINE
    policy = engine.policy(agent)
    from ha import AUTOMATION_KNOWLEDGE
    hints, _ = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
    base_rel = dict(engine.context_relevance.get(agent["id"]) or {})
    selected, meta = _BASE_SELECTOR(
        agent, state_map, dict(engine.entity_registry), hints,
        relevance_scores=base_rel,
    )
    selected, meta = _promote_manual_entities(agent, state_map, selected, meta, scores)
    result = _migrate_schema(policy, selected, meta)
    if result["changed"]:
        core.STORE.save_model(agent["id"], policy.serialize())
        rt = engine.runtime.setdefault(agent["id"], {})
        rt["context_meta"] = dict(policy.selection_meta)
        core.STORE.event(
            agent["id"], "info", "manual_context_schema_refresh",
            "Manual corrections changed the selected context schema",
            {"added": result["added"], "removed": result["removed"],
             "manual_scores": meta.get("manual_context_scores") or {}},
        )
    return result


def observe(core, agent, state_map, desired, rejected=None, source="manual", user_id=None, refresh_policy=True):
    """Persist broad correction context and, when justified, refresh the live schema."""
    if core is None or core.STORE is None or core.ENGINE is None:
        return {"recorded": False, "reason": "runtime unavailable"}
    snapshot = _candidate_snapshot(agent, state_map, dict(core.ENGINE.entity_registry))
    if not snapshot:
        return {"recorded": False, "reason": "no eligible context"}
    _insert_snapshot(core.STORE, agent["id"], desired, rejected, source, user_id, snapshot)
    scores = manual_scores(core.STORE, agent["id"])
    refresh = (_refresh_policy(core, agent, state_map, scores) if refresh_policy
               else {"changed": False, "added": [], "removed": []})
    return {
        "recorded": True,
        "candidates": len(snapshot),
        "observations": int(_OBSERVATION_CACHE.get(str(agent["id"]), 0)),
        "scores": {k: round(float(v), 4) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])[:12]},
        "schema_changed": bool(refresh.get("changed")),
        "added": list(refresh.get("added") or []),
        "removed": list(refresh.get("removed") or []),
        "schema_refresh_deferred": not refresh_policy,
    }


def install(core):
    """Install persistent observer and make future policy selection manual-feedback aware."""
    global _PATCHED, _CORE, _BASE_SELECTOR
    _CORE = core
    _ensure_table(core.STORE)
    if _PATCHED:
        return
    import policy as policy_module
    _BASE_SELECTOR = policy_module.select_context_entities

    def select_with_manual(agent, state_map, registry, hint_entities, max_entities=None, relevance_scores=None):
        selected, meta = _BASE_SELECTOR(
            agent, state_map, registry, hint_entities,
            max_entities=max_entities, relevance_scores=relevance_scores,
        )
        scores = manual_scores(core.STORE, agent.get("id")) if agent.get("id") else {}
        return _promote_manual_entities(agent, state_map, selected, meta, scores)

    policy_module.select_context_entities = select_with_manual
    _PATCHED = True
    core.STORE.event(None, "info", "manual_context_learning_ready",
                     "Full-context manual correction observer is active", None)
