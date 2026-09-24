"""0.14.82 Stage-3 tiny MLP inference/persistence Shadow contracts."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from observation_space import ObservationMask, observation_schema_id
from policy_backend import backend_capabilities, verify_model_checksum
from policy_tiny_mlp import TinyMLPBackend
from storage import Store
from tiny_mlp_shadow import TinyMLPShadowService, install


FEATURE_IDS = tuple(f"feature:{idx}" for idx in range(12))


def backend(actions=(0.0, 1.0), seed=1482):
    return TinyMLPBackend(
        actions=actions,
        horizons=(1,),
        feature_ids=FEATURE_IDS,
        schema_id="obs-stage3-test",
        mask_id="mask-stage3-test",
        hidden=(32, 16),
        init_seed=seed,
    )


def observation(ids=FEATURE_IDS):
    return {
        "feature_ids": list(ids),
        "values": [((idx % 7) - 3) / 3.0 for idx in range(len(ids))],
    }


class TinyMLPBackendTests(unittest.TestCase):
    def test_backend_is_real_shadow_only_and_online_reward_training_is_disabled(self):
        caps = backend_capabilities("tiny_mlp")
        self.assertTrue(caps["implemented"])
        self.assertFalse(caps["production_active_capable"])
        self.assertTrue(caps["shadow_only"])
        self.assertTrue(caps["historical_training"])
        self.assertTrue(caps["supervised_training"])
        self.assertFalse(caps["online_reward_updates"])

        model = backend()
        diag = model.diagnostics()
        self.assertEqual(diag["backend"], "tiny_mlp")
        self.assertEqual(diag["dtype"], "float32")
        self.assertEqual(diag["architecture"], [12, 32, 16, 2])
        self.assertFalse(diag["trained"])
        self.assertFalse(diag["dispatch_capability"])
        self.assertFalse(diag["physical_authority"])
        with self.assertRaisesRegex(RuntimeError, "training is disabled"):
            model.update(1, 0, observation(), 1.0)

    def test_deterministic_initialization_and_prediction(self):
        first = backend(seed=991)
        second = backend(seed=991)
        self.assertEqual(first.model_revision, second.model_revision)
        self.assertEqual(
            [list(x) for x in first.weights],
            [list(x) for x in second.weights],
        )
        self.assertEqual(
            first.predict(observation())[0]["index"],
            second.predict(observation())[0]["index"],
        )

    def test_binary_and_setpoint_outputs_use_existing_action_values(self):
        binary = backend(actions=(0.0, 1.0))
        setpoint = backend(actions=(18.0, 19.5, 21.0, 22.5))
        chosen_binary = binary.predict(observation())[0]
        chosen_setpoint = setpoint.predict(observation())[0]
        self.assertIn(chosen_binary["value"], binary.actions)
        self.assertIn(chosen_setpoint["value"], setpoint.actions)
        self.assertEqual(len(setpoint.predict(observation())[2]), 4)
        self.assertEqual(setpoint.output_size, 4)

    def test_roundtrip_preserves_checksum_parameters_and_prediction(self):
        first = backend(seed=42)
        raw = first.serialize()
        self.assertTrue(verify_model_checksum(raw))
        restored = TinyMLPBackend.deserialize(
            raw,
            expected_schema_id=first.schema_id,
            expected_mask_id=first.mask_id,
            expected_feature_ids=first.feature_ids,
            expected_actions=first.actions,
            expected_horizons=first.horizons,
        )
        self.assertEqual(restored.serialize()["model_checksum"], raw["model_checksum"])
        self.assertEqual(restored.model_revision, first.model_revision)
        self.assertEqual(restored.parameter_count, first.parameter_count)
        self.assertEqual(
            restored.predict(observation())[0]["index"],
            first.predict(observation())[0]["index"],
        )

    def test_checksum_schema_mask_and_feature_order_are_guarded(self):
        model = backend()
        raw = model.serialize()

        tampered = copy.deepcopy(raw)
        tampered["weights"][0][0] += 0.25
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            TinyMLPBackend.deserialize(tampered)

        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            TinyMLPBackend.deserialize(raw, expected_schema_id="wrong-schema")
        with self.assertRaisesRegex(ValueError, "mask mismatch"):
            TinyMLPBackend.deserialize(raw, expected_mask_id="wrong-mask")
        with self.assertRaisesRegex(ValueError, "feature order mismatch"):
            TinyMLPBackend.deserialize(
                raw, expected_feature_ids=tuple(reversed(FEATURE_IDS))
            )

    def test_predict_rejects_reordered_runtime_observation(self):
        model = backend()
        with self.assertRaisesRegex(ValueError, "feature order mismatch"):
            model.predict(observation(tuple(reversed(FEATURE_IDS))))


class TinyMLPShadowPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-stage3-mlp-")
        self.store = Store(Path(self.temp.name) / "stage3.db")
        self.engine = SimpleNamespace(lock=threading.RLock(), runtime={})
        self.agent = {"id": "candidate-stage3-test", "mode": "shadow"}
        self.policy = SimpleNamespace(actions=[0.0, 1.0], horizons=[1])
        self.mask = ObservationMask(
            schema_id=observation_schema_id(),
            mask_version=1,
            feature_ids=FEATURE_IDS,
            features=tuple({"id": fid} for fid in FEATURE_IDS),
            selected_entities=(),
            global_feature_count=len(FEATURE_IDS),
            missing_feature_count=0,
        )
        self.source_policy_revision = "ridge-rev-stage3"

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_id_model_persists_and_reloads_after_service_restart(self):
        first_service = TinyMLPShadowService(
            self.store, self.engine, enabled=True
        )
        first, source = first_service._backend(
            self.agent,
            self.policy,
            self.mask,
            source_policy_revision=self.source_policy_revision,
        )
        self.assertEqual(source, "created")
        first_raw = first_service.persisted_model(self.agent["id"])
        self.assertTrue(first_raw)
        self.assertEqual(first_raw["policy_backend"], "tiny_mlp")
        persisted_mask = first_service.persisted_mask(self.agent["id"])
        self.assertEqual(persisted_mask["mask_id"], self.mask.mask_id)

        second_service = TinyMLPShadowService(
            self.store, self.engine, enabled=True
        )
        restored, source = second_service._backend(
            self.agent,
            self.policy,
            self.mask,
            source_policy_revision=self.source_policy_revision,
        )
        self.assertEqual(source, "persisted_restart")
        self.assertEqual(
            restored.serialize()["model_checksum"],
            first.serialize()["model_checksum"],
        )
        self.assertEqual(restored.model_revision, first.model_revision)

    def test_mask_change_reinitializes_only_isolated_shadow_copy(self):
        service = TinyMLPShadowService(self.store, self.engine, enabled=True)
        first, _ = service._backend(
            self.agent,
            self.policy,
            self.mask,
            source_policy_revision=self.source_policy_revision,
        )
        changed_ids = FEATURE_IDS[:-1] + ("feature:new",)
        changed_mask = ObservationMask(
            schema_id=observation_schema_id(),
            mask_version=1,
            feature_ids=changed_ids,
            features=tuple({"id": fid} for fid in changed_ids),
            selected_entities=(),
            global_feature_count=len(changed_ids),
            missing_feature_count=0,
        )
        second, source = service._backend(
            self.agent,
            self.policy,
            changed_mask,
            source_policy_revision=self.source_policy_revision,
        )
        self.assertEqual(source, "created")
        self.assertNotEqual(first.mask_id, second.mask_id)
        self.assertEqual(second.mask_id, changed_mask.mask_id)


class TinyMLPAuthorityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-stage3-authority-")
        self.store = Store(Path(self.temp.name) / "authority.db")

    def tearDown(self):
        self.temp.cleanup()

    def _engine(self):
        class FakeEngine:
            def __init__(self):
                self.lock = threading.RLock()
                self.runtime = {}
                self.temporal_history = object()
                self.context = SimpleNamespace()

            def process_agent(self, agent, state_map, changed_entities=None):
                with self.lock:
                    rt = self.runtime.setdefault(str(agent["id"]), {})
                    rt["last_inference_ts"] = float(rt.get("last_inference_ts") or 0) + 1.0
                return {"authoritative": "ridge-result"}

            def policy(self, agent):
                return SimpleNamespace(BACKEND="diagonal_linucb")

        return FakeEngine()

    def test_control_mode_never_calls_neural_policy(self):
        engine = self._engine()
        core = SimpleNamespace(ENGINE=engine, STORE=self.store)
        service = install(core)
        calls = []
        service.observe = lambda *args, **kwargs: calls.append((args, kwargs))
        result = engine.process_agent(
            {"id": "control-agent", "mode": "control"}, {}, {"sensor.x"}
        )
        self.assertEqual(result, {"authoritative": "ridge-result"})
        self.assertEqual(calls, [])
        self.assertNotIn(
            "tiny_mlp_shadow", engine.runtime["control-agent"]
        )

    def test_shadow_observer_runs_after_authoritative_result_and_cannot_replace_it(self):
        engine = self._engine()
        core = SimpleNamespace(ENGINE=engine, STORE=self.store)
        service = install(core)
        calls = []

        def observe(agent, policy, state_map, temporal, **kwargs):
            calls.append((agent["id"], policy.BACKEND, kwargs["timestamp"]))
            return {"chosen_value": 1.0}

        service.observe = observe
        result = engine.process_agent(
            {"id": "shadow-agent", "mode": "shadow"}, {}, {"sensor.x"}
        )
        self.assertEqual(result, {"authoritative": "ridge-result"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "diagonal_linucb")

    def test_shadow_observer_failure_is_fail_open_for_ridge(self):
        engine = self._engine()
        core = SimpleNamespace(ENGINE=engine, STORE=self.store)
        service = install(core)

        def explode(*args, **kwargs):
            raise RuntimeError("synthetic neural failure")

        service.observe = explode
        result = engine.process_agent(
            {"id": "shadow-fail", "mode": "shadow"}, {}, {"sensor.x"}
        )
        self.assertEqual(result, {"authoritative": "ridge-result"})
        diag = engine.runtime["shadow-fail"]["tiny_mlp_shadow"]
        self.assertIn("synthetic neural failure", diag["error"])
        self.assertFalse(diag["dispatch_capability"])
        self.assertFalse(diag["physical_authority"])


if __name__ == "__main__":
    unittest.main()
