FAST_ACTION_INTERVAL_SECONDS = 0.25
FAST_SETTLING_SECONDS = 0.10
FAST_ACK_TIMEOUT_SECONDS = 2.0
FAST_OFF_CONFIRMATION_SECONDS = 6.0


def is_fast_target(agent):
    entity = str((agent or {}).get("target_entity") or "")
    prop = str((agent or {}).get("target_property") or "")
    domain = entity.split(".", 1)[0]
    if domain == "light" and prop in ("power", "brightness_pct"):
        return True
    return domain in ("switch", "input_boolean") and prop == "power"


FAST_ON_PRESENCE_ROLES = {
    "pir", "radar_occupancy", "occupancy_binary", "tracker",
}


def fast_light_presence_evidence(forecast):
    """Return independent positive room-presence sources suitable for ON anticipation."""
    if not isinstance(forecast, dict) or not bool(forecast.get("known")):
        return []
    try:
        near_presence = max(
            float(forecast.get("occupancy_now", 0.0) or 0.0),
            float(forecast.get("occupancy_in_1s", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        return []
    if near_presence < 0.5:
        return []
    positive = {}
    for row in forecast.get("evidence_sources") or ():
        if not isinstance(row, dict) or not bool(row.get("available")):
            continue
        role = str(row.get("role") or "")
        if role not in FAST_ON_PRESENCE_ROLES:
            continue
        try:
            contribution = float(row.get("contribution") or 0.0)
            communication = float(row.get("communication_reliability") or 0.0)
            freshness = float(row.get("evidence_freshness") or 0.0)
        except (TypeError, ValueError):
            continue
        if contribution <= 0.0 or communication < 0.5 or freshness < 0.5:
            continue
        eid = str(row.get("entity_id") or "")
        if eid:
            positive[eid] = {
                "entity_id": eid,
                "role": role,
                "contribution": contribution,
            }
    return [positive[eid] for eid in sorted(positive)]


def fast_light_on_assist_action(agent, current, desired, decision_source, arms, forecast):
    """Select ON only when independent presence evidence agrees and ON remains plausible.

    This is not a fallback rule that ignores the learned model. It merely lets strong,
    independent RoomBelief evidence break an OFF decision when the existing policy's own
    ON UCB reaches the OFF mean. Explicit instructions/preferences/experiments are left
    untouched and all Executor confidence/support/novelty guards still apply in Control.
    """
    target = str((agent or {}).get("target_entity") or "")
    prop = str((agent or {}).get("target_property") or "")
    if target.split(".", 1)[0] != "light" or prop != "power":
        return None
    if str(decision_source or "") != "historical_policy_bootstrap":
        return None
    try:
        if float(current) >= 0.5 or float(desired) >= 0.5:
            return None
    except (TypeError, ValueError):
        return None
    evidence = fast_light_presence_evidence(forecast)
    if len(evidence) < 2:
        return None
    on_arm = next((a for a in (arms or ()) if float(a.get("value", 0.0)) >= 0.5), None)
    off_arm = next((a for a in (arms or ()) if float(a.get("value", 0.0)) < 0.5), None)
    if on_arm is None or off_arm is None:
        return None
    try:
        on_ucb = float(on_arm.get("ucb"))
        off_mean = float(off_arm.get("mean"))
    except (TypeError, ValueError):
        return None
    if on_ucb + 1e-12 < off_mean:
        return None
    return int(on_arm.get("index"))


def stabilize_fast_light_power_decision(agent, rt, current, desired, decision_source,
                                        timestamp, confirmation_seconds=None):
    """Suppress transient learned OFF flips without delaying explicit user intent.

    Fast lighting is intentionally asymmetric: ON remains immediate, while only the
    statistical historical-policy path must sustain an OFF recommendation before an
    already-ON lamp is allowed to transition. Explicit instructions/preferences,
    experiments and a physical user OFF bypass this filter. Runtime state is ephemeral;
    no model/data schema is reinterpreted.
    """
    target = str((agent or {}).get("target_entity") or "")
    prop = str((agent or {}).get("target_property") or "")
    applies = target.split(".", 1)[0] == "light" and prop == "power"
    statistical = str(decision_source or "") == "historical_policy_bootstrap"
    try:
        current_value = float(current)
        desired_value = float(desired)
    except (TypeError, ValueError):
        rt.pop("fast_off_candidate_since", None)
        rt["fast_off_confirmation_active"] = False
        return desired, False

    if not applies or not statistical or current_value < .5 or desired_value >= .5:
        rt.pop("fast_off_candidate_since", None)
        rt["fast_off_confirmation_active"] = False
        return desired_value, False

    required = FAST_OFF_CONFIRMATION_SECONDS if confirmation_seconds is None else max(
        0.0, float(confirmation_seconds)
    )
    if required <= 0.0:
        rt.pop("fast_off_candidate_since", None)
        rt["fast_off_confirmation_active"] = False
        return desired_value, False

    now = float(timestamp)
    since = rt.get("fast_off_candidate_since")
    try:
        since = float(since)
    except (TypeError, ValueError):
        since = now
        rt["fast_off_candidate_since"] = since

    elapsed = max(0.0, now - since)
    if elapsed + 1e-9 < required:
        rt["fast_off_confirmation_active"] = True
        rt["fast_off_confirmation_elapsed"] = elapsed
        rt["fast_off_confirmation_required"] = required
        return current_value, True

    rt.pop("fast_off_candidate_since", None)
    rt["fast_off_confirmation_active"] = False
    rt["fast_off_confirmation_elapsed"] = elapsed
    rt["fast_off_confirmation_required"] = required
    return desired_value, False


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
    """Normalize fast timing without weakening explicit user priority.

    Older fast-runtime releases cleared manual holds while migrating lights/switches.
    Manual priority is a safety/intent contract independent from reaction latency, so a
    valid explicit_user_v8 hold must survive startup and timing normalization unchanged.
    """
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
        # Fast targets need short action/settling timing, not weaker user priority.
        # Delegate to the authoritative Engine contract so runtime + durable
        # explicit_user_v8 evidence are preserved exactly like every other target.
        result = original_manual_hold(agent, rt, timestamp)
        if is_fast_target(agent):
            rt["manual_feedback_ts"] = float(timestamp)
        return result

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
