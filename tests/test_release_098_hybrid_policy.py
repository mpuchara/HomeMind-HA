"""0.14.98 Tiny MLP action selector + Ridge safety/fallback contracts."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hybrid_policy_runtime import HybridPolicyService
from settings import OPTIONS


class FakeHead:
    def __init__(self, structural=.92, ceiling=.88, accuracy=.91, samples=24):
        self.structural = float(structural)
        self.ceiling = float(ceiling)
        self.accuracy = float(accuracy)
        self.samples = int(samples)

    def structural_confidence(self, arms, action_idx):
        return self.structural

    def calibration(self, action_idx):
        return {
            "accuracy": self.accuracy,
            "ceiling": self.ceiling,
            "samples": self.samples,
        }


class FakePolicy:
    def __init__(self, *, revision="ridge-generation-1", head=None):
        self.actions = [0.0, 1.0]
        self.horizons = [1]
        self.tournament_revision = revision
        self.model_revision = "ridge-model-current"
        self.heads = {1: head or FakeHead()}


class FakeNeural:
    def __init__(
        self,
        *,
        source_revision="ridge-generation-1",
        selected_backend="tiny_mlp",
        tournament_passed=True,
        trained=True,
        chosen_index=1,
        confidence=.93,
    ):
        self.source_revision = source_revision
        self.selected_backend = selected_backend
        self.tournament_passed = bool(tournament_passed)
        self.trained = bool(trained)
        self.chosen_index = int(chosen_index)
        self.confidence = float(confidence)

    def persisted_record(self, agent_id):
        return {
            "selected_backend": self.selected_backend,
            "source_policy_revision": self.source_revision,
            "model": {
                "trained": self.trained,
                "model_revision": "mlp-r7",
            },
            "mask": {
                "mask_id": "mask-hybrid",
                "selected_entities": [
                    "binary_sensor.room_presence",
                    "sensor.room_lux",
                ],
            },
            "tournament": {
                "passed": self.tournament_passed,
                "selected_backend": self.selected_backend,
            },
        }

    def predict_persisted(
        self, agent, policy, state_map, temporal, *, timestamp, require_selected=False
    ):
        chosen = {
            "index": self.chosen_index,
            "value": float(policy.actions[self.chosen_index]),
        }
        return {
            "backend": SimpleNamespace(
                model_revision="mlp-r7",
                mask_id="mask-hybrid",
            ),
            "chosen": chosen,
            "confidence": self.confidence,
        }


def ridge_inputs():
    return {
        "ridge_chosen": {
            "index": 0,
            "value": 0.0,
            "uncertainty": .12,
            "mean": .65,
        },
        "ridge_confidence": .90,
        "ridge_arms": [
            {
                "index": 0,
                "value": 0.0,
                "mean": .65,
                "uncertainty": .12,
                "support": .82,
                "novelty": .10,
            },
            {
                "index": 1,
                "value": 1.0,
                "mean": .52,
                "uncertainty": .18,
                "support": .74,
                "novelty": .12,
            },
        ],
        "ridge_horizon": 1,
        "ridge_support": .82,
        "ridge_novelty": .10,
    }


class HybridPolicyTests(unittest.TestCase):
    def service(self, neural=None):
        engine = SimpleNamespace(tiny_mlp_shadow=neural or FakeNeural())
        return HybridPolicyService(engine)

    def agent(self):
        return {
            "id": "agent-1",
            "confidence_threshold": .80,
            "target_property": "power",
        }

    def evaluate(self, service, policy=None):
        return service.evaluate(
            self.agent(),
            policy or FakePolicy(),
            {},
            object(),
            timestamp=1000.0,
            **ridge_inputs(),
        )

    def test_selected_mlp_can_choose_action_but_ridge_owns_safety_values(self):
        with patch.dict(
            OPTIONS,
            {
                "hybrid_policy_enabled": True,
                "hybrid_policy_min_ridge_confidence": .60,
                "hybrid_policy_min_ridge_support": .20,
                "hybrid_policy_max_ridge_novelty": .85,
                "hybrid_policy_min_mlp_decision_strength": .55,
                "min_historical_support": .20,
                "max_context_novelty": .85,
            },
            clear=False,
        ):
            result = self.evaluate(self.service())

        self.assertTrue(result["applied"])
        self.assertEqual(result["chosen"]["index"], 1)
        self.assertEqual(result["chosen"]["value"], 1.0)
        self.assertAlmostEqual(result["confidence"], .88)
        self.assertAlmostEqual(result["support"], .74)
        self.assertAlmostEqual(result["novelty"], .12)
        self.assertEqual(result["horizon"], 1)
        self.assertEqual(
            set(result["dependencies"]),
            {"binary_sensor.room_presence", "sensor.room_lux"},
        )
        self.assertEqual(result["decision_source"], "hybrid_tiny_mlp_ridge_guard")
        self.assertFalse(result["agreement"])

    def test_low_ridge_confidence_rejects_neural_override_and_keeps_fallback(self):
        policy = FakePolicy(head=FakeHead(structural=.70, ceiling=.72))
        with patch.dict(OPTIONS, {"hybrid_policy_enabled": True}, clear=False):
            result = self.evaluate(self.service(), policy)
        self.assertFalse(result["applied"])
        self.assertIn("ridge_confidence", result["reason"])
        self.assertNotIn("chosen", result)

    def test_stale_neural_generation_never_applies(self):
        with patch.dict(OPTIONS, {"hybrid_policy_enabled": True}, clear=False):
            result = self.evaluate(
                self.service(FakeNeural(source_revision="old-ridge-generation"))
            )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "neural_source_revision_stale")

    def test_unselected_or_failed_neural_tournament_is_plain_ridge_fallback(self):
        with patch.dict(OPTIONS, {"hybrid_policy_enabled": True}, clear=False):
            unselected = self.evaluate(
                self.service(FakeNeural(selected_backend="diagonal_linucb"))
            )
            failed = self.evaluate(
                self.service(FakeNeural(tournament_passed=False))
            )
        self.assertEqual(unselected["reason"], "ridge_selected_by_tournament")
        self.assertEqual(failed["reason"], "neural_tournament_not_passed")
        self.assertFalse(unselected["applied"])
        self.assertFalse(failed["applied"])


if __name__ == "__main__":
    unittest.main()
