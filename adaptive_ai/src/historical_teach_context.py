import math

USER_ID = "adaptive_ai_history_teach"


def observe(core, agent, ts, desired, rejected):
    import manual_context_learning as m
    from context import archived_state, context_scalar, controllable_context_exclusions, electrical_context_exclusions, is_context_candidate_entity
    with core.ENGINE.lock:
        live = dict(core.ENGINE.state_map); registry = dict(core.ENGINE.entity_registry)
    a, _ = controllable_context_exclusions(live, registry)
    b, _ = electrical_context_exclusions(live, registry)
    excluded = a | b
    limit = max(32, int(m._option("manual_context_observer_max_entities", 512)))
    ids = [eid for eid, st in live.items() if eid != agent["target_entity"] and eid not in excluded and is_context_candidate_entity(eid, st, excluded)][:limit]
    snap = {}
    with core.STORE.conn() as c:
        for eid in ids:
            row = c.execute("SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (eid, float(ts))).fetchone()
            if not row: continue
            raw = dict(row); value = context_scalar(eid, archived_state(raw), agent)
            try: value = float(value)
            except (TypeError, ValueError): continue
            if math.isfinite(value): snap[eid] = {"v": value, "age": max(0.0, float(ts)-float(raw["ts"]))}
    if not snap: return {"recorded": False}
    m._insert_snapshot(core.STORE, agent["id"], desired, rejected, "history_teach", USER_ID, snap)
    scores = m.manual_scores(core.STORE, agent["id"])
    rt = core.ENGINE.runtime.get(agent["id"], {})
    refresh = m._refresh_policy(core, agent, live, scores) if not (rt.get("pending") or rt.get("outcomes")) else {"changed": False}
    return {"recorded": True, "candidates": len(snap), "scores": scores, "schema_changed": bool(refresh.get("changed"))}
