"""Execute startup coordination with real polling and final workflow scripts."""
import re
import shutil
import subprocess
import unittest

from support import ROOT


class UiStartup159Tests(unittest.TestCase):
    def run_node(self, scenario):
        if not shutil.which('node'):
            self.skipTest('Node required')
        result = subprocess.run(['node', 'tests/ui_startup_harness.js', scenario],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_loading_document_waits_and_starts_each_callback_once(self):
        self.run_node('loading')

    def test_interactive_document_waits_for_remaining_script_layers(self):
        self.run_node('interactive')

    def test_complete_document_starts_without_missing_readiness_event(self):
        self.run_node('complete')

    def test_load_fallback_and_late_registration_do_not_restart_pollers(self):
        self.run_node('fallback')

    def test_fast_backend_cannot_render_before_final_controls_install(self):
        self.run_node('fast-status')

    def test_live_bootstrap_still_renders_while_status_waits(self):
        self.run_node('slow-status')

    def test_hidden_ingress_still_hydrates_candidates_after_ui_is_ready(self):
        self.run_node('hidden')

    def test_bootstrap_precedes_all_layers_in_classic_script_bundle(self):
        text = (ROOT / 'adaptive_ai/src/static/index.html').read_text(encoding='utf-8')
        # Ignore the historical compatibility marker inside an HTML comment.
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        tags = re.findall(r'<script\b([^>]+)>', text)
        self.assertIn('ui_bootstrap.js?', tags[0])
        for tag in tags:
            self.assertNotRegex(tag, r'\b(?:async|defer|type)\b')

