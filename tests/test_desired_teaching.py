"""Real policy/SQLite feedback; physical dispatch is mocked and must remain unused."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import agent, state
import test_executor as fixtures
import storage
import manual_context_learning as context_learning
from manual_feedback import apply_ui_correction, install, teach_desired


class DesiredTeachingTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    tearDown = fixtures.ExecutorTests.tearDown

    def prepare(self, mode="shadow", current="on", predicted=0.0):
        patch.object(storage, "STORE", self.store).start()
        context_learning._ensure_table(self.store)
        context_learning._SCORE_CACHE.clear()
        context_learning._OBSERVATION_CACHE.clear()
        self.store.update_agent(self.a["id"], {"mode": mode})
        self.a = self.store.get_agent_config(self.a["id"])
        self.e.state_map[self.a["target_entity"]] = state(self.a["target_entity"], current)
        self.e.state_map["binary_sensor.motion"] = state("binary_sensor.motion", "on", device_class="motion")
        self.rt = self.e.runtime.setdefault(self.a["id"], {})
        self.rt["last_prediction"] = predicted
        return SimpleNamespace(ENGINE=self.e, STORE=self.store)

    def test_binary_teaching_toggles_desired_not_opposite_current(self):
        core = self.prepare(current="on", predicted=0.0)
        before = copy.deepcopy(self.e.state_map)
        result = teach_desired(core, self.a)
        self.assertEqual(result["desired_value"], 1.0)
        self.assertEqual(self.rt["last_prediction"], 1.0)
        self.assertTrue(result["negative_applied"])
        self.assertTrue(result["positive_applied"])
        self.assertEqual(self.e.state_map, before)
        self.assertEqual(self.store.get_agent_config(self.a["id"])["mode"], "shadow")
        self.assertNotIn("manual_override_until", self.rt)
        self.service.assert_not_called()
        saved = self.store.get_model(self.a["id"])
        self.assertIsNotNone(saved)
        with self.store.conn() as c:
            row = c.execute("SELECT desired_value,rejected_value,source FROM manual_context_feedback WHERE agent_id=?", (self.a["id"],)).fetchone()
        self.assertEqual(tuple(row), (1.0, 0.0, "ui_teach_desired"))

    def test_control_teaching_does_not_dispatch_or_reward_physical_action(self):
        core = self.prepare(mode="control", current="off", predicted=1.0)
        pending = {"action_value": 1.0, "started_ts": 123}
        self.rt.update(pending=pending, manual_override_until=567, last_service="old")
        with patch.object(self.e, "_reward_pending") as reward, patch.object(self.e, "set_manual_hold") as hold:
            result = teach_desired(core, self.a)
            reward.assert_not_called()
            hold.assert_not_called()
        self.assertEqual(result["desired_value"], 0.0)
        self.assertTrue(result["context_learning"]["schema_refresh_deferred"])
        self.assertIs(self.rt["pending"], pending)
        self.assertEqual(self.rt["manual_override_until"], 567)
        self.assertEqual(self.rt["last_service"], "old")
        self.service.assert_not_called()

    def test_current_correction_still_toggles_physical_current_in_shadow(self):
        core = self.prepare(current="on", predicted=0.0)
        result = apply_ui_correction(core, self.a)
        self.assertEqual(result["desired_value"], 0.0)
        self.service.assert_called_once_with("light", "turn_off", {"entity_id": self.a["target_entity"]})

    def test_missing_prediction_requires_explicit_label(self):
        core = self.prepare(predicted=None)
        with self.assertRaisesRegex(ValueError, "desired_value is required"):
            teach_desired(core, self.a)
        result = teach_desired(core, self.a, 0)
        self.assertTrue(result["positive_applied"])
        self.assertFalse(result["negative_applied"])
        self.service.assert_not_called()

    def test_numeric_label_respects_device_limits_without_dispatch(self):
        core = self.prepare(mode="paused", predicted=2.0)
        self.a = self.store.create_agent(agent(target_entity="number.test", target_property="value", min_value=0, max_value=10, mode="paused", deadband=.1))
        self.e.state_map["number.test"] = state("number.test", "2", min=2, max=8, step=.5)
        result = teach_desired(core, self.a, 3.26)
        self.assertEqual(result["desired_value"], 3.5)
        self.assertEqual(result["current_value"], 2)
        self.service.assert_not_called()

    def test_bad_label_has_no_learning_side_effects(self):
        core = self.prepare()
        revision = self.model.model_revision
        with self.assertRaisesRegex(ValueError, "finite"):
            teach_desired(core, self.a, float("nan"))
        self.assertEqual(self.model.model_revision, revision)
        self.service.assert_not_called()

    def test_training_race_checks_fresh_configuration_before_learning(self):
        core = self.prepare()
        self.store.set_training_state(self.a["id"], "training")
        revision = self.model.model_revision
        with self.assertRaisesRegex(ValueError, "trening historyczny"):
            teach_desired(core, self.a, 1)
        self.assertEqual(self.model.model_revision, revision)
        self.service.assert_not_called()

    def test_http_route_authenticates_and_teaches_without_service(self):
        core = self.prepare()
        class Handler:
            do_POST = Mock()
        core.Handler = Handler
        core.initialize_runtime = lambda: None
        install(core, attach_runtime=False)
        handler = Handler()
        handler.path = "/api/agents/" + self.a["id"] + "/teach-desired"
        handler.require_trusted_client = Mock(return_value=False)
        handler.require_runtime = Mock(return_value=True)
        handler.read_json = Mock(return_value={"desired_value": 1})
        handler.send_json = Mock()
        handler.do_POST()
        handler.read_json.assert_not_called()
        handler.require_trusted_client.return_value = True
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        self.assertEqual(handler.send_json.call_args.args[1]["desired_value"], 1)
        self.service.assert_not_called()
