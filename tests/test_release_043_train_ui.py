"""0.14.43 regressions for manual Train admission and layered agent-card renderers."""
import unittest

from support import ROOT


class Release043TrainUiTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / "static" / name).read_text(encoding="utf-8")

    def test_p0_renderer_tolerates_generation_workflow_replacing_action_row(self):
        source = self.source("p0.js")
        block = source.split("const toggle=el.querySelector('[data-a=mode]');", 1)[1].split(
            "if(r.teaching_id)", 1
        )[0]
        self.assertIn("if(toggle){", block)
        self.assertIn("toggle.textContent=", block)
        self.assertIn("toggle.setAttribute(", block)
        self.assertNotIn("\n    toggle.textContent=", block)

    def test_train_http_success_is_not_relabelled_as_failure_by_refresh_exception(self):
        source = self.source("app.js")
        block = source.split("async function trainAgent(id){", 1)[1].split(
            "async function resumeLearning", 1
        )[0]
        # Admission has its own failure boundary.
        self.assertIn("await api(\`api/agents/\${id}/train\`", block)
        self.assertIn("alert('Train failed: '+e.message)", block)
        # Post-admission rendering is best-effort and cannot fall into Train failed.
        self.assertIn("Training was accepted; UI refresh will retry automatically", block)
        self.assertGreater(block.index("alert('Train failed: '+e.message)"), block.index("await api("))
        self.assertGreater(block.index("try{await load();}catch(e){"), block.index("return;"))

    def test_generation_workflow_is_the_layer_that_replaces_p0_actions(self):
        source = self.source("agent_workflow_ui.js")
        self.assertIn('data-wf="train"', source)
        self.assertIn("actions.innerHTML=", source)
        self.assertIn("window.trainAgent?.(a.id)", source)


if __name__ == "__main__":
    unittest.main()
