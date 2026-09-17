"""Regression tests for the stage-06 single-wrapper composition.

These tests intentionally inspect the public compatibility shim rather than depending on
implementation details of Engine.  The shim may enable Candidate-only semantics, but it
must not define another process_agent wrapper of its own.
"""
import inspect
import unittest

import manual_feedback_live_isolation


class ManualFeedbackCompositionTests(unittest.TestCase):
    def test_live_isolation_is_declarative_shim_not_second_process_wrapper(self):
        source = inspect.getsource(manual_feedback_live_isolation)
        self.assertIn("manual_feedback_candidate_only", source)
        self.assertIn("install_runtime_physical_equivalence", source)
        self.assertNotIn("def process_agent", source)
        self.assertNotIn("engine.process_agent =", source)


if __name__ == "__main__":
    unittest.main()
