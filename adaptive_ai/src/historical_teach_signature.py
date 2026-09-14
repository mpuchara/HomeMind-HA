import json
import time


def refresh(core, manager, agent, label_id, sample_ts=None):
    import teaching
    engine = core.ENGINE
    ts = float(sample_ts if sample_ts is not None else time.time())
    if sample_ts is None:
        with engine.lock: states = dict(engine.state_map)
        temporal = engine.temporal_history; policy = engine.policy(agent)
    else:
        states, temporal, policy = manager.point_context(engine, agent, ts)
    sig = teaching.signature(policy, states, temporal, ts)
    if not sig: return False
    with manager.lock, core.STORE.lock, core.STORE.conn() as c:
        c.execute("UPDATE teaching_labels SET signature_json=? WHERE id=?", (json.dumps(sig), int(label_id)))
        manager.cache.pop(agent["id"], None)
    return True
