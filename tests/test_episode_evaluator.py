import unittest

from support import *
from episode_evaluator import COUNTERFACTUAL_PROXY, EpisodeEvaluator
from storage import Store


class EpisodeEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "episode.db")
        self.evaluator = EpisodeEvaluator(self.store, clock=lambda: 100.0)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def policy(key, decisions=(), *, executed=False, initial=False, role="candidate"):
        return {
            "policy_key": key,
            "role": role,
            "executed": executed,
            "initial_power": initial,
            "decisions": list(decisions),
        }

    def evaluate(self, observations, policies, *, end=60.0, episode_id="episode", corrections=()):
        return self.evaluator.evaluate_episode(
            episode_id=episode_id,
            agent_id="light-agent",
            start_ts=0.0,
            end_ts=end,
            observations=observations,
            policies=policies,
            manual_corrections=corrections,
        )

    @staticmethod
    def metrics(result, index=0):
        return result["policies"][index]["metrics"]

    def test_stationary_person_is_not_a_sequence_of_tick_successes(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": True, "light_need": True, "power": True},
                {"ts": 60, "presence": True, "light_need": True, "power": True},
            ],
            [self.policy("live", executed=True)],
        )
        metrics = self.metrics(result)
        self.assertEqual(metrics["off_while_needed_seconds"], 0.0)
        self.assertEqual(metrics["needed_on_delay_seconds"], 0.0)
        self.assertTrue(metrics["meaningful"])
        self.assertEqual(len(result["policies"]), 1)

    def test_entry_and_quick_return_counts_retrigger_at_episode_level(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": True, "light_need": True, "power": True},
                {"ts": 5, "presence": False, "light_need": False, "power": False},
                {"ts": 10, "presence": True, "light_need": True, "power": True},
                {"ts": 20, "presence": True, "light_need": True, "power": True},
            ],
            [self.policy("live", executed=True)],
            end=20,
        )
        metrics = self.metrics(result)
        self.assertEqual(metrics["retrigger_count"], 1)
        self.assertGreaterEqual(metrics["chatter_count"], 1)

    def test_no_arrival_is_a_negative_prediction_opportunity(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": False, "light_need": None, "power": False},
                {"ts": 8, "presence": False, "light_need": None, "power": False},
            ],
            [self.policy("shadow", [{"ts": 0, "power": True, "anticipatory": True}])],
            end=8,
        )
        metrics = self.metrics(result)
        self.assertEqual(metrics["false_arrival_prediction"], 1)
        self.assertTrue(metrics["harmful"])
        # Absence is not automatically a light-comfort label.
        self.assertIsNone(metrics["unnecessary_on_seconds"])

    def test_light_needed_without_action_records_dark_time_and_delay(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": True, "light_need": True, "power": False},
                {"ts": 12, "presence": True, "light_need": True, "power": False},
            ],
            [self.policy("live", executed=True)],
            end=12,
        )
        metrics = self.metrics(result)
        self.assertEqual(metrics["needed_on_delay_seconds"], 12.0)
        self.assertEqual(metrics["off_while_needed_seconds"], 12.0)

    def test_day_without_light_need_is_meaningful_even_without_device_change(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": True, "light_need": False, "power": False},
                {"ts": 30, "presence": True, "light_need": False, "power": False},
            ],
            [self.policy("live", executed=True)],
            end=30,
        )
        metrics = self.metrics(result)
        self.assertTrue(metrics["meaningful"])
        self.assertEqual(metrics["unnecessary_on_seconds"], 0.0)

    def test_sensor_failure_stays_unknown_instead_of_becoming_zero(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": None, "light_need": None, "power": False, "observable": False},
                {"ts": 10, "presence": None, "light_need": None, "power": False, "observable": False},
            ],
            [self.policy("live", executed=True)],
            end=10,
        )
        metrics = self.metrics(result)
        self.assertIsNone(metrics["needed_on_delay_seconds"])
        self.assertIsNone(metrics["off_while_needed_seconds"])
        self.assertIsNone(metrics["unnecessary_on_seconds"])
        self.assertFalse(metrics["meaningful"])

    def test_later_correct_off_does_not_erase_earlier_darkness_error(self):
        result = self.evaluate(
            [
                {"ts": 0, "presence": True, "light_need": True, "power": False},
                {"ts": 10, "presence": True, "light_need": True, "power": True},
                {"ts": 30, "presence": False, "light_need": False, "power": False},
                {"ts": 40, "presence": False, "light_need": False, "power": False},
            ],
            [self.policy("live", executed=True)],
            end=40,
        )
        metrics = self.metrics(result)
        self.assertEqual(metrics["off_while_needed_seconds"], 10.0)
        self.assertEqual(metrics["needed_on_delay_seconds"], 10.0)

    def test_shadow_metrics_are_marked_counterfactual_and_policies_share_episode_ids(self):
        observations = [
            {"ts": 0, "presence": True, "light_need": True, "power": False},
            {"ts": 10, "presence": True, "light_need": True, "power": True},
            {"ts": 20, "presence": True, "light_need": True, "power": True},
        ]
        self.evaluate(
            observations,
            [
                self.policy("parent", [{"ts": 8, "power": True}], initial=False),
                self.policy("candidate", [{"ts": 2, "power": True}], initial=False),
            ],
            end=20,
            episode_id="shared",
        )
        comparison = self.evaluator.compare_policies("light-agent", "parent", "candidate")
        self.assertEqual(comparison["episode_ids"], ["shared"])
        self.assertEqual(comparison["matched_episodes"], 1)
        self.assertEqual(comparison["evidence_mode"], "independent_labels")
        self.assertLess(
            comparison["candidate"]["metric_means"]["needed_on_delay_seconds"],
            comparison["parent"]["metric_means"]["needed_on_delay_seconds"],
        )
        rows = self.evaluator._policy_rows("light-agent", "candidate")
        self.assertEqual(
            rows["shared"]["evidence"]["needed_on_delay_seconds"]["source"],
            COUNTERFACTUAL_PROXY,
        )

    def test_automation_replay_is_separate_proxy_not_light_need(self):
        self.evaluator.record_automation_proxy(
            episode_id="proxy",
            agent_id="light-agent",
            start_ts=0,
            end_ts=10,
            baseline_power=False,
            policies=[
                self.policy("parent", initial=False),
                self.policy("candidate", [{"ts": 0, "power": True}], initial=False),
            ],
        )
        comparison = self.evaluator.compare_policies("light-agent", "parent", "candidate")
        self.assertEqual(comparison["evidence_mode"], "automation_replay_proxy")
        self.assertEqual(comparison["independently_observed_episodes"], 0)
        self.assertIsNone(comparison["candidate"]["metric_means"]["off_while_needed_seconds"])

    def test_finalized_episode_is_idempotent_but_not_reinterpretable(self):
        args = dict(
            episode_id="immutable",
            agent_id="light-agent",
            start_ts=0,
            end_ts=1,
            observations=[
                {"ts": 0, "light_need": False, "power": False},
                {"ts": 1, "light_need": False, "power": False},
            ],
            policies=[self.policy("live", executed=True)],
        )
        self.evaluator.evaluate_episode(**args)
        self.evaluator.evaluate_episode(**args)
        with self.assertRaises(ValueError):
            self.evaluator.evaluate_episode(**(args | {"end_ts": 2}))


if __name__ == "__main__":
    unittest.main()
