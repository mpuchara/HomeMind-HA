import unittest
from pathlib import Path

class ManualFeedbackUiContractTests(unittest.TestCase):
    def test_teaching_and_undo_never_use_physical_correction_endpoint(self):
        js = (Path(__file__).resolve().parents[1] / 'adaptive_ai/src/static/manual_feedback.js').read_text(encoding='utf-8')
        self.assertIn('/teaching`', js)
        self.assertIn('/undo-teaching`', js)
        self.assertNotIn('/manual-correction', js)
        self.assertNotIn('/teach-desired', js)
