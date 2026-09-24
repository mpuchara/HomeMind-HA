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

# Importing the executable installs only the benchmark-time Engine/Executor clock bridge;
# it does not compose/start the shipped runtime until its isolated worker is invoked.
import run_product_runtime_benchmark as runner
import benchmark_product_runtime as bench
from runtime_composition import ENTRYPOINT_CHAIN, CONTRACT_VERSION


class ProductRuntimeBenchmarkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Component-level deterministic benchmark contract. The expensive *fully composed*
        # 3-seed product run is a dedicated CI step, not duplicated in every Python matrix.
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
        manual = bench.observation(
            11, 1, "manual_change", "future", 25, 0, manual_user=True
        )
        self.assertEqual(manual[bench.TARGET]["context"]["user_id"], "benchmark-user")
        self.assertNotIn("light_need", json.dumps(manual))

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

    def test_future_scenario_context_reaches_controller_and_sensor_move_changes_topology(self):
        class DummyBackend:
            def predict(self, features):
                return {"value": 0.0}, 0.9, [], 1, 0.9, 0.1

        first = bench.observation(11, 1, "one_occupant", "train", 0, 0)
        runtime = bench.FeatureRuntime(bench.agent_template(), first)
        controller = bench.RidgeShadow(DummyBackend(), runtime)
        states = bench.observation(11, 2, "sensor_moved", "future", 20, 0)
        _, meta = controller.decide(
            states, 20, 1700000020.0, scenario="sensor_moved", phase="future"
        )
        self.assertEqual(runtime.registry[bench.SENSORS[1]]["area_id"], "adjacent_room")
        self.assertEqual(meta["sensor_area"], "adjacent_room")

    def test_manual_hold_effective_action_is_separate_from_shadow_prediction(self):
        states = bench.observation(11, 1, "manual_change", "future", 26, 0)
        self.assertEqual(
            bench._manual_hold_action(states, {"manual_override_until": 400.0}, 100.0), 0
        )
        self.assertIsNone(
            bench._manual_hold_action(states, {"manual_override_until": 100.0}, 100.0)
        )
        metrics = self.seed["metrics"]["production_current"]
        self.assertEqual(metrics["manual_override_events"], 1)
        self.assertEqual(metrics["manual_override_window_ticks"], 11)
        self.assertGreater(metrics["runtime_manual_hold_ticks"], 0)
        self.assertGreater(metrics["executor_shadow_ticks"], 0)
        self.assertGreater(metrics["moved_sensor_topology_ticks"], 0)

    def test_validation_is_real_and_control_threshold_is_not_lowered(self):
        built = bench.build_training_data(11, replicas=1)
        detail = built["validation_detail"]
        self.assertEqual(detail["source"], "chronological_validation_demonstrations_not_hidden_future_truth")
        self.assertGreater(detail["counts"]["samples"], 0)
        self.assertGreater(detail["counts"]["per_action"]["0"]["samples"], 0)
        self.assertGreater(detail["counts"]["per_action"]["1"]["samples"], 0)
        qualification = built["control_qualification"]
        self.assertEqual(qualification["threshold"], 0.78)
        self.assertEqual(qualification["minimum_samples_per_action"], 20)
        self.assertAlmostEqual(qualification["confidence_z"], 1.96)

    def test_final_shipped_runtime_composition_is_the_dedicated_ci_quality_path(self):
        expected = (
            "run.sh", "trial_queue_main.py", "preference_queue_main.py",
            "fast_queue_main.py", "queue_main.py", "main.py",
        )
        self.assertEqual(ENTRYPOINT_CHAIN, expected)
        self.assertEqual(CONTRACT_VERSION, 2)
        source = (TOOLS / "run_product_runtime_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("import trial_queue_main as shipped", source)
        self.assertIn("shipped_core.prepare_runtime_extensions()", source)
        self.assertIn("shipped_core.prepare_engine_extensions()", source)
        self.assertIn("fresh process", source)
        self.assertIn("final RuntimeCompositionRoot", source)
        self.assertIn("registry=core.registry_for(phase, scenario)", source)
        self.assertIn("core._manual_hold_action(states, rt, ts)", source)
        self.assertIn('store.update_agent(agent["id"], {"mode": "shadow"})', source)
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("python tools/run_product_runtime_benchmark.py --seeds 11,23,37 --replicas 1", workflow)

    def test_same_seed_has_identical_quality_even_if_wall_clock_changes(self):
        second = bench.run([11], replicas=1)
        for name in self.report["comparators"]:
            self.assertEqual(bench._quality_view(self.report["per_seed"][0]["metrics"][name]),
                             bench._quality_view(second["per_seed"][0]["metrics"][name]))
        self.assertEqual(self.report["unmet_criteria"], second["unmet_criteria"])

    def test_benchmark_never_auto_deploys_challenger(self):
        self.assertFalse(self.report["model_deployment"]["automatic"])
        self.assertFalse(self.report["model_deployment"]["full_ridge_default_changed"])
        self.assertIn("full_ridge_shadow", self.report["comparators"])
        self.assertEqual(self.report["runtime_scope"]["ha_service_dispatch"],
                         "forbidden/asserted in Shadow benchmark")

    def test_build_info_records_real_entrypoint_and_product_benchmark_contract(self):
        build = json.loads((ROOT / "adaptive_ai" / "BUILD_INFO.json").read_text(encoding="utf-8"))
        self.assertEqual(build["product_benchmark_contract"], 2)
        self.assertIn("trial_queue_main.py", build["production_entrypoint"])
        self.assertEqual(build["product_benchmark_seeds"], [11, 23, 37])
        self.assertIsInstance(build["product_benchmark_unmet_criteria"], list)
        self.assertIn("not deployed", build["product_benchmark_full_ridge"])
        self.assertIn("component fixture", build["anticipation_simulator"])
        self.assertEqual(build["product_benchmark_unmet_criteria"], [
            "needed_light_not_worse_than_fixed_by_more_than_2pp",
        ])
        self.assertEqual(build["tests_passed"], 1201)
        self.assertIn("6 s", build["fast_light_off_confirmation"])
        self.assertIn("82.52%", build["product_benchmark_control_qualification"])
        report = (ROOT / "BENCHMARK_PRODUCT_F24.md").read_text(encoding="utf-8")
        self.assertIn("Needed-light fraction", report)
        self.assertIn("Runtime manual-hold ticks", report)
        self.assertIn("manual_override_enters_runtime_hold", report)
        self.assertIn("Product acceptance is represented by the explicit criteria above", report)


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
