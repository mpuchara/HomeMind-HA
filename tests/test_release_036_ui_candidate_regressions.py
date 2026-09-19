"""0.14.36 UI/Candidate read-path regressions found on Raspberry Pi soak."""
import unittest
from pathlib import Path

from support import ROOT


class Release036UiCandidateRegressionTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_routing_cache_keeps_all_configs_separate_from_inference_subset(self):
        source = self.source("engine.py")
        refresh = source.split("def _refresh_agent_index", 1)[1].split(
            "def _active_agents_for_changes", 1
        )[0]
        self.assertIn("all_configs = {}", refresh)
        self.assertIn("all_configs[aid] = agent", refresh)
        self.assertIn("if not self.inference_eligible(agent):", refresh)
        self.assertIn("self.all_agent_configs = all_configs", refresh)
        self.assertIn("self.agent_configs = active", refresh)

    def test_hot_ui_uses_complete_config_cache_and_truthful_home_diagnostics(self):
        source = self.source("release_017_ui_lifeline.py")
        hot = source.split("def hot_configs():", 1)[1].split("def snapshot():", 1)[0]
        self.assertIn("core.ENGINE.all_agent_configs.values()", hot)
        self.assertNotIn("core.STORE.list_agent_configs()", hot)
        self.assertIn("def hot_home_diagnostics():", source)
        self.assertIn("core.ENGINE.context.diagnostics()", source)
        self.assertIn('payload["home_intelligence"] = home_intelligence', source)
        self.assertIn('"active_inference_agent_count": active_inference_agent_count', source)

    def test_candidate_card_reads_do_not_run_history_aggregate_agent_queries(self):
        source = self.source("agent_candidates.py")
        summary = source.split("def _comparison_summary", 1)[1].split(
            "def _status_from_row", 1
        )[0]
        status = source.split("def _status_from_row", 1)[1].split(
            "@staticmethod", 1
        )[0]
        self.assertNotIn("self.store.get_agent(", summary)
        self.assertNotIn("self.store.get_agent(", status)
        self.assertIn("self.store.get_agent_config", summary)
        self.assertIn("candidate = self.store.get_agent_config", status)

    def test_candidate_list_status_evaluates_each_edge_once(self):
        source = self.source("agent_candidates.py")
        block = source.split("def list_status(self):", 1)[1].split(
            "@staticmethod", 1
        )[0]
        self.assertIn("status = self._status_from_row(row)", block)
        self.assertNotIn("self.status(r[", block)

    def test_preference_decorator_stays_on_config_only_candidate_reads(self):
        source = self.source("agent_candidate_preference_metrics.py")
        install = source.split("def install(manager):", 1)[1]
        self.assertNotIn("manager.store.get_agent(row.get", install)
        self.assertIn("manager.store.get_agent_config(row.get", install)

    def test_late_promotion_and_confidence_wrappers_do_not_reintroduce_aggregate_reads(self):
        for name in ("promotion_validation.py", "confidence_contract.py"):
            source = self.source(name)
            install = source.split("def install(manager", 1)[1]
            self.assertNotIn("manager.store.get_agent(row.get", install, name)
            self.assertIn("manager.store.get_agent_config(row.get", install, name)

    def test_candidate_ui_does_not_outpoll_main_ui_on_pi(self):
        source = (ROOT / "adaptive_ai" / "src" / "static" / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("setTimeout(loop,4000)", source)
        self.assertNotIn("setTimeout(loop,1500)", source)


if __name__ == "__main__":
    unittest.main()
