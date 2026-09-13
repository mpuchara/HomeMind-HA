from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManualFeedbackButtonContract(unittest.TestCase):
    def test_manual_learning_handles_p0_cards_and_hides_verify(self):
        text = (ROOT / "adaptive_ai/src/static/manual_feedback.js").read_text(encoding="utf-8")
        self.assertIn("card?.dataset?.agentId", text)
        self.assertIn("x.dataset.a==='verify'", text)
        self.assertIn("manual-legacy-verify", text)
        self.assertIn("verify.hidden=true", text)
        self.assertIn("👎 Naucz / popraw", text)
        self.assertIn("manual-correction-hint", text)

    def test_manual_feedback_does_not_observe_or_poll_its_own_dom_changes(self):
        text = (ROOT / "adaptive_ai/src/static/manual_feedback.js").read_text(encoding="utf-8")
        self.assertNotIn("MutationObserver", text)
        self.assertNotIn("setInterval(installButtons", text)
        self.assertIn("previousRenderAgents=renderAgents", text)
        self.assertIn("installButtons();", text)

    def test_release_version_is_consistent(self):
        self.assertIn('version: "0.10.8"', (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8"))
        self.assertIn('APP_VERSION = "0.10.8"', (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8"))
        self.assertIn('ARG BUILD_VERSION=0.10.8', (ROOT / "adaptive_ai/Dockerfile").read_text(encoding="utf-8"))
        info = json.loads((ROOT / "adaptive_ai/BUILD_INFO.json").read_text(encoding="utf-8"))
        self.assertEqual("0.10.8", info["version"])
        html = (ROOT / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        self.assertIn("0.10.8 HF1", html)
        self.assertIn("manual_feedback.js?v=0.10.8-hf1", html)


if __name__ == "__main__":
    unittest.main()
