import unittest
from types import SimpleNamespace
import test_executor as fixtures
from historical_teach_reward import install


class HistoricalTeachRewardTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    tearDown = fixtures.ExecutorTests.tearDown

    def test_accepted_taught_action_updates_base_policy(self):
        install(SimpleNamespace(ENGINE=self.e))
        features, _, _ = self.model.features(self.e.state_map, self.e.temporal_history)
        before = self.model.total_updates
        self.e.runtime[self.a["id"]] = {"pending": {
            "teaching_id": 7, "experiment": False,
            "policy_head": min(self.model.horizons), "action_index": 1,
            "action_value": self.model.actions[1], "features": features,
        }}
        rt = self.e.runtime[self.a["id"]]
        self.e._reward_pending(self.a, rt, .15, "accepted")
        self.assertGreater(self.model.total_updates, before)
        self.assertIsNone(rt.get("pending"))
        self.assertEqual(rt.get("last_reward"), .15)


if __name__ == "__main__": unittest.main()
