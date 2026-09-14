def install(core):
    import manual_context_learning as m
    if getattr(m, "_manual_primary_patch", False): return
    base = m._promote_manual_entities

    def promote(agent, state_map, selected, meta, scores):
        selected, meta = base(agent, state_map, selected, meta, scores)
        from context import entity_capability_tags
        threshold = max(.05, min(.99, float(m._option("manual_context_promote_score", .55))))
        ranked = [(float(scores.get(eid, 0)), eid) for eid in selected
                  if float(scores.get(eid, 0)) >= threshold
                  and entity_capability_tags(eid, (state_map or {}).get(eid) or {}) & {"occupancy", "activity"}]
        if ranked:
            score, eid = max(ranked)
            meta = dict(meta or {})
            meta["primary_occupancy_sensor"] = eid
            meta["manual_primary_occupancy_score"] = round(score, 4)
            reasons = dict(meta.get("selection_reasons") or {})
            reasons[eid] = list(reasons.get(eid) or [])
            if "manual-primary" not in reasons[eid]: reasons[eid].append("manual-primary")
            meta["selection_reasons"] = reasons
        return selected, meta

    m._promote_manual_entities = promote
    m._manual_primary_patch = True
