from support import agent, state

import context
import control
import settings
from device_targets import (
    VACUUM_RETURN_HOME,
    VACUUM_START,
    VACUUM_STATE if False else VACUUM_START,  # compatibility sentinel; not used
    VACUUM_STOP,
    install,
)


# The production entrypoint installs extensions before engine/history/executor imports.
install()

STATE_FEATURE = 4096


def vacuum(value="docked", features=STATE_FEATURE | VACUUM_START | VACUUM_RETURN_HOME):
    return state("vacuum.ryszard", value, friendly_name="Ryszard", supported_features=features)


def test_vacuum_is_a_first_class_binary_target():
    opts = context.target_options_for_state(vacuum())
    cleaning = next(x for x in opts if x["property"] == "power")
    assert cleaning["label"] == "Cleaning (start / dock)"
    assert cleaning["min"] == 0
    assert cleaning["max"] == 1
    assert any(x["property"] == "power" for x in settings.SUPPORTED_TARGETS["vacuum"])


def test_vacuum_activity_maps_to_cleaning_state_without_learning_errors():
    assert context.target_value(vacuum("cleaning"), "power") == 1.0
    assert context.target_value(vacuum("docked"), "power") == 0.0
    assert context.target_value(vacuum("returning"), "power") == 0.0
    assert context.target_value(vacuum("paused"), "power") == 0.0
    assert context.target_value(vacuum("error"), "power") is None
    assert context.target_value(vacuum("unavailable"), "power") is None


def test_vacuum_control_prefers_start_and_return_to_dock():
    st = vacuum("docked")
    assert context.target_call("vacuum.ryszard", "power", 1, st) == (
        "vacuum", "start", {"entity_id": "vacuum.ryszard"}
    )
    assert context.target_call("vacuum.ryszard", "power", 0, st) == (
        "vacuum", "return_to_base", {"entity_id": "vacuum.ryszard"}
    )


def test_vacuum_falls_back_to_stop_when_return_home_is_unavailable():
    st = vacuum("cleaning", STATE_FEATURE | VACUUM_START | VACUUM_STOP)
    assert context.target_call("vacuum.ryszard", "power", 0, st) == (
        "vacuum", "stop", {"entity_id": "vacuum.ryszard"}
    )


def test_read_only_vacuum_is_not_offered_as_a_target():
    assert context.target_options_for_state(vacuum(features=STATE_FEATURE)) == []


def test_vacuum_has_slow_command_timing_and_long_manual_hold():
    a = agent(target_entity="vacuum.ryszard", target_property="power")
    assert context.default_action_interval(a["target_entity"], a["target_property"]) == 60.0
    assert context.historical_acceptance_seconds(a) == 1800
    timing = control.timing_for(a)
    assert timing.acknowledgement == 30
    assert timing.settling == 60
    assert timing.manual_hold == 1800
