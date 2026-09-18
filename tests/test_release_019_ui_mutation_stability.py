"""0.14.19 regression: confidence diagnostics must not self-trigger DOM mutation loops."""
from pathlib import Path
import unittest

from support import ROOT


class Release019UiMutationStabilityTests(unittest.TestCase):
    def source(self):
        return (ROOT / "adaptive_ai/src/static/confidence_contract_ui.js").read_text(encoding="utf-8")

    def test_confidence_decorator_never_observes_the_entire_body_subtree(self):
        source = self.source()
        self.assertNotIn("const root=document.body", source)
        self.assertNotIn("observe(root,{childList:true,subtree:true})", source)
        self.assertIn("document.getElementById('agents')", source)
        self.assertIn("observe(root,{childList:true})", source)

    def test_confidence_metric_text_updates_are_idempotent(self):
        source = self.source()
        self.assertIn("if(node.textContent!==text)node.textContent=text", source)
        self.assertIn("setText(node.querySelector('span'),label)", source)
        self.assertIn("setText(node.querySelector('b'),value)", source)
        self.assertIn("setText(small,title)", source)

    def test_card_observer_coalesces_decorations(self):
        source = self.source()
        self.assertIn("let queued=false", source)
        self.assertIn("if(queued)return", source)
        self.assertIn("queueMicrotask(()=>{queued=false;decorateAll();})", source)


if __name__ == "__main__":
    unittest.main()
