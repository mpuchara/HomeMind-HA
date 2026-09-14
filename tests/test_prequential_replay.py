import unittest

from support import *
from replay import DeferredUpdates


class FakeHead:
    def __init__(self, policy):
        self.policy = policy
        self.validation_seen_updates = []

    def validate(self, action_idx, features, reward, sample_ts=None):
        # Validation must see only evidence from strictly earlier events.
        self.validation_seen_updates.append(self.policy.update_count)


class FakePolicy:
    def __init__(self):
        self.agent = {'id': 'agent-1'}
        self.update_count = 0
        self.update_order = []
        self.heads = {1: FakeHead(self)}

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        self.update_count += 1
        self.update_order.append((horizon, action_idx, dict(features), reward, sample_ts))


class PrequentialReplayTests(unittest.TestCase):
    def test_each_future_event_is_scored_before_it_is_learned(self):
        policy = FakePolicy()
        heldout = DeferredUpdates({'agent-1': policy})

        # This mirrors history.py: validate first, then append the same outcome as
        # training evidence. Event 1 must see zero updates; event 2 may see event 1.
        policy.heads[1].validate(1, {0: 1.0}, 1.0, 100.0)
        heldout.append((policy, 1, 1, {0: 1.0}, 1.0, 100.0))
        policy.heads[1].validate(0, {0: 1.0}, 1.0, 101.0)
        heldout.append((policy, 1, 0, {0: 1.0}, 1.0, 101.0))

        self.assertEqual(policy.heads[1].validation_seen_updates, [0, 1])
        self.assertEqual(policy.update_count, 2)
        self.assertEqual(len(heldout), 2)

        # The legacy end-of-pass fold loop in history.py must not learn these samples a
        # second time because they have already been incorporated prequentially.
        self.assertEqual(list(heldout), [])
        self.assertEqual(policy.update_count, 2)

    def test_training_only_future_sample_is_available_to_the_next_event(self):
        policy = FakePolicy()
        heldout = DeferredUpdates({'agent-1': policy})

        # Persistence/upstream samples in the validation period are training-only. They
        # should still enter immediately so the next chronological event uses them.
        heldout.append((policy, 1, 1, {3: 0.5}, 0.35, 200.0))
        policy.heads[1].validate(1, {3: 0.5}, 1.0, 201.0)

        self.assertEqual(policy.heads[1].validation_seen_updates, [1])
        self.assertEqual(policy.update_count, 1)


if __name__ == '__main__':
    unittest.main()
