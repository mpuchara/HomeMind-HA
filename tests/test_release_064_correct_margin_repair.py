"""0.14.64 Stage-2 Correct margin-repair and stable-base regression tests."""

import json
import threading
import unittest

from support import ROOT

from correct_margin_repair import (
    _repair_margins,
    _select_stable_candidate,
)
from policy import DiagonalLinUCB


SRC = ROOT / "adaptive_ai" / "src"


class _FakeHead:
    def __init__(self, means, step=0.25, learns=True):
        self.means = list(means)
        self.step = float(step)
        self.learns = bool(learns)

    def evaluate(self, _features):
        return [
            {
                "index": index,
                "value": float(index),
                "mean": float(mean),
                "uncertainty": 0.0,
                "ucb": float(mean),
                "count": 10,
                "support": 1.0,
                "novelty": 0.0,
            }
            for index, mean in enumerate(self.means)
        ]

    def update(self, action_idx, _features, reward, sample_ts=None):
        if self.learns:
            self.means[int(action_idx)] += self.step * float(reward)


class _FakePolicy:
    def __init__(self, means=(4.0, 0.0), step=0.25, learns=True):
        self.actions = [0.0, 1.0]
        self.horizons = [0]
        self.heads = {0: _FakeHead(means, step=step, learns=learns)}
        self.lock = threading.RLock()

    def predict(self, features):
        arms = self.heads[0].evaluate(features)
        chosen = max(arms, key=lambda arm: (float(arm["mean"]), -int(arm["index"])))
        return dict(chosen), 1.0, arms, 0, 1.0, 0.0


def _sample(event_id="correct-on", desired_idx=1):
    return {
        "label": {
            "id": 1,
            "supervision_event_id": event_id,
            "fingerprint": "fixture",
            "sample_ts": 123.0,
            "desired": float(desired_idx),
        },
        "features": {0: 1.0, 1: 1.0},
        "desired_idx": int(desired_idx),
    }


class CorrectMarginRepairTests(unittest.TestCase):
    def test_positive_only_stalls_but_pairwise_margin_repair_crosses_boundary(self):
        positive_only = _FakePolicy(means=(4.0, 0.0), step=0.25)
        for _ in range(12):
            positive_only.heads[0].update(1, _sample()["features"], 1.0)
        chosen, *_ = positive_only.predict(_sample()["features"])
        self.assertEqual(chosen["index"], 0, "positive-only repair should reproduce the old stall")

        policy = _FakePolicy(means=(4.0, 0.0), step=0.25)
        repair = _repair_margins(
            policy,
            [_sample()],
            target_margin=0.10,
            max_rounds=12,
            per_label_round_budget=12,
            stall_rounds=2,
            min_progress=1e-6,
        )
        chosen, *_ = policy.predict(_sample()["features"])
        self.assertEqual(chosen["index"], 1)
        self.assertGreaterEqual(repair["after"]["margin_min"], 0.10)
        self.assertGreater(repair["negative_updates"], 0)
        self.assertEqual(repair["positive_updates"], repair["negative_updates"])
        self.assertEqual(repair["stop_reason"], "target_margin_satisfied")

    def test_real_diagonal_linucb_needs_wrong_arm_penalty_to_cross_boundary(self):
        class ActualPolicy:
            def __init__(self):
                self.actions = [0.0, 1.0]
                self.horizons = [0]
                self.heads = {0: DiagonalLinUCB(2, self.actions, 0.0)}
                self.lock = threading.RLock()

            def predict(self, features):
                chosen, confidence, arms = self.heads[0].choose(features, explore=False)
                return chosen, confidence, arms, 0, 1.0, 0.0

        features = {0: 1.0}
        positive_only = ActualPolicy()
        for _ in range(40):
            positive_only.heads[0].update(0, features, 1.0)
        for _ in range(12):
            positive_only.heads[0].update(1, features, 1.0)
        chosen, *_ = positive_only.predict(features)
        self.assertEqual(chosen["index"], 0)

        policy = ActualPolicy()
        for _ in range(40):
            policy.heads[0].update(0, features, 1.0)
        sample = _sample("linucb-boundary")
        sample["features"] = features
        repair = _repair_margins(
            policy,
            [sample],
            target_margin=0.02,
            max_rounds=12,
            per_label_round_budget=12,
            stall_rounds=2,
            min_progress=1e-6,
        )
        chosen, *_ = policy.predict(features)
        self.assertEqual(chosen["index"], 1)
        self.assertGreaterEqual(repair["after"]["margin_min"], 0.02)
        self.assertGreater(repair["negative_updates"], 0)

    def test_no_progress_stops_before_global_round_limit(self):
        policy = _FakePolicy(means=(4.0, 0.0), step=0.25, learns=False)
        repair = _repair_margins(
            policy,
            [_sample()],
            target_margin=0.10,
            max_rounds=12,
            per_label_round_budget=12,
            stall_rounds=2,
            min_progress=1e-6,
        )
        self.assertEqual(repair["stop_reason"], "no_margin_progress")
        self.assertLess(repair["rounds"], 12)
        self.assertEqual(repair["after"]["fit"], 0)

    def test_per_label_budget_prevents_unbounded_repeat_evidence(self):
        policy = _FakePolicy(means=(4.0, 0.0), step=0.10)
        repair = _repair_margins(
            policy,
            [_sample("budgeted")],
            target_margin=0.10,
            max_rounds=12,
            per_label_round_budget=1,
            stall_rounds=3,
            min_progress=1e-6,
        )
        self.assertEqual(repair["per_label_rounds"]["budgeted"], 1)
        self.assertEqual(repair["stop_reason"], "per_label_budget_exhausted")
        self.assertEqual(repair["updates"], 2)

    def test_newest_passed_candidate_is_stable_base_not_blocked_tip(self):
        rows = [
            {
                "generation_id": "candidate:g10",
                "agent_id": "g10",
                "generation_number": 10,
                "created_ts": 10.0,
                "config_fingerprint": "same-config",
                "model_retained": 1,
                "offline_gate_json": json.dumps({"passed": False, "status": "failed"}),
            },
            {
                "generation_id": "candidate:g9",
                "agent_id": "g9",
                "generation_number": 9,
                "created_ts": 9.0,
                "config_fingerprint": "same-config",
                "model_retained": 1,
                "offline_gate_json": json.dumps({"passed": True, "status": "passed"}),
            },
            {
                "generation_id": "candidate:g8",
                "agent_id": "g8",
                "generation_number": 8,
                "created_ts": 8.0,
                "config_fingerprint": "same-config",
                "model_retained": 1,
                "offline_gate_json": json.dumps({"passed": True, "status": "passed"}),
            },
        ]
        selected = _select_stable_candidate(rows, "same-config")
        self.assertEqual(selected["agent_id"], "g9")

    def test_config_mismatch_or_pruned_candidate_cannot_be_correction_base(self):
        rows = [
            {
                "generation_id": "candidate:g9",
                "agent_id": "g9",
                "generation_number": 9,
                "config_fingerprint": "old-config",
                "model_retained": 1,
                "offline_gate_json": '{"passed":true}',
            },
            {
                "generation_id": "candidate:g8",
                "agent_id": "g8",
                "generation_number": 8,
                "config_fingerprint": "new-config",
                "model_retained": 0,
                "offline_gate_json": '{"passed":true}',
            },
        ]
        self.assertIsNone(_select_stable_candidate(rows, "new-config"))

    def test_final_runtime_installs_optimizer_before_stage1_residual_wrapper(self):
        source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        margin = source.index("manager = install_correct_margin_repair")
        residual = source.index("manager = install_correct_data_foundation")
        self.assertLess(margin, residual)
        self.assertIn('"optimizer": getattr(manager, "correct_optimizer_contract"', source)
        self.assertIn('"base": getattr(manager, "correct_base_contract"', source)

    def test_optimizer_stays_off_realtime_hot_path(self):
        engine = (SRC / "engine.py").read_text(encoding="utf-8")
        source = (SRC / "correct_margin_repair.py").read_text(encoding="utf-8")
        self.assertNotIn("correct_margin_repair", engine)
        self.assertNotIn("ActionIntent(", source)
        self.assertNotIn(".executor.", source)
        self.assertIn('"hot_path": False', source)

    def test_balancing_anchor_vectors_are_private_handoff_only(self):
        balanced = (SRC / "agent_candidate_balanced_correct.py").read_text(encoding="utf-8")
        stage2 = (SRC / "correct_margin_repair.py").read_text(encoding="utf-8")
        self.assertIn('"_stability_anchor_samples"', balanced)
        self.assertIn('report.pop("_stability_anchor_samples"', stage2)


if __name__ == "__main__":
    unittest.main()
