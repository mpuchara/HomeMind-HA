import unittest
from pathlib import Path


class ManualFeedbackUiContractTests(unittest.TestCase):
    def test_ui_reports_wrong_state_and_requests_real_correction(self):
        js = (Path(__file__).resolve().parents[1] / 'adaptive_ai/src/static/manual_feedback.js').read_text(encoding='utf-8')
        self.assertIn('Agent zrobił źle', js)
        self.assertNotIn('Obecny stan jest poprawny', js)
        self.assertNotIn('keep_current:true', js)
        # For binary power targets the UI sends no desired value. The backend therefore
        # reads the live state under the target lock and toggles it immediately.
        self.assertIn("agent.target_property==='power'?{}:{desired_value:desired}", js)
        self.assertIn("api/agents/${encodeURIComponent(agentId)}/manual-correction", js)


if __name__ == '__main__':
    unittest.main()
