import math


def install(core):
    import manual_context_learning as m
    if getattr(m, "_one_sided_scores", False): return
    base_scores = m.manual_scores

    def scores(store, agent_id, now=None):
        out = dict(base_scores(store, agent_id, now))
        rows = m._rows(store, agent_id)
        minimum = max(3, int(m._option("manual_context_min_samples", 4)))
        if len(rows) < minimum: return out
        desired = [float(r["desired"]) for r in rows]
        if max(desired)-min(desired) > 1e-6: return out
        from context import entity_capability_tags
        with core.ENGINE.lock: states = dict(core.ENGINE.state_map)
        by = {}
        for row in rows:
            for eid, item in (row.get("snapshot") or {}).items():
                if not (entity_capability_tags(eid, states.get(eid) or {}) & {"occupancy", "activity"}): continue
                age = item.get("age") if isinstance(item, dict) else None
                if age is not None: by.setdefault(eid, []).append(max(0.0, float(age)))
        for eid, ages in by.items():
            if len(ages) < minimum: continue
            coverage = min(1.0, len(ages)/float(minimum*2))
            recent = sum(math.exp(-a/12.0) for a in ages)/len(ages)
            score = .70*coverage*recent
            if score > out.get(eid, 0.0): out[eid] = round(score, 6)
        return out

    m.manual_scores = scores
    m._one_sided_scores = True
