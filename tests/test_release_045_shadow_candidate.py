"""0.14.45 regressions for Shadow transition UI and persistent Candidate events."""
import inspect
import unittest

from support import ROOT
import agent_candidate_shadow_runtime as shadow_runtime


class Release045ShadowCandidateTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / "static" / name).read_text(encoding="utf-8")

    def test_mode_patch_success_is_not_relabelled_by_refresh_error(self):
        promotion = self.source("promotion_lifecycle_ui.js")
        app = self.source("app.js")
        for source in (promotion, app):
            block = source.split("setMode=async", 1)[1] if "setMode=async" in source else source.split("async function setMode", 1)[1]
            self.assertIn("Mode changed successfully; UI refresh will retry automatically", block)
            self.assertIn("return;", block)
        self.assertNotIn("alert('Nie udało się zmienić trybu: '+e.message);\n      if(typeof window.load", promotion)

    def test_candidate_metric_refresh_repairs_transient_missing_children(self):
        source = self.source("candidate_preference_ui.js")
        block = source.split("const ensureMetric=", 1)[1].split("const decisionValue", 1)[0]
        self.assertIn("if(!root)return", block)
        self.assertIn("if(!labelNode)", block)
        self.assertIn("if(!valueNode)", block)
        self.assertNotIn("node.querySelector('span').textContent", block)

    def test_candidate_shadow_has_passive_event_fallback_contract(self):
        source = inspect.getsource(shadow_runtime.install)
        self.assertIn("candidate_dependency_roots", source)
        self.assertIn("candidate_state_changed", source)
        self.assertIn("drain_candidate_shadow_events", source)
        self.assertIn("candidate-passive:", source)
        self.assertIn("include_parent=False", source)
        self.assertIn("last_candidate_observed_revision", source)
        self.assertIn("DECISION_HEARTBEAT_SECONDS", source)

    def test_passive_candidate_events_never_enter_executor(self):
        source = inspect.getsource(shadow_runtime.install)
        passive = source.split("def _observe_passive_root", 1)[1].split("def drain_candidate_shadow_events", 1)[0]
        self.assertNotIn(".executor.", passive)
        self.assertNotIn("ActionIntent", passive)
        self.assertNotIn(".service(", passive)
        self.assertNotIn(".store.event(", passive)
        self.assertIn('root_rt["passive_observations"]', passive)


if __name__ == "__main__":
    unittest.main()
