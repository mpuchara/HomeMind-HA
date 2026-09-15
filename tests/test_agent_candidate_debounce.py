import time
import unittest

import support
from agent_candidate_debounce import DEBOUNCE_SECONDS, install


class FakeManager:
    def __init__(self):
        self.calls = 0
        self._start_build = self.start_build

    def start_build(self, row):
        self.calls += 1
        return True


class CandidateDebounceTests(unittest.TestCase):
    def test_feedback_burst_stays_queued_during_coalescing_window(self):
        manager = install(FakeManager())
        row = {"reason": "teach", "queued_ts": time.time()}
        self.assertFalse(manager._start_build(row))
        self.assertEqual(manager.calls, 0)
        row["queued_ts"] = time.time() - DEBOUNCE_SECONDS - .1
        self.assertTrue(manager._start_build(row))
        self.assertEqual(manager.calls, 1)

    def test_explicit_train_and_rebuild_bypass_coalescing_delay(self):
        manager = install(FakeManager())
        for reason in ("teach_train", "manual_rebuild"):
            self.assertTrue(manager._start_build({"reason": reason, "queued_ts": time.time()}))
        self.assertEqual(manager.calls, 2)


if __name__ == "__main__":
    unittest.main()
