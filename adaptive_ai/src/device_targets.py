"""Optional target adapters installed before the Adaptive AI runtime imports core modules.

The core policy engine is deliberately generic, but Home Assistant domains do not all
share the same state/service semantics.  This module is the extension point for domains
that need a small translation layer without teaching the RL code vendor-specific rules.

The first adapter is ``vacuum``.  It exposes one binary policy:

    1 = cleaning (vacuum.start)
    0 = not cleaning (prefer vacuum.return_to_base)

A STOP or legacy turn_off service is used only when RETURN_HOME is not advertised.
The adapter is capability-aware and does not expose a controllable target when the
entity cannot both start and end a cleaning run.
"""

RELEASE_VERSION = "0.10.2"
_INSTALLED = False

# Home Assistant VacuumEntityFeature values.  Kept local so the add-on does not import
# Home Assistant Python internals; the Supervisor API only gives us the bit field.
VACUUM_TURN_ON = 1
VACUUM_TURN_OFF = 2
VACUUM_PAUSE = 4
VACUUM_STOP = 8
VACUUM_RETURN_HOME = 16
VACUUM_START = 8192


def _supported_features(state):
    try:
        return int(((state or {}).get("attributes") or {}).get("supported_features") or 0)
    except (TypeError, ValueError):
        return 0


def _vacuum_value(state):
    """Map HA vacuum activity to the binary cleaning policy.

    Fault/unknown states are not converted to OFF because they are not evidence of a
    user preference and should not become historical RL labels.
    """
    if not state:
        return None
    activity = str(state.get("state") or "").strip().lower()
    if activity in ("unknown", "unavailable", "error", ""):
        return None
    if activity in ("cleaning", "on"):
        return 1.0
    if activity in ("docked", "idle", "paused", "returning", "off"):
        return 0.0
    return None


def _vacuum_call(entity_id, value, state=None):
    features = _supported_features(state)
    starting = float(value) >= 0.5

    if starting:
        if features == 0 or features & VACUUM_START:
            return "vacuum", "start", {"entity_id": entity_id}
        if features & VACUUM_TURN_ON:
            return "vacuum", "turn_on", {"entity_id": entity_id}
        raise ValueError(f"{entity_id} does not advertise a start-cleaning action")

    # A completed/aborted autonomous cleaning policy should leave the robot in a useful
    # stable state.  Prefer the dock rather than STOP, which can strand a robot mid-room.
    if features == 0 or features & VACUUM_RETURN_HOME:
        return "vacuum", "return_to_base", {"entity_id": entity_id}
    if features & VACUUM_STOP:
        return "vacuum", "stop", {"entity_id": entity_id}
    if features & VACUUM_TURN_OFF:
        return "vacuum", "turn_off", {"entity_id": entity_id}
    raise ValueError(f"{entity_id} does not advertise return-home, stop or turn-off")


def install():
    """Install target-domain extensions before queue/runtime modules import symbols.

    Returns a short diagnostics dictionary and is safe to call repeatedly.
    """
    global _INSTALLED
    if _INSTALLED:
        return {"installed": False, "vacuum": True, "reason": "already installed"}

    import settings
    import context
    import control

    # Keep the runtime/API version aligned with the add-on package without forcing a
    # feature-schema rebuild: this adapter changes target-domain semantics, not models.
    settings.APP_VERSION = RELEASE_VERSION

    settings.SUPPORTED_TARGETS.setdefault("vacuum", [])
    if not any(x.get("property") == "power" for x in settings.SUPPORTED_TARGETS["vacuum"]):
        settings.SUPPORTED_TARGETS["vacuum"].append({
            "property": "power",
            "label": "Cleaning (start / dock)",
            "min": 0,
            "max": 1,
            "deadband": 0.5,
            "exploration_step": 1,
        })
    settings.SENSOR_NEEDS.setdefault("vacuum", [
        ("occupancy", "Home occupancy / presence",
         "Helps the agent learn when cleaning is acceptable instead of running while people are using the home."),
    ])

    base_target_value = context.target_value
    base_target_call = context.target_call
    base_target_options = context.target_options_for_state
    base_default_action_interval = context.default_action_interval
    base_historical_acceptance = context.historical_acceptance_seconds
    base_timing_for = control.timing_for

    def target_value(state, property_name):
        domain = str((state or {}).get("entity_id") or "").split(".", 1)[0]
        if domain == "vacuum" and property_name == "power":
            return _vacuum_value(state)
        return base_target_value(state, property_name)

    def target_call(entity_id, property_name, value, state=None):
        domain = str(entity_id or "").split(".", 1)[0]
        if domain == "vacuum" and property_name == "power":
            return _vacuum_call(entity_id, value, state)
        return base_target_call(entity_id, property_name, value, state)

    def target_options_for_state(state):
        options = base_target_options(state)
        domain = str((state or {}).get("entity_id") or "").split(".", 1)[0]
        if domain != "vacuum":
            return options
        features = _supported_features(state)
        if features == 0:
            return options
        can_start = bool(features & (VACUUM_START | VACUUM_TURN_ON))
        can_finish = bool(features & (VACUUM_RETURN_HOME | VACUUM_STOP | VACUUM_TURN_OFF))
        if can_start and can_finish:
            return options
        return [x for x in options if x.get("property") != "power"]

    def default_action_interval(entity_id, target_property):
        if str(entity_id or "").split(".", 1)[0] == "vacuum" and target_property == "power":
            return 60.0
        return base_default_action_interval(entity_id, target_property)

    def historical_acceptance_seconds(agent):
        if str(agent.get("target_entity") or "").split(".", 1)[0] == "vacuum":
            return 1800
        return base_historical_acceptance(agent)

    def timing_for(agent):
        if str(agent.get("target_entity") or "").split(".", 1)[0] != "vacuum":
            return base_timing_for(agent)
        defaults = control.Timing(30, 60, 1800)
        return control.Timing(
            float(agent.get("ack_timeout") or defaults.acknowledgement),
            float(agent.get("settling_seconds") or defaults.settling),
            float(agent.get("manual_hold_seconds") or defaults.manual_hold),
        )

    # Replace module globals before engine/history/executor import them with ``from``.
    context.target_value = target_value
    context.target_call = target_call
    context.target_options_for_state = target_options_for_state
    context.default_action_interval = default_action_interval
    context.historical_acceptance_seconds = historical_acceptance_seconds
    control.timing_for = timing_for

    _INSTALLED = True
    return {"installed": True, "vacuum": True, "version": RELEASE_VERSION}
