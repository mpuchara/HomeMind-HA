from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManualFeedbackButtonContract(unittest.TestCase):
    def test_manual_learning_reuses_verify_slot_and_is_always_enabled(self):
        text = (ROOT / "adaptive_ai/src/static/manual_feedback.js").read_text(encoding="utf-8")
        self.assertIn("onclick.includes('verifyControl(')", text)
        self.assertIn("b.disabled=false", text)
        self.assertIn("b.hidden=false", text)
        self.assertIn("👎 Naucz / popraw", text)
        self.assertIn("manual-correction-hint", text)

    def test_release_version_is_consistent(self):
        self.assertIn('version: "0.10.7"', (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8"))
        self.assertIn('APP_VERSION = "0.10.7"', (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8"))
        self.assertIn('ARG BUILD_VERSION=0.10.7', (ROOT / "adaptive_ai/Dockerfile").read_text(encoding="utf-8"))
        info = json.loads((ROOT / "adaptive_ai/BUILD_INFO.json").read_text(encoding="utf-8"))
        self.assertEqual("0.10.7", info["version"])
        html = (ROOT / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        self.assertIn("0.10.7", html)
        self.assertIn("manual_feedback.js?v=0.10.7", html)


if __name__ == "__main__":
    unittest.main()
