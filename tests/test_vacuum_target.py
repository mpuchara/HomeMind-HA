import unittest

from support import agent, state

import context
import control
import experiments
import settings
from device_targets import VACUUM_RETURN_HOME, VACUUM_START, VACUUM_STOP, install


# The production entrypoint installs extensions before engine/history/executor imports.
install()

STATE_FEATURE = 4096


def vacuum(value="docked", features=STATE_FEATURE | VACUUM_START | VACUUM_RETURN_HOME):
    return state("vacuum.ryszard", value, friendly_name="Ryszard", supported_features=features)


class VacuumTargetTests(unittest.TestCase):
    def test_vacuum_is_a_first_class_binary_target(self):
        opts = context.target_options_for_state(vacuum())
        cleaning = next(x for x in opts if x["property"] == "power")
        self.assertEqual(cleaning["label"], "Cleaning (start / dock)")
        self.assertEqual(cleaning["min"], 0)
        self.assertEqual(cleaning["max"], 1)
        self.assertTrue(any(x["property"] == "power" for x in settings.SUPPORTED_TARGETS["vacuum"]))

    def test_vacuum_activity_maps_to_cleaning_state_without_learning_errors(self):
        self.assertEqual(context.target_value(vacuum("cleaning"), "power"), 1.0)
        self.assertEqual(context.target_value(vacuum("docked"), "power"), 0.0)
        self.assertEqual(context.target_value(vacuum("returning"), "power"), 0.0)
        self.assertEqual(context.target_value(vacuum("paused"), "power"), 0.0)
        self.assertIsNone(context.target_value(vacuum("error"), "power"))
        self.assertIsNone(context.target_value(vacuum("unavailable"), "power"))

    def test_vacuum_control_prefers_start_and_return_to_dock(self):
        st = vacuum("docked")
        self.assertEqual(context.target_call("vacuum.ryszard", "power", 1, st), (
            "vacuum", "start", {"entity_id": "vacuum.ryszard"}
        ))
        self.assertEqual(context.target_call("vacuum.ryszard", "power", 0, st), (
            "vacuum", "return_to_base", {"entity_id": "vacuum.ryszard"}
        ))

    def test_vacuum_falls_back_to_stop_when_return_home_is_unavailable(self):
        st = vacuum("cleaning", STATE_FEATURE | VACUUM_START | VACUUM_STOP)
        self.assertEqual(context.target_call("vacuum.ryszard", "power", 0, st), (
            "vacuum", "stop", {"entity_id": "vacuum.ryszard"}
        ))

    def test_read_only_vacuum_is_not_offered_as_a_target(self):
        self.assertEqual(context.target_options_for_state(vacuum(features=STATE_FEATURE)), [])

    def test_vacuum_has_slow_command_timing_and_long_manual_hold(self):
        a = agent(target_entity="vacuum.ryszard", target_property="power")
        self.assertEqual(context.default_action_interval(a["target_entity"], a["target_property"]), 60.0)
        self.assertEqual(context.historical_acceptance_seconds(a), 1800)
        timing = control.timing_for(a)
        self.assertEqual(timing.acknowledgement, 30)
        self.assertEqual(timing.settling, 60)
        self.assertEqual(timing.manual_hold, 1800)

    def test_physical_context_experiments_cannot_start_a_mobile_vacuum(self):
        runner = experiments.Experiments(object())
        with self.assertRaisesRegex(ValueError, "disabled for mobile vacuum"):
            runner.configure(agent(target_entity="vacuum.ryszard", target_property="power"), {"enabled": True})


if __name__ == "__main__":
    unittest.main()
