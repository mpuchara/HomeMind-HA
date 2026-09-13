FAST_ACTION_INTERVAL_SECONDS = 0.25
FAST_SETTLING_SECONDS = 0.10
FAST_ACK_TIMEOUT_SECONDS = 2.0


def is_fast_target(agent):
    entity = str((agent or {}).get("target_entity") or "")
    prop = str((agent or {}).get("target_property") or "")
    domain = entity.split(".", 1)[0]
    if domain == "light" and prop in ("power", "brightness_pct"):
        return True
    return domain in ("switch", "input_boolean") and prop == "power"


def _positive_cap(value, maximum, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback)
    if number <= 0:
        number = float(fallback)
    return min(number, float(maximum))


def normalize_fast_payload(payload, existing=None, include_defaults=False):
    merged = dict(existing or {})
    merged.update(payload or {})
    if not is_fast_target(merged):
        return dict(payload or {})
    out = dict(payload or {})
    if include_defaults or "action_interval" in out:
        out["action_interval"] = _positive_cap(out.get("action_interval", merged.get("action_interval")), FAST_ACTION_INTERVAL_SECONDS, FAST_ACTION_INTERVAL_SECONDS)
    if include_defaults or "settling_seconds" in out:
        out["settling_seconds"] = _positive_cap(out.get("settling_seconds", merged.get("settling_seconds")), FAST_SETTLING_SECONDS, FAST_SETTLING_SECONDS)
    if include_defaults or "ack_timeout" in out:
        out["ack_timeout"] = _positive_cap(out.get("ack_timeout", merged.get("ack_timeout")), FAST_ACK_TIMEOUT_SECONDS, FAST_ACK_TIMEOUT_SECONDS)
    return out


def migrate_existing_fast_agents(core):
    changed = []
    for agent in core.STORE.list_agent_configs():
        if not is_fast_target(agent):
            continue
        desired = normalize_fast_payload({}, existing=agent, include_defaults=True)
        update = {}
        for key, value in desired.items():
            try:
                current = float(agent.get(key) or 0.0)
            except (TypeError, ValueError):
                current = 0.0
            if abs(current - float(value)) > 1e-9:
                update[key] = value
        if update:
            core.STORE.update_agent(agent["id"], update)
            changed.append({"agent_id": agent["id"], "target": agent["target_entity"], **update})
        core.STORE.meta_set("manual_hold:" + agent["id"], "0")
        core.STORE.meta_set("manual_hold_source:" + agent["id"], "")
        runtime = core.ENGINE.runtime.get(agent["id"])
        if runtime is not None:
            runtime["manual_override_until"] = 0.0
    return changed


def install(core):
    if getattr(core, "_FAST_RUNTIME_INSTALLED", False):
        return []
    if not core.runtime_available() or core.ENGINE is None or core.STORE is None:
        return []

    store = core.STORE
    engine = core.ENGINE
    original_create = store.create_agent
    original_update = store.update_agent
    original_manual_hold = engine.set_manual_hold
    original_default = core.default_action_interval

    def create_agent(payload):
        return original_create(normalize_fast_payload(payload, include_defaults=True))

    def update_agent(agent_id, payload):
        existing = store.get_agent_config(agent_id)
        return original_update(agent_id, normalize_fast_payload(payload, existing=existing, include_defaults=False))

    def set_manual_hold(agent, rt, timestamp):
        if is_fast_target(agent):
            rt["manual_override_until"] = 0.0
            store.meta_set("manual_hold:" + agent["id"], "0")
            store.meta_set("manual_hold_source:" + agent["id"], "")
            rt["manual_feedback_ts"] = float(timestamp)
            engine.wake_event.set()
            return
        return original_manual_hold(agent, rt, timestamp)

    def default_action_interval(entity_id, target_property):
        probe = {"target_entity": entity_id, "target_property": target_property}
        if is_fast_target(probe):
            return FAST_ACTION_INTERVAL_SECONDS
        return float(original_default(entity_id, target_property))

    store.create_agent = create_agent
    store.update_agent = update_agent
    engine.set_manual_hold = set_manual_hold
    core.default_action_interval = default_action_interval
    try:
        import history as history_module
        history_module.default_action_interval = default_action_interval
    except Exception:
        pass

    core._FAST_RUNTIME_INSTALLED = True
    return migrate_existing_fast_agents(core)
