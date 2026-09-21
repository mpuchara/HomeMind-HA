from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ManualFeedbackButtonContract(unittest.TestCase):
    def test_model_ready_card_has_six_generation_workflow_actions(self):
        text = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        templates = re.findall(r'actions\.innerHTML=`([^`]+)`;', text)
        template = next((value for value in templates if 'data-wf="auto"' in value), None)
        self.assertIsNotNone(template, "model-ready generation workflow action template missing")
        actions = re.findall(r'data-wf="([^"]+)"', template)
        self.assertEqual(actions, ["auto", "correct", "explore", "change", "settings", "debug"])
        self.assertIn(">Autonomous<", template)
        self.assertIn(">Correct<", template)
        self.assertIn(">Explore<", template)
        self.assertIn(">Change decision<", template)
        self.assertIn(">Settings<", template)
        self.assertIn(">Export debug<", template)
        self.assertRegex(template, r'data-wf="explore"[^>]*disabled')
        self.assertNotIn(">Teach<", template)
        self.assertNotIn(">Wrong decision<", template)

    def test_runtime_layer_replaces_legacy_primary_actions_after_every_live_render(self):
        text = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")
        self.assertIn("const baseRender=window.renderAgents", text)
        self.assertIn("window.renderAgents=()=>", text)
        self.assertIn("liveActions(card,a)", text)
        # app.js uses a top-level lexical `let lastAgents`; the workflow must read that
        # actual binding rather than a nonexistent window.lastAgents property.
        self.assertIn("Array.isArray(lastAgents)?lastAgents:[]", text)
        self.assertNotIn("window.lastAgents", text)

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
