def install(core):
    engine = core.ENGINE
    if getattr(engine, "_teaching_reward_patch", False): return
    original = engine._reward_pending

    def reward(agent, rt, value, reason, user_id=None, experience=None):
        pending = experience if experience is not None else rt.get("pending")
        if pending and pending.get("teaching_id") and not pending.get("experiment"):
            marker = pending.get("teaching_id")
            pending["teaching_id"] = 0
            try:
                return original(agent, rt, value, reason + " (Teach confirmed)", user_id, experience)
            finally:
                pending["teaching_id"] = marker
        return original(agent, rt, value, reason, user_id, experience)

    engine._reward_pending = reward
    engine._teaching_reward_patch = True
