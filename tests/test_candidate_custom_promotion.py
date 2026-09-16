from pathlib import Path
import unittest

from agent_candidate_user_promotion import evaluate_custom_rules, normalize_rules


ROOT = Path(__file__).resolve().parents[1]


def status(*, state="offline_blocked", gate_passed=False, samples=6, on_events=3, off_events=3, gain=0.0):
    return {
        "state": state,
        "training_state": "qualified",
        "offline_gate": {"status": "failed" if not gate_passed else "passed", "passed": gate_passed},
        "comparison": {
            "samples": samples,
            "on_events": on_events,
            "off_events": off_events,
            "accuracy_gain": gain,
            "required_future_samples_per_action": 20,
        },
    }


class CustomPromotionRuleTests(unittest.TestCase):
    def test_blocked_offline_gate_requires_explicit_override(self):
        report = evaluate_custom_rules(status(), {
            "min_future_samples": 6,
            "min_per_binary_action": 2,
            "max_future_regression_pp": 15,
            "allow_offline_gate_override": False,
        })
        self.assertFalse(report["passed"])
        self.assertTrue(any("offline gate" in item for item in report["failures"]))

    def test_user_can_accept_blocked_gate_with_smaller_future_sample_budget(self):
        report = evaluate_custom_rules(status(samples=6, on_events=3, off_events=3, gain=-0.05), {
            "min_future_samples": 6,
            "min_per_binary_action": 2,
            "max_future_regression_pp": 15,
            "allow_offline_gate_override": True,
        })
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["rules"]["min_future_samples"], 6)
        self.assertEqual(report["rules"]["min_per_binary_action"], 2)

    def test_user_can_explicitly_choose_immediate_shadow_promotion_evidence_budget(self):
        report = evaluate_custom_rules(status(samples=0, on_events=0, off_events=0, gain=None), {
            "min_future_samples": 0,
            "min_per_binary_action": 0,
            "max_future_regression_pp": None,
            "allow_offline_gate_override": True,
        })
        self.assertTrue(report["passed"], report)

    def test_user_regression_limit_is_enforced(self):
        report = evaluate_custom_rules(status(samples=8, on_events=4, off_events=4, gain=-0.21), {
            "min_future_samples": 6,
            "min_per_binary_action": 2,
            "max_future_regression_pp": 15,
            "allow_offline_gate_override": True,
        })
        self.assertFalse(report["passed"])
        self.assertTrue(any("regression" in item for item in report["failures"]))

    def test_rules_are_bounded_and_blank_regression_means_ignore(self):
        rules = normalize_rules({
            "min_future_samples": -5,
            "min_per_binary_action": -2,
            "max_future_regression_pp": "",
            "allow_offline_gate_override": "true",
        })
        self.assertEqual(rules["min_future_samples"], 0)
        self.assertEqual(rules["min_per_binary_action"], 0)
        self.assertIsNone(rules["max_future_regression_pp"])
        self.assertTrue(rules["allow_offline_gate_override"])


class CustomPromotionUiContractTests(unittest.TestCase):
    def test_details_open_state_survives_candidate_poll_rebuild(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("const uiState=new Map()", text)
        self.assertIn("detailsOpen", text)
        self.assertIn("details.ontoggle", text)
        self.assertIn("remember(el", text)
        self.assertIn("draft.detailsOpen?'open':''", text)

    def test_custom_promotion_controls_and_endpoint_are_exposed(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("data-promote-custom", text)
        self.assertIn("data-custom-min-future", text)
        self.assertIn("data-custom-min-action", text)
        self.assertIn("data-custom-max-regression", text)
        self.assertIn("data-custom-offline", text)
        self.assertIn("candidate/promote-custom", text)
        self.assertIn("Offline gate reason", text)
        self.assertIn("explicit confirmation accepts the Candidate", text)

    def test_custom_layer_is_installed_outside_atomic_promoter(self):
        text = (ROOT / "adaptive_ai/src/fast_queue_main.py").read_text(encoding="utf-8")
        atomic = text.index("candidates = install_candidate_atomic_promote(candidates)")
        custom = text.index("candidates = install_candidate_user_promotion(candidates)")
        self.assertLess(atomic, custom)

    def test_offline_gate_stays_standard_blocker_but_not_observation_blocker(self):
        text = (ROOT / "adaptive_ai/src/agent_candidate_user_promotion.py").read_text(encoding="utf-8")
        self.assertIn("passed_for_observation_only", text)
        self.assertIn("offline_gate_blocks_standard_promotion_but_not_passive_future_ab_collection", text)
        self.assertIn("original_promote", text)
        self.assertIn("Control qualification", text)


if __name__ == "__main__":
    unittest.main()
