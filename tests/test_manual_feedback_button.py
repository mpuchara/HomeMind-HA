from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManualFeedbackButtonContract(unittest.TestCase):
    def test_card_has_exactly_four_primary_actions(self):
        import re
        text = (ROOT / "adaptive_ai/src/static/p0.js").read_text(encoding="utf-8")
        template = text.split("const create = a => {", 1)[1].split("`;", 1)[0]
        actions = re.findall(r'<button[^>]+data-a="([^"]+)"', template)
        self.assertEqual(actions, ["mode", "wrong", "settings", "teach"])
        self.assertNotIn('<details', template)

    def test_teaching_does_not_observe_its_own_dom_changes(self):
        text = (ROOT / "adaptive_ai/src/static/manual_feedback.js").read_text(encoding="utf-8")
        self.assertNotIn("new MutationObserver", text)
        self.assertNotIn("installButtons", text)

    def test_release_version_is_consistent(self):
        info = json.loads((ROOT / "adaptive_ai/BUILD_INFO.json").read_text(encoding="utf-8"))
        version = info["version"]
        self.assertIn(f'version: "{version}"', (ROOT / "adaptive_ai/config.yaml").read_text(encoding="utf-8"))
        self.assertIn(f'APP_VERSION = "{version}"', (ROOT / "adaptive_ai/src/settings.py").read_text(encoding="utf-8"))
        self.assertIn(f'ARG BUILD_VERSION={version}', (ROOT / "adaptive_ai/Dockerfile").read_text(encoding="utf-8"))
        html = (ROOT / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        self.assertIn(f'LOCAL HOME INTELLIGENCE · {version}', html)
        for script in (ROOT / 'adaptive_ai/src/static').glob('*.js'):
            self.assertIn(f'{script.name}?v={version}', html)


if __name__ == "__main__":
    unittest.main()
