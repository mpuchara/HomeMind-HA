from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SRC = ROOT / "adaptive_ai" / "src"
for path in (str(TOOLS), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_product_runtime_benchmark as bench


class ProductRuntimeBenchmarkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One expensive fully-composed seed is shared across contract assertions. A second
        # independent execution is reserved for the determinism test below.
        cls.report = bench.run([11], replicas=1)
        cls.seed = cls.report["per_seed"][0]

    def test_hidden_truth_is_not_an_observation_field_and_light_changes_lux(self):
        truth = bench._truth("night", 20, "future")
        dark = bench.observation(11, 1, "night", "future", 20, 0)
        lit = bench.observation(11, 1, "night", "future", 20, 1)
        self.assertIn("light_need", truth)
        self.assertTrue(truth["occupied"])
        self.assertNotIn("light_need", json.dumps(dark))
        self.assertNotIn("occupied", json.dumps(dark))
        self.assertGreater(float(lit[bench.SENSORS[4]]["state"]), float(dark[bench.SENSORS[4]]["state"]) + 150.0)

    def test_required_scenarios_and_split_ids_are_disjoint(self):
        required = {"one_occupant", "two_occupants", "branch", "stillness", "no_arrival",
                    "quick_return", "day", "night", "manual_change", "false_sensor",
                    "sensor_moved", "habit_shift"}
        self.assertEqual(set(bench.SCENARIOS), required)
        train = set(self.seed["split_episode_ids"]["train"])
        validation = set(self.seed["split_episode_ids"]["validation"])
        future = set(self.seed["split_episode_ids"]["future"])
        self.assertFalse(train & validation)
        self.assertFalse(train & future)
        self.assertFalse(validation & future)

    def test_validation_is_real_and_control_threshold_is_not_lowered(self):
        detail = self.seed["validation_detail"]
        self.assertEqual(detail["source"], "chronological_validation_demonstrations_not_hidden_future_truth")
        self.assertGreater(detail["counts"]["samples"], 0)
        self.assertGreater(detail["counts"]["per_action"]["0"]["samples"], 0)
        self.assertGreater(detail["counts"]["per_action"]["1"]["samples"], 0)
        qualification = self.seed["control_qualification"]
        self.assertEqual(qualification["threshold"], 0.78)
        self.assertEqual(qualification["minimum_samples_per_action"], 20)
        self.assertAlmostEqual(qualification["confidence_z"], 1.96)

    def test_final_shipped_runtime_composition_is_the_quality_path(self):
        expected = [
            "run.sh", "trial_queue_main.py", "preference_queue_main.py",
            "fast_queue_main.py", "queue_main.py", "main.py",
        ]
        self.assertEqual(self.report["runtime_scope"]["entrypoint_chain"], expected)
        self.assertEqual(self.report["runtime_scope"]["composition_contract_versions"], [2])
        self.assertEqual(self.seed["runtime_composition"]["entrypoint_chain"], expected)
        self.assertEqual(self.seed["runtime_composition"]["version"], 2)
        self.assertIn("final RuntimeCompositionRoot", self.report["runtime_scope"]["quality_path"])

    def test_same_seed_has_identical_quality_even_if_wall_clock_changes(self):
        second = bench.run([11], replicas=1)
        for name in self.report["comparators"]:
            self.assertEqual(bench._quality_view(self.report["per_seed"][0]["metrics"][name]),
                             bench._quality_view(second["per_seed"][0]["metrics"][name]))
        self.assertEqual(self.report["unmet_criteria"], second["unmet_criteria"])

    def test_production_shadow_intents_are_not_expired_by_runner_wall_clock(self):
        current = self.seed["metrics"]["production_current"]
        self.assertLess(current["fallback_ticks"], current["decision_calls"])

    def test_benchmark_never_auto_deploys_challenger(self):
        self.assertFalse(self.report["model_deployment"]["automatic"])
        self.assertFalse(self.report["model_deployment"]["full_ridge_default_changed"])
        self.assertIn("full_ridge_shadow", self.report["comparators"])
        self.assertEqual(self.report["runtime_scope"]["ha_service_dispatch"],
                         "forbidden/asserted in Shadow benchmark")

    def test_build_info_records_real_entrypoint_and_product_benchmark_contract(self):
        build = json.loads((ROOT / "adaptive_ai" / "BUILD_INFO.json").read_text(encoding="utf-8"))
        self.assertEqual(build["product_benchmark_contract"], 1)
        self.assertIn("trial_queue_main.py", build["production_entrypoint"])
        self.assertEqual(build["product_benchmark_seeds"], [11, 23, 37])
        self.assertIsInstance(build["product_benchmark_unmet_criteria"], list)
        self.assertIn("not deployed", build["product_benchmark_full_ridge"])
        self.assertIn("component fixture", build["anticipation_simulator"])
        self.assertGreaterEqual(build["tests_passed"], 784)


class ProductRuntimeBenchmarkFixtureNamingTests(unittest.TestCase):
    def test_legacy_anticipation_simulator_admits_component_fixture_scope(self):
        text = (TOOLS / "simulate_anticipation.py").read_text(encoding="utf-8")
        self.assertIn("fixture", text)
        self.assertIn("permissive per-agent threshold", text)
        self.assertIn("benchmark_score=1.0", text)
        product = (TOOLS / "benchmark_product_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("from simulate_anticipation import fixture", product)


if __name__ == "__main__":
    unittest.main()
