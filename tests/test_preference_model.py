import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from manual_feedback_unified import UnifiedManualFeedbackJournal
from preference_model import LightingPreferenceModel, PreferenceDecisionComposer
from storage import Store


class LightingPreferenceModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "preference.db"
        self.store = Store(self.path)
        self.now = 1000.0
        self.journal = UnifiedManualFeedbackJournal(self.store, clock=lambda: self.now)
        self.model = LightingPreferenceModel(self.store)
        self.agent = {
            "id": "light-agent", "target_entity": "light.kitchen",
            "target_property": "power", "deadband": .5,
        }
        self.context = {
            "binary_sensor.motion / state": 1.0,
            "home:p_arrival_30": 0.20,
            "home:p_arrival_120": 0.30,
            "home:occupancy_now": 0.0,
            "meta:home_known": 1.0,
            "meta:feature_schema_version": 12.0,
            "meta:policy_version": 10.0,
            "meta:signature_contract": 2.0,
        }

    def tearDown(self):
        self.temp.cleanup()

    def record(self, correct, *, rejected=None, scope="similar_context", signature=None,
               feedback_id=None, source="correct"):
        return self.journal.record(
            agent_id=self.agent["id"], selected_ts=self.now - 1,
            source=source, rejected_action=rejected, correct_action=correct,
            error_kind="state", scope=scope, fingerprint="fp",
            context_signature=dict(signature or self.context), deadband=.5,
            feedback_id=feedback_id,
        )

    def evaluate(self, signature=None, episode_id=None):
        return self.model.evaluate(
            self.agent, [0.0, 1.0], dict(signature or self.context), episode_id=episode_id
        )

    def test_new_explicit_preference_overrides_bootstrap_without_counting_twice(self):
        row = self.record(1.0, rejected=0.0)
        result = self.evaluate()
        self.assertTrue(result["applied"])
        self.assertEqual(result["action_value"], 1.0)
        self.assertEqual(result["source"], "preference_model")
        self.assertEqual(result["independent_evidence_count"], 1)
        self.assertEqual(result["calibration_evidence_count"], 1)
        self.assertEqual(result["optimization_terms"], 2)
        self.assertEqual(result["evidence_ids"], [row["feedback_id"]])
        self.assertEqual(result["weights"]["historical_demonstration"], 0.0)
        self.assertEqual(result["weights"]["episode_outcome"], 0.0)
        self.assertEqual(result["weights"]["absence_of_feedback"], 0.0)

        # Reusing one label at inference/optimization time never creates new evidence.
        for _ in range(5):
            repeated = self.evaluate()
            self.assertEqual(repeated["calibration_evidence_count"], 1)
            self.assertEqual(repeated["evidence_ids"], [row["feedback_id"]])

    def test_negative_rating_does_not_invent_the_other_action(self):
        self.record(None, rejected=0.0, source="negative_rating")
        result = self.evaluate()
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "negative_rating_without_positive_action_label")
        self.assertEqual(result["independent_evidence_count"], 1)

    def test_one_time_exception_does_not_generalize_over_persistent_preference(self):
        persistent = self.record(0.0, rejected=1.0, scope="persistent_preference")
        exception = self.record(1.0, rejected=0.0, scope="one_time")
        result = self.evaluate()
        self.assertTrue(result["applied"])
        self.assertEqual(result["action_value"], 0.0)
        self.assertEqual(result["evidence_ids"], [persistent["feedback_id"]])
        self.assertNotIn(exception["feedback_id"], result["evidence_ids"])

    def test_conflicting_same_context_is_not_trainable_preference(self):
        first = self.record(0.0, rejected=1.0)
        second = self.record(1.0, rejected=0.0)
        self.assertEqual(self.journal.get(first["feedback_id"])["application_status"], "conflict")
        self.assertEqual(self.journal.get(second["feedback_id"])["application_status"], "conflict")
        result = self.evaluate()
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "no_matching_explicit_preference")
        self.assertEqual(result["independent_evidence_count"], 0)

    def test_no_feedback_is_not_preference_evidence(self):
        result = self.evaluate()
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "no_matching_explicit_preference")
        self.assertEqual(result["independent_evidence_count"], 0)

    def test_undo_and_restart_remove_complete_preference_influence(self):
        row = self.record(1.0, rejected=0.0)
        self.assertTrue(self.evaluate()["applied"])
        self.journal.undo(row["feedback_id"])
        self.assertFalse(self.evaluate()["applied"])

        restarted_store = Store(self.path)
        restarted = LightingPreferenceModel(restarted_store)
        after_restart = restarted.evaluate(self.agent, [0.0, 1.0], dict(self.context))
        self.assertFalse(after_restart["applied"])
        self.assertEqual(after_restart["independent_evidence_count"], 0)

    def test_held_out_adaptation_needs_one_correction_and_does_not_regress_untouched_context(self):
        # Bootstrap imitates the old automation and says OFF in both held-out contexts.
        adapted = dict(self.context)
        adapted["home:p_arrival_30"] = 0.23
        untouched = dict(self.context)
        untouched["binary_sensor.motion / state"] = -1.0
        desired = {"adapted": 1.0, "untouched": 0.0}
        bootstrap = {"adapted": 0.0, "untouched": 0.0}
        bootstrap_errors = sum(bootstrap[key] != desired[key] for key in desired)

        # An old exact-context correction does not by itself predict the slightly shifted
        # held-out context.  The semantic preference baseline is allowed to generalize only
        # through the same versioned Teaching distance contract.
        old_exact_correction = {"adapted": bootstrap["adapted"], "untouched": bootstrap["untouched"]}
        old_errors = sum(old_exact_correction[key] != desired[key] for key in desired)

        self.record(1.0, rejected=0.0, signature=self.context)
        adapted_pref = self.model.evaluate(self.agent, [0.0, 1.0], adapted)
        untouched_pref = self.model.evaluate(self.agent, [0.0, 1.0], untouched)
        new_predictions = {
            "adapted": adapted_pref["action_value"] if adapted_pref["applied"] else bootstrap["adapted"],
            "untouched": untouched_pref["action_value"] if untouched_pref["applied"] else bootstrap["untouched"],
        }
        new_errors = sum(new_predictions[key] != desired[key] for key in desired)
        regression = int(new_predictions["untouched"] != bootstrap["untouched"])

        self.assertEqual(bootstrap_errors, 1)
        self.assertEqual(old_errors, 1)
        self.assertEqual(new_errors, 0)
        self.assertEqual(regression, 0)
        self.assertEqual(adapted_pref["independent_evidence_count"], 1)
        self.assertFalse(untouched_pref["applied"])

    def test_instruction_scope_one_time_expires_using_existing_intent_ttl(self):
        row = self.record(1.0, rejected=0.0, scope="one_time")
        self.journal.link(row["feedback_id"], "learning", "teaching_label", 42)
        immediate = self.model.instruction_state(42, self.now)
        later = self.model.instruction_state(42, self.now + 60)
        self.assertTrue(immediate["active"])
        self.assertEqual(immediate["scope"], "one_time")
        self.assertFalse(later["active"])


class FakeHead:
    def structural_confidence(self, arms, index):
        return .8

    def calibration(self, index):
        return {"accuracy": .75, "ceiling": .7, "samples": 20}


class PreferenceDecisionComposerTests(unittest.TestCase):
    def setUp(self):
        self.teaching = SimpleNamespace(match=Mock(return_value=None))
        self.experiments = SimpleNamespace(propose=Mock(return_value=None))
        self.engine = SimpleNamespace(teaching=self.teaching, experiments=self.experiments)
        self.preference = Mock()
        self.preference.instruction_state.return_value = {"active": True, "scope": "persistent_preference"}
        self.preference.predict.return_value = {"applied": False, "reason": "none"}
        self.composer = PreferenceDecisionComposer(self.engine, self.preference)
        self.policy = SimpleNamespace(actions=[0.0, 1.0], heads={1: FakeHead()})
        self.chosen = {
            "index": 0, "value": 0.0, "mean": .2, "uncertainty": .1,
            "support": .8, "novelty": .1,
        }
        self.arms = [
            dict(self.chosen),
            {"index": 1, "value": 1.0, "mean": .1, "uncertainty": .2,
             "support": .7, "novelty": .15},
        ]
        self.agent = {"id": "a", "target_entity": "light.kitchen", "target_property": "power"}

    def compose(self):
        return self.composer.compose(
            agent=self.agent, policy=self.policy, state_map={}, temporal=None,
            timestamp=1000.0, features={0: 1.0}, labels={}, chosen=dict(self.chosen),
            confidence=.75, arms=[dict(x) for x in self.arms], horizon=1,
            support=.8, novelty=.1, runtime={}, registry={},
        )

    def test_scoped_instruction_precedes_preference_and_experiment(self):
        self.teaching.match.return_value = {"id": 7, "desired": 1.0}
        result = self.compose()
        self.assertEqual(result["source"], "scoped_instruction:persistent_preference")
        self.assertEqual(result["chosen"]["value"], 1.0)
        self.preference.predict.assert_not_called()
        self.experiments.propose.assert_not_called()

    def test_preference_precedes_experiment_but_keeps_policy_safety_statistics(self):
        self.preference.predict.return_value = {
            "applied": True, "action_index": 1, "action_value": 1.0,
            "independent_evidence_count": 1,
        }
        result = self.compose()
        self.assertEqual(result["source"], "preference_model")
        self.assertEqual(result["chosen"]["value"], 1.0)
        self.assertEqual(result["support"], .7)
        self.assertLessEqual(result["confidence"], .7)
        self.experiments.propose.assert_not_called()

    def test_no_instruction_or_preference_falls_through_to_existing_experiment(self):
        self.experiments.propose.return_value = {
            "value": 1.0, "index": 1, "support": .6, "novelty": .2,
            "focus": "presence", "token": "trial", "snapshot": {},
        }
        result = self.compose()
        self.assertEqual(result["source"], "experiment")
        self.assertEqual(result["chosen"]["value"], 1.0)


if __name__ == "__main__":
    unittest.main()
