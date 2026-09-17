"""Sanity contract for stage-06 imports.

The facade preserves the historical module under manual_feedback_legacy and exposes one
runtime physical-equivalence wrapper.  This small test protects the intended composition.
"""
import unittest

import manual_feedback
import manual_feedback_legacy


class ManualFeedbackFacadeTests(unittest.TestCase):
    def test_public_helpers_remain_compatible(self):
        self.assertIs(manual_feedback._manual_value, manual_feedback_legacy._manual_value)
        self.assertIs(manual_feedback.teach_desired, manual_feedback_legacy.teach_desired)
        self.assertIs(manual_feedback.apply_ui_correction, manual_feedback_legacy.apply_ui_correction)


if __name__ == "__main__":
    unittest.main()
