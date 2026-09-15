import shutil
import subprocess
import unittest
from pathlib import Path

from support import ROOT


class SimpleAgentNamesTests(unittest.TestCase):
    def test_ui_contract_removes_redundant_status_from_agent_heading(self):
        text = (ROOT / "adaptive_ai/src/static/simple_agent_names.js").read_text(encoding="utf-8")
        self.assertIn("badge.hidden = true", text)
        self.assertIn("training.remove()", text)
        self.assertIn("window.updateAgentLive", text)
        self.assertIn("window.renderAgents", text)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_only_generated_auto_lifecycle_suffix_is_removed(self):
        script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const c={window:{},lastAgents:[],document:{querySelectorAll:()=>[]}};c.window=c;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/simple_agent_names.js','utf8'),c);
const clean=c.simpleAgentDisplayName;
assert.equal(clean('Lampy kuchnia AUTO - QUALIFIED'),'Lampy kuchnia');
assert.equal(clean('Lampka AUTO · PAUSED'),'Lampka');
assert.equal(clean('Ryszard AUTO - WAITING'),'Ryszard');
assert.equal(clean('shellyplus1pm AUTO · NEEDS_RETRAIN'),'shellyplus1pm');
assert.equal(clean('AUTO Salon'),'AUTO Salon');
assert.equal(clean('Pompa QUALIFIED'),'Pompa QUALIFIED');
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_name_simplifier_is_loaded_after_other_agent_renderers(self):
        html = (ROOT / "adaptive_ai/src/static/index.html").read_text(encoding="utf-8")
        self.assertIn('simple_agent_names.js?v=0.13.1', html)
        self.assertGreater(html.index('simple_agent_names.js'), html.index('candidate_ui.js'))
        self.assertGreater(html.index('simple_agent_names.js'), html.index('automation_baseline_ui.js'))


if __name__ == "__main__":
    unittest.main()
