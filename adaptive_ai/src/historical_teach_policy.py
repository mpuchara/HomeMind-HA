USER_ID = "adaptive_ai_history_teach"
WEIGHT = 8


def train(core, manager, agent, desired, rejected, sample_ts=None):
    engine = core.ENGINE
    if sample_ts is None:
        with engine.lock: states = dict(engine.state_map)
        temporal = engine.temporal_history; policy = engine.policy(agent)
    else:
        states, temporal, policy = manager.point_context(engine, agent, float(sample_ts))
    features, _, _ = policy.features(states, temporal, at_ts=sample_ts)
    wanted = min(range(len(policy.actions)), key=lambda i: abs(float(policy.actions[i])-float(desired)))
    old = None if rejected is None else min(range(len(policy.actions)), key=lambda i: abs(float(policy.actions[i])-float(rejected)))
    for _ in range(WEIGHT):
        if old is not None and old != wanted:
            for h in policy.horizons: policy.update(h, old, features, -1.0)
        for h in policy.horizons: policy.update(h, wanted, features, 1.0)
    core.STORE.save_model(agent["id"], policy.serialize())
    if old is not None and old != wanted:
        core.STORE.add_feedback(agent["id"], old, policy.actions[old], -1.0, "historical Teach rejected prediction", features, USER_ID)
    core.STORE.add_feedback(agent["id"], wanted, policy.actions[wanted], 1.0, "historical Teach supervised label", features, USER_ID)
    return True
