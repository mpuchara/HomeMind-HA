"""0.14.99 Hybrid + Candidate hot-path baseline contracts."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"adaptive_ai"/"src"
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))
from agent_candidates import AgentCandidateManager
from hybrid_inference_benchmark import run
from inference_hot_path_metrics import InferenceHotPathMetrics

class InferenceHotPathMetricsTests(unittest.TestCase):
    def test_ram_only_rolling_metrics_report_required_percentiles(self):
        metrics=InferenceHotPathMetrics(window=32)
        for value in range(1,41): metrics.observe("ridge_feature_construction",float(value))
        snap=metrics.snapshot(); row=snap["stages"]["ridge_feature_construction"]
        self.assertEqual(snap["storage"],"ram_only_bounded_no_sqlite")
        self.assertEqual(row["count"],40); self.assertEqual(row["window_count"],32)
        self.assertIsNotNone(row["p50_ms"]); self.assertIsNotNone(row["p95_ms"]); self.assertIsNotNone(row["p99_ms"])
        self.assertEqual(row["max_ms"],40.0)

class HybridHotPathBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.report=run(iterations=4,entity_count=4)
    def test_scenarios_share_context_within_each_live_or_candidate_decision(self):
        expected={"A_ridge_only":1.0,"B_ridge_plus_tiny_mlp_hybrid":1.0,
                  "C_hybrid_plus_ridge_candidate":2.0,"D_hybrid_plus_hybrid_candidate":2.0}
        for name,calls in expected.items():
            row=self.report["scenarios"][name]
            self.assertAlmostEqual(row["work"]["forecast_calls_per_inference"],calls)
            self.assertEqual(row["work"]["model_deserialize_calls_per_inference"],0.0)
            self.assertIn("p95_us",row["total"])
        self.assertEqual(self.report["contract"],"hybrid_inference_shared_context_v2")
        self.assertTrue(self.report["pass"])
    def test_mlp_and_candidate_stages_are_explicit(self):
        s=self.report["scenarios"]
        self.assertIn("mlp_observation_construction",s["B_ridge_plus_tiny_mlp_hybrid"]["stages"])
        self.assertIn("candidate_ridge_feature_construction",s["C_hybrid_plus_ridge_candidate"]["stages"])
        self.assertIn("candidate_mlp_forward",s["D_hybrid_plus_hybrid_candidate"]["stages"])
        guard=s["D_hybrid_plus_hybrid_candidate"]["guard_metrics"]
        self.assertIn("hybrid_ridge_guard",guard); self.assertIn("candidate_hybrid_ridge_guard",guard)
    def test_candidate_wrapper_runs_only_lightweight_post_live_hook_before_return(self):
        order=[]; engine=SimpleNamespace(inference_hot_path_metrics=None)
        engine.process_agent=lambda *a,**k:(order.append("live") or {"ok":True})
        manager=SimpleNamespace(engine=engine,before_live_process=lambda *a,**k:order.append("before"),
                                after_live_process=lambda *a,**k:order.append("enqueue"))
        AgentCandidateManager._install_process_wrapper(manager)
        result=engine.process_agent({"id":"a"},{},{"sensor.x"})
        self.assertEqual(result,{"ok":True}); self.assertEqual(order,["before","live","enqueue"])
if __name__=="__main__": unittest.main()
