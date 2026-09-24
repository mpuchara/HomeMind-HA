"""0.14.75 Stage-1 contracts: current behavior lock + multi-backend foundation."""
import copy
import unittest

from agent_candidate_lineage import model_metadata
from policy_backend import (
    BACKEND_CAPABILITIES,
    LEGACY_DEFAULT_BACKEND,
    UnsupportedPolicyBackendError,
    backend_id,
    feature_mask_id,
    feature_schema_id,
    model_checksum,
    require_backend,
    serialize_backend_model,
    verify_model_checksum,
)
from policy_backend_stage1_benchmark import synthetic_tiny_mlp_benchmark
from policy_full_ridge import FullRidgeLinUCBBackend
from agent_correct_generation_history import CHART_CONTRACT


class PolicyBackendFoundationTests(unittest.TestCase):
    def test_legacy_model_without_backend_keeps_current_backend(self):
        legacy = {"version": 11, "schema": {"version": 12, "entities": ["binary_sensor.motion"]}}
        self.assertEqual(backend_id(legacy), LEGACY_DEFAULT_BACKEND)
        self.assertEqual(require_backend(legacy, expected="diagonal_linucb"), "diagonal_linucb")

    def test_unknown_backend_is_never_silently_reinterpreted(self):
        with self.assertRaises(UnsupportedPolicyBackendError):
            require_backend({"policy_backend": "mystery_net"})

    def test_tiny_mlp_stage3_is_implemented_but_never_production_active(self):
        self.assertIn("tiny_mlp", BACKEND_CAPABILITIES)
        self.assertTrue(BACKEND_CAPABILITIES["tiny_mlp"]["implemented"])
        self.assertFalse(BACKEND_CAPABILITIES["tiny_mlp"]["production_active_capable"])
        self.assertTrue(BACKEND_CAPABILITIES["tiny_mlp"]["shadow_only"])
        self.assertFalse(BACKEND_CAPABILITIES["tiny_mlp"]["historical_training"])
        self.assertEqual(
            require_backend({"policy_backend": "tiny_mlp"}),
            "tiny_mlp",
        )

    def test_checksum_ignores_store_bookkeeping_but_detects_model_change(self):
        raw = serialize_backend_model(
            {"version": 11, "dims": 128, "schema": {"version": 12, "entities": ["a"]}},
            policy_backend="diagonal_linucb", backend_version=11,
        )
        self.assertTrue(verify_model_checksum(raw))
        stored = dict(raw)
        stored["_history_watermark"] = 123
        stored["_benchmark_counts"] = {"ok": 4}
        self.assertTrue(verify_model_checksum(stored))
        changed = copy.deepcopy(stored)
        changed["schema"]["entities"].append("b")
        self.assertFalse(verify_model_checksum(changed))

    def test_schema_and_mask_identities_are_separate(self):
        a = {"dims": 128, "schema": {"version": 12, "feature_contract_version": 2, "entities": ["a"]}}
        b = {"dims": 128, "schema": {"version": 12, "feature_contract_version": 2, "entities": ["b"]}}
        self.assertEqual(feature_schema_id(a), feature_schema_id(b))
        self.assertNotEqual(feature_mask_id(a), feature_mask_id(b))

    def test_full_ridge_roundtrip_carries_common_envelope(self):
        backend = FullRidgeLinUCBBackend(
            actions=[0, 1], horizons=[1], feature_indices=[0, 1, 2]
        )
        backend.update(1, 1, {0: 1.0, 1: 0.5}, 1.0)
        raw = backend.serialize()
        self.assertEqual(raw["policy_backend"], "full_ridge_linucb")
        self.assertEqual(raw["model_format_version"], 1)
        self.assertTrue(raw["feature_schema_id"])
        self.assertTrue(raw["feature_mask_id"])
        self.assertTrue(verify_model_checksum(raw))
        restored = FullRidgeLinUCBBackend.deserialize(raw)
        self.assertEqual(restored.serialize()["feature_indices"], [0, 1, 2])

    def test_lineage_prefers_serialized_model_checksum(self):
        raw = serialize_backend_model(
            {"version": 11, "dims": 128, "schema": {"version": 12, "entities": ["a"]}},
            policy_backend="diagonal_linucb", backend_version=11,
        )
        meta = model_metadata(raw)
        self.assertEqual(meta["model_identity"], raw["model_checksum"])
        self.assertEqual(meta["policy_backend"], "diagonal_linucb")


class Stage1BenchmarkAndCorrectLockTests(unittest.TestCase):
    def test_synthetic_mlp_harness_is_diagnostics_only_and_reports_required_metrics(self):
        result = synthetic_tiny_mlp_benchmark(iterations=8, event_to_intent_baseline_us=250.0)
        self.assertFalse(result["dispatch_capability"])
        self.assertIn("p50", result["inference_us"])
        self.assertIn("p95", result["inference_us"])
        self.assertIn("p99", result["inference_us"])
        self.assertIn("p95", result["synthetic_update_us"])
        self.assertGreater(result["model_memory_bytes"], 0)
        self.assertGreater(result["projected_model_memory_bytes"]["20_agents"], 0)
        self.assertGreater(result["projected_model_memory_bytes"]["50_agents"], 0)
        self.assertGreaterEqual(result["event_to_intent"]["projected_with_policy_us"], 250.0)

    def test_correct_generation_chart_contract_remains_observed_and_direct_parent_only(self):
        # Functional Agent/Candidate chart and lineage behavior is covered by
        # test_correct_multigeneration_history and test_generation_workflow_acceptance.
        # This release gate pins the public contract name used by those paths.
        self.assertEqual(
            CHART_CONTRACT,
            "observed_direct_parent_vs_child_no_policy_replay",
        )


if __name__ == "__main__":
    unittest.main()
