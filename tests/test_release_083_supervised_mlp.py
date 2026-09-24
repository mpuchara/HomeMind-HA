"""0.14.83 Stage-4 offline supervised Tiny MLP + neutral tournament contracts."""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import threading
import unittest

from candidate_neural_shadow import install as install_candidate_neural_shadow
from observation_space import ObservationMask, observation_schema_id
from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_training import (
    build_training_artifact,
    evaluate_supervised,
    tournament_result,
    train_supervised,
)
from storage import Store
from tiny_mlp_shadow import (
    load_training_record,
    publish_training_artifact,
    restore_training_record,
)


FEATURE_IDS = ("feature:x", "feature:bias")


def backend():
    return TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=FEATURE_IDS,
        schema_id=observation_schema_id(),
        mask_id="stage4-test-mask",
        hidden=(8, 4),
        init_seed=1483,
    )


def observation(x):
    return {
        "feature_ids": list(FEATURE_IDS),
        "values": [float(x), 1.0],
    }


def classification_rows(n=160):
    rows = []
    for idx in range(n):
        x = -1.0 + 2.0 * (idx % 40) / 39.0
        rows.append({
            "observation": observation(x),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(idx),
        })
    return rows


class SupervisedTrainingTests(unittest.TestCase):
    def test_offline_supervised_training_learns_and_online_update_stays_disabled(self):
        model = backend()
        report = train_supervised(
            model,
            classification_rows(),
            max_epochs=18,
            batch_size=16,
            learning_rate=0.03,
            gradient_clip=1.0,
            early_stop_patience=4,
        )
        self.assertTrue(report["trained"])
        self.assertTrue(model.trained)
        self.assertGreater(model.training_samples, 0)
        metrics = evaluate_supervised(model, {
            "target_property": "power",
            "deadband": .5,
            "min_value": 0,
            "max_value": 1,
        }, classification_rows(80))
        self.assertGreater(metrics["score"], 0.90)
        chosen, confidence, _arms, _h, _support, _novelty = model.predict(observation(.8))
        self.assertEqual(chosen["index"], 1)
        self.assertGreater(confidence, .5)
        self.assertEqual(chosen["confidence_kind"], "softmax_uncalibrated")
        with self.assertRaisesRegex(RuntimeError, "training is disabled"):
            model.update(1, 1, observation(.8), 1.0)

    def test_trained_model_roundtrip_keeps_normalization_and_prediction(self):
        model = backend()
        train_supervised(model, classification_rows(), max_epochs=10, learning_rate=.03)
        raw = model.serialize()
        restored = TinyMLPBackend.deserialize(
            raw,
            expected_schema_id=model.schema_id,
            expected_mask_id=model.mask_id,
            expected_feature_ids=model.feature_ids,
            expected_actions=model.actions,
            expected_horizons=model.horizons,
        )
        self.assertTrue(restored.trained)
        self.assertEqual(restored.training_samples, model.training_samples)
        self.assertEqual(list(restored.input_mean), list(model.input_mean))
        self.assertEqual(list(restored.input_scale), list(model.input_scale))
        self.assertEqual(
            restored.predict(observation(.75))[0]["index"],
            model.predict(observation(.75))[0]["index"],
        )


class NeutralTournamentTests(unittest.TestCase):
    def setUp(self):
        self.agent = {
            "target_property": "power",
            "deadband": .5,
            "min_value": 0,
            "max_value": 1,
        }
        self.ridge = {
            "samples": 20,
            "correct": 15,
            "per_action": {
                "0": {"samples": 10, "correct": 7},
                "1": {"samples": 10, "correct": 8},
            },
        }
        self.mlp = {
            "samples": 20,
            "correct": 18,
            "per_action": {
                "0": {"samples": 10, "correct": 9},
                "1": {"samples": 10, "correct": 9},
            },
            "score": .90,
            "class_coverage": True,
            "per_action_accuracy": {"0": .9, "1": .9},
        }

    def result(self, **changes):
        mlp = {**self.mlp, **changes.pop("mlp", {})}
        return tournament_result(
            agent=self.agent,
            actions=(0.0, 1.0),
            ridge_stats=changes.pop("ridge", self.ridge),
            mlp_metrics=mlp,
            threshold=changes.pop("threshold", .78),
            minimum_samples=changes.pop("minimum_samples", 12),
            minimum_gain=changes.pop("minimum_gain", 0.0),
            parameter_count=changes.pop("parameter_count", 4000),
            serialized_bytes=changes.pop("serialized_bytes", 80000),
            **changes,
        )

    def test_mlp_wins_only_when_strictly_better_on_same_holdout(self):
        result = self.result()
        self.assertTrue(result["passed"])
        self.assertEqual(result["selected_backend"], "tiny_mlp")
        self.assertTrue(result["same_holdout_rows"])
        self.assertGreater(result["gain"], 0)

    def test_equal_or_worse_mlp_keeps_ridge(self):
        result = self.result(
            threshold=.70,
            mlp={
                "score": .75,
                "per_action_accuracy": {"0": .75, "1": .75},
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["selected_backend"], "diagonal_linucb")
        self.assertEqual(result["reason"], "ridge_equal_or_better_on_identical_holdout")

    def test_holdout_mismatch_or_resource_failure_can_never_select_mlp(self):
        mismatch = self.result(mlp={"samples": 19})
        self.assertFalse(mismatch["passed"])
        self.assertEqual(mismatch["reason"], "holdout_sample_mismatch")
        resource = self.result(parameter_count=60000)
        self.assertFalse(resource["passed"])
        self.assertEqual(resource["reason"], "mlp_resource_gate_failed")


class TrainingArtifactPersistenceTests(unittest.TestCase):
    def test_verified_trained_artifact_persists_tournament_without_touching_rl_model(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage4-") as root:
            store = Store(Path(root) / "stage4.db")
            model = backend()
            trainer = train_supervised(model, classification_rows(), max_epochs=10, learning_rate=.03)
            tournament = {
                "passed": True,
                "selected_backend": "tiny_mlp",
                "mlp_score": .9,
                "ridge_score": .75,
                "samples": 20,
            }
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=FEATURE_IDS,
                features=(
                    {"id": FEATURE_IDS[0]},
                    {"id": FEATURE_IDS[1]},
                ),
                selected_entities=(),
                global_feature_count=2,
                missing_feature_count=0,
            )
            # The backend uses a synthetic mask id in this low-level fixture; align it
            # with the real exported mask before building the artifact.
            model.mask_id = mask.mask_id
            policy = SimpleNamespace(
                tournament_revision="ridge-generation-4",
                model_revision="ridge-model",
            )
            agent = {"id": "candidate-stage4"}
            artifact = build_training_artifact(
                agent=agent,
                policy=policy,
                mask=mask,
                backend=model,
                trainer=trainer,
                tournament=tournament,
            )
            published = publish_training_artifact(store, artifact)
            self.assertEqual(published["selected_backend"], "tiny_mlp")
            record = load_training_record(store, agent["id"])
            self.assertTrue(record["model"]["trained"])
            self.assertEqual(record["selected_backend"], "tiny_mlp")
            self.assertEqual(record["tournament"]["mlp_score"], .9)
            self.assertIsNone(store.get_model(agent["id"]))


    def test_training_record_restore_reverts_or_deletes_new_artifact(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage4-restore-") as root:
            store = Store(Path(root) / "stage4.db")
            model = backend()
            train_supervised(model, classification_rows(), max_epochs=6, learning_rate=.03)
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=FEATURE_IDS,
                features=tuple({"id": fid} for fid in FEATURE_IDS),
                selected_entities=(),
                global_feature_count=2,
                missing_feature_count=0,
            )
            model.mask_id = mask.mask_id
            artifact = build_training_artifact(
                agent={"id": "candidate-rollback"},
                policy=SimpleNamespace(tournament_revision="ridge-r1", model_revision="m1"),
                mask=mask,
                backend=model,
                trainer={"trained": True},
                tournament={"passed": True, "selected_backend": "tiny_mlp"},
            )
            self.assertIsNone(load_training_record(store, "candidate-rollback"))
            publish_training_artifact(store, artifact)
            self.assertIsNotNone(load_training_record(store, "candidate-rollback"))
            restore_training_record(store, "candidate-rollback", None)
            self.assertIsNone(load_training_record(store, "candidate-rollback"))


class CandidateNeuralShadowTests(unittest.TestCase):
    def manager(self, *, offline_passed=True, selected=True):
        calls = []
        record = {
            "model": {"trained": True},
            "mask": {"mask_id": "m"},
            "selected_backend": "tiny_mlp" if selected else "diagonal_linucb",
            "tournament": {"passed": bool(selected), "selected_backend": "tiny_mlp"},
        }

        class Service:
            def persisted_record(self, agent_id):
                return dict(record)

            def predict_persisted(self, agent, policy, state_map, temporal, **kwargs):
                calls.append((agent["id"], kwargs.get("require_selected")))
                return {
                    "backend": SimpleNamespace(
                        model_revision="mlp-rev",
                        mask_id="mask-rev",
                    ),
                    "chosen": {
                        "value": 1.0,
                        "confidence_kind": "softmax_uncalibrated",
                    },
                    "confidence": .87,
                }

        class StoreStub:
            def get_agent_config(self, agent_id):
                return {
                    "id": str(agent_id),
                    "target_property": "power",
                }

            def get_model(self, agent_id):
                return {"policy_backend": "diagonal_linucb"}

        engine = SimpleNamespace(
            tiny_mlp_shadow=Service(),
            models={"candidate-1": SimpleNamespace()},
            temporal_history=object(),
        )
        manager = SimpleNamespace(
            engine=engine,
            store=StoreStub(),
            status=lambda parent_id: {"candidate_id": "candidate-1"},
            list_status=lambda *a, **k: [{"candidate_id": "candidate-1"}],
            promote=lambda parent_id, *a, **k: {"promoted": parent_id},
            promote_custom=lambda parent_id, *a, **k: {"custom_promoted": parent_id},
            _row_by_candidate=lambda candidate_id: {
                "candidate_id": candidate_id,
                "offline_gate_json": json.dumps({"passed": offline_passed}),
            },
        )
        return install_candidate_neural_shadow(manager), calls

    def test_selected_mlp_routes_candidate_shadow_only_after_existing_offline_gate(self):
        manager, calls = self.manager(offline_passed=True, selected=True)
        result = manager.candidate_backend_predictor(
            {
                "generation_id": "g2",
                "agent_id": "candidate-1",
            },
            {"light.test": {"state": "off"}},
            123.0,
        )
        self.assertEqual(result["desired"], 1.0)
        self.assertEqual(result["policy_backend"], "tiny_mlp")
        self.assertEqual(calls, [("candidate-1", True)])
        status = manager.status("root")
        self.assertTrue(status["candidate_neural_shadow_active"])
        self.assertFalse(status["candidate_neural_physical_authority"])

    def test_selected_neural_candidate_cannot_be_promoted_to_live_or_control(self):
        manager, _calls = self.manager(offline_passed=True, selected=True)
        with self.assertRaisesRegex(ValueError, "Shadow-only"):
            manager.promote("candidate-1")
        with self.assertRaisesRegex(ValueError, "Shadow-only"):
            manager.promote_custom("candidate-1")

    def test_failed_offline_gate_or_ridge_tournament_keeps_existing_candidate_backend(self):
        blocked, calls = self.manager(offline_passed=False, selected=True)
        self.assertIsNone(blocked.candidate_backend_predictor(
            {"generation_id": "g2", "agent_id": "candidate-1"}, {}, 123.0
        ))
        self.assertEqual(calls, [])
        ridge, calls = self.manager(offline_passed=True, selected=False)
        self.assertIsNone(ridge.candidate_backend_predictor(
            {"generation_id": "g2", "agent_id": "candidate-1"}, {}, 123.0
        ))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
