import types


def install(core):
    engine = core.ENGINE; manager = engine.teaching
    if getattr(manager, "_supervised_graph_teach", False): return
    original = manager.teach

    def teach(self, eng, agent, desired=None, sample_ts=None):
        rejected = (eng.runtime.get(agent["id"], {}).get("last_prediction") if sample_ts is None
                    else self.point(eng, agent, sample_ts).get("desired"))
        result = original(eng, agent, desired, sample_ts)
        agent = core.STORE.get_agent_config(agent["id"]) or agent
        try:
            if sample_ts is None:
                import manual_context_learning as m
                with eng.lock: states = dict(eng.state_map)
                rt = eng.runtime.get(agent["id"], {})
                info = m.observe(core, agent, states, result["desired_value"], rejected=rejected,
                                 source="history_teach_live", user_id="adaptive_ai_history_teach",
                                 refresh_policy=not bool(rt.get("pending") or rt.get("outcomes")))
            else:
                from historical_teach_context import observe
                info = observe(core, agent, result["sample_ts"], result["desired_value"], rejected)
            from historical_teach_signature import refresh
            refresh(core, self, agent, result["label_id"], sample_ts=result["sample_ts"] if sample_ts is not None else None)
            self.refresh(eng, agent)
            result.update(context_learning=info)
        except Exception as exc:
            core.STORE.event(agent["id"], "warning", "graph_teach_context_failed", str(exc), {"label_id": result["label_id"]})
            result.update(context_learning_error=f"{type(exc).__name__}: {exc}")
        return result

    manager.teach = types.MethodType(teach, manager)
    manager._supervised_graph_teach = True
