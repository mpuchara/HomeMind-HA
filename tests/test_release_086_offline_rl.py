"""0.14.86 Stage-7 conservative Offline-RL Candidate contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import unittest

import test_agent_workflow_actions as workflow_fixture

from automatic_correct_rewards import AutomaticRewardJournal
from candidate_neural_shadow import install as install_candidate_neural_shadow
from candidate_offline_rl import (
    install as install_candidate_offline_rl,
    workflow_offline_rl,
)
from observation_space import ObservationMask, observation_schema_id
from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_offline_rl import (
    offline_rl_gate,
    train_conservative_offline_rl,
)
from policy_tiny_mlp_training import (
    build_training_artifact,
    train_supervised,
)
from tiny_mlp_shadow import (
    load_training_record,
    publish_training_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
FEATURE_IDS = ("feature:x", "feature:bias")


def observation(x):
    return {
        "feature_ids": list(FEATURE_IDS),
        "values": [float(x), 1.0],
    }


def supervised_rows(n=240):
    rows = []
    for idx in range(int(n)):
        x = -1.0 + 2.0 * (idx % 80) / 79.0
        rows.append({
            "observation": observation(x),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(idx),
        })
    return rows


def trained_parent(seed=1486):
    model = TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=FEATURE_IDS,
        schema_id=observation_schema_id(),
        mask_id="stage7-mask",
        hidden=(8, 4),
        init_seed=seed,
    )
    train_supervised(
        model,
        supervised_rows(),
        max_epochs=14,
        batch_size=16,
        learning_rate=.03,
        gradient_clip=1.0,
        early_stop_patience=4,
    )
    return model


def reward_rows(n=64):
    rows = []
    for idx in range(int(n)):
        x = -0.95 + 1.9 * (idx % 32) / 31.0
        action = idx % 2
        # Both actions have repeated support. Reward says action 0 is preferable on the
        # negative side and action 1 on the positive side.
        good = (x < 0.0 and action == 0) or (x >= 0.0 and action == 1)
        rows.append({
            "observation": observation(x),
            "action_idx": action,
            "action_value": float(action),
            "reward": .8 if good else -.8,
            "weight": .95,
            "timestamp": float(idx),
        })
    return rows


class OfflineRLMathTests(unittest.TestCase):
    def test_parent_is_immutable_and_child_stays_inside_hard_distance_limit(self):
        parent = trained_parent()
        before = parent.serialize()
        child, report = train_conservative_offline_rl(
            parent,
            reward_rows(48),
            max_epochs=5,
            learning_rate=.004,
            kl_beta=2.0,
            max_parent_relative_l2=.025,
            min_action_support=4,
        )
        self.assertTrue(report["trained"])
        self.assertEqual(parent.serialize(), before)
        self.assertIsNot(child, parent)
        self.assertLessEqual(
            float(report["parent_distance"]["relative_l2"]),
            .0250001,
        )
        self.assertFalse(report["online_exploration"])
        self.assertFalse(report["online_reward_updates"])
        self.assertFalse(report["physical_authority"])

    def test_manual_correct_anchor_is_stronger_than_conflicting_reward(self):
        parent = trained_parent()
        anchor = [{
            "label_id": 77,
            "observation": observation(-.8),
            "action_idx": 0,
            "weight": 1.0,
        }]
        rows = reward_rows(48)
        # Add reward pressure in the opposite direction at the exact human-labelled
        # context. The explicit label must remain authoritative.
        rows.extend({
            "observation": observation(-.8),
            "action_idx": 1,
            "action_value": 1.0,
            "reward": 1.0,
            "weight": 1.0,
            "timestamp": 1000.0 + idx,
        } for idx in range(8))
        child, report = train_conservative_offline_rl(
            parent,
            rows,
            manual_samples=anchor,
            manual_weight=10.0,
            max_epochs=6,
            learning_rate=.003,
            max_parent_relative_l2=.08,
        )
        self.assertEqual(
            report["manual_fit_candidate"]["score"], 1.0
        )
        self.assertEqual(
            child.predict(observation(-.8))[0]["index"], 0
        )

    def test_gate_blocks_unsupported_action_extrapolation(self):
        parent = trained_parent()
        rows = [
            {
                "observation": observation(-.8 + idx * .01),
                "action_idx": 0,
                "action_value": 0.0,
                "reward": .8,
                "weight": 1.0,
                "timestamp": float(idx),
            }
            for idx in range(32)
        ]
        child, _report = train_conservative_offline_rl(
            parent,
            rows[:24],
            max_epochs=3,
            min_action_support=4,
        )
        gate = offline_rl_gate(
            parent,
            child,
            train_rows=rows[:24],
            holdout_rows=rows[24:],
            min_total_samples=24,
            min_holdout_samples=8,
            min_supported_actions=2,
            min_action_support=4,
            min_reward_gain=-1.0,
            min_parent_agreement=0.0,
            max_mean_tv=1.0,
            max_max_tv=1.0,
            max_parent_relative_l2=1.0,
            max_unsupported_probability_lift=1.0,
            max_regression_fraction=1.0,
            max_unseen_context_rate=1.0,
        )
        self.assertFalse(gate["passed"])
        self.assertIn(
            "insufficient_action_support", gate["reasons"]
        )

    def test_gate_records_required_offline_comparison_metrics(self):
        parent = trained_parent()
        rows = reward_rows(64)
        train, holdout = rows[:48], rows[48:]
        child, _report = train_conservative_offline_rl(
            parent,
            train,
            max_epochs=4,
            max_parent_relative_l2=.08,
        )
        gate = offline_rl_gate(
            parent,
            child,
            train_rows=train,
            holdout_rows=holdout,
            min_total_samples=24,
            min_holdout_samples=8,
            min_action_support=4,
            min_reward_gain=-1.0,
            min_parent_agreement=0.0,
            max_mean_tv=1.0,
            max_max_tv=1.0,
            max_parent_relative_l2=1.0,
            max_unsupported_probability_lift=1.0,
            max_regression_fraction=1.0,
            max_unseen_context_rate=1.0,
        )
        child_metrics = gate["comparison"]["offline_rl_candidate"]
        for key in (
            "reward_improvement_estimate",
            "parent_action_agreement",
            "action_drift_mean_tv",
            "unseen_context_rate",
            "q_proxy_calibration",
            "regression_count",
        ):
            self.assertIn(key, child_metrics)
        self.assertFalse(gate["behavior_propensity_known"])
        self.assertIn(
            "not unbiased", gate["evaluation_claim"]
        )


class _HeavySlot:
    owner = None

    def acquire(self, owner):
        self.owner = owner
        return True

    def release(self, owner):
        if self.owner == owner:
            self.owner = None
        return True


class OfflineRLCandidateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_fixture.AgentWorkflowActionTests()
        self.fixture.setUp()
        self.manager = install_candidate_offline_rl(
            self.fixture.manager
        )
        self.store = self.fixture.store
        self.root = self.fixture.root

        self.mask = ObservationMask(
            schema_id=observation_schema_id(),
            mask_version=1,
            feature_ids=FEATURE_IDS,
            features=(
                {
                    "id": FEATURE_IDS[0],
                    "name": "x",
                    "kind": "global",
                    "entity_id": None,
                    "area_id": None,
                    "descriptor": "hour_sin",
                    "lag_seconds": 0.0,
                },
                {
                    "id": FEATURE_IDS[1],
                    "name": "bias",
                    "kind": "global",
                    "entity_id": None,
                    "area_id": None,
                    "descriptor": "hour_cos",
                    "lag_seconds": 0.0,
                },
            ),
            selected_entities=(),
            global_feature_count=2,
            missing_feature_count=0,
        )
        self.parent_backend = trained_parent()
        # Use the exact mask identity persisted for this lifecycle fixture.
        self.parent_backend.schema_id = self.mask.schema_id
        self.parent_backend.mask_id = self.mask.mask_id
        policy = self.fixture.engine.policy(self.root)
        artifact = build_training_artifact(
            agent=self.root,
            policy=policy,
            mask=self.mask,
            backend=self.parent_backend,
            trainer={"trained": True, "contract": "fixture_supervised"},
            tournament={
                "contract": "fixture",
                "passed": True,
                "selected_backend": "tiny_mlp",
                "samples": 80,
                "mlp_score": .9,
            },
        )
        publish_training_artifact(self.store, artifact)
        self.parent_checksum = load_training_record(
            self.store, self.root["id"]
        )["model"]["model_checksum"]

        journal = AutomaticRewardJournal(self.store)
        now = 10_000.0
        for idx, row in enumerate(reward_rows(40)):
            key = f"decision:stage7-{idx}"
            payload = {
                "resolution_key": key,
                "agent_id": self.root["id"],
                "generation_id": f"root:{self.root['id']}",
                "decision_id": f"stage7-{idx}",
                "trial_id": None,
                "action_index": int(row["action_idx"]),
                "action_value": float(row["action_value"]),
                "action_ts": now + idx,
                "observation_start": now + idx,
                "observation_end": now + idx + 90.0,
                "target_entity": self.root["target_entity"],
                "target_property": self.root["target_property"],
                "area_id": "test",
                "observation_schema_id": self.mask.schema_id,
                "observation_mask_id": self.mask.mask_id,
                "observation": row["observation"],
                "observation_mask": self.mask.export(),
                "prediction_inputs": [],
                "background_dependencies": [],
                "outcome_sources": {},
                "reward_sources": ["fixture"],
                "metadata": {},
            }
            _saved, inserted = journal.start(payload)
            self.assertTrue(inserted)
            journal.resolve(
                key,
                status="trusted",
                outcome="fixture",
                proposed_reward=row["reward"],
                trusted_reward=row["reward"],
                confidence=.95,
                attribution_reason="fixture trusted outcome",
                source_entity_id="binary_sensor.fixture",
                source_reliability=.95,
                reward_sources=["binary_sensor.fixture"],
            )

    def tearDown(self):
        self.fixture.manager = self.manager
        self.fixture.tearDown()

    def test_trusted_stage6_rows_create_gated_neural_child_without_parent_mutation(self):
        options = {
            "offline_rl_min_trusted_samples": 24,
            "offline_rl_min_holdout_samples": 8,
            "offline_rl_holdout_fraction": .25,
            "offline_rl_min_supported_actions": 2,
            "offline_rl_min_action_support": 4,
            "offline_rl_max_train_samples": 64,
            "offline_rl_max_epochs": 3,
            "offline_rl_batch_size": 8,
            "offline_rl_learning_rate": .002,
            "offline_rl_min_effective_sample_size": 1.0,
            "offline_rl_min_reward_gain": -1.0,
            "offline_rl_min_parent_agreement": 0.0,
            "offline_rl_max_mean_tv": 1.0,
            "offline_rl_max_max_tv": 1.0,
            "offline_rl_max_parent_relative_l2": 1.0,
            "offline_rl_max_unsupported_probability_lift": 1.0,
            "offline_rl_max_regression_fraction": 1.0,
            "offline_rl_max_unseen_context_rate": 1.0,
        }
        import candidate_offline_rl as module
        with patch.dict(module.OPTIONS, options, clear=False),              patch.object(module, "TRAINING_BUDGET", _NoBudget()),              patch.object(module, "HEAVY_JOBS", _HeavySlot()):
            result = workflow_offline_rl(
                self.manager, self.root["id"]
            )
            row = self.manager._candidate_row(self.root["id"])
            self.assertEqual(row["reason"], "offline_rl")
            self.assertTrue(self.manager._start_build(row))

        after_parent = load_training_record(
            self.store, self.root["id"]
        )
        self.assertEqual(
            after_parent["model"]["model_checksum"],
            self.parent_checksum,
        )
        child_generation = self.manager.lineage_status(
            result["child_generation_id"]
        )
        child_id = child_generation["agent_id"]
        child_record = load_training_record(
            self.store, child_id
        )
        self.assertIsNotNone(child_record)
        self.assertEqual(
            child_record["selected_backend"], "tiny_mlp"
        )
        self.assertEqual(
            child_record["tournament"]["contract"],
            "offline_rl_parent_vs_candidate_logged_reward_v1",
        )
        status = self.manager.status(self.root["id"])
        self.assertEqual(status["state"], "comparing")
        self.assertTrue(status["offline_gate"]["passed"])
        self.assertFalse(
            status["offline_rl_online_exploration"]
        )
        self.assertEqual(
            status["offline_rl"]["status"], "passed"
        )
        self.assertEqual(
            status["offline_rl"]["compatible_total"], 40
        )

    def test_unknown_stage6_rows_are_not_training_input(self):
        journal = AutomaticRewardJournal(self.store)
        payload = {
            "resolution_key": "decision:unknown-extra",
            "agent_id": self.root["id"],
            "generation_id": f"root:{self.root['id']}",
            "decision_id": "unknown-extra",
            "trial_id": None,
            "action_index": 0,
            "action_value": 0.0,
            "action_ts": 20_000.0,
            "observation_start": 20_000.0,
            "observation_end": 20_090.0,
            "target_entity": self.root["target_entity"],
            "target_property": self.root["target_property"],
            "area_id": "test",
            "observation_schema_id": self.mask.schema_id,
            "observation_mask_id": self.mask.mask_id,
            "observation": observation(-.5),
            "observation_mask": self.mask.export(),
            "prediction_inputs": [],
            "background_dependencies": [],
            "outcome_sources": {},
            "reward_sources": [],
            "metadata": {},
        }
        journal.start(payload)
        journal.resolve(
            "decision:unknown-extra",
            status="unknown",
            outcome="no_override_observed",
            proposed_reward=.15,
            confidence=.2,
            unknown_reason="silence",
        )
        import candidate_offline_rl as module
        from agent_workflow_actions import _resolve_generation
        generation, agent = _resolve_generation(
            self.manager, self.root["id"]
        )
        info = module.readiness(
            self.manager, generation, agent
        )
        self.assertEqual(info["trusted_total"], 40)
        self.assertEqual(info["compatible_total"], 40)


class Stage7ShadowAndSourceContracts(unittest.TestCase):
    def test_offline_rl_selected_artifact_uses_existing_neural_shadow_and_stays_non_promotable(self):
        calls = []
        record = {
            "model": {"trained": True},
            "mask": {"mask_id": "stage7"},
            "selected_backend": "tiny_mlp",
            "tournament": {
                "contract": "offline_rl_parent_vs_candidate_logged_reward_v1",
                "passed": True,
                "selected_backend": "tiny_mlp",
            },
        }

        class Service:
            def persisted_record(self, agent_id):
                return dict(record)

            def predict_persisted(self, agent, policy, state_map, temporal, **kwargs):
                calls.append(kwargs.get("require_selected"))
                return {
                    "backend": SimpleNamespace(
                        model_revision="rl-child",
                        mask_id="stage7-mask",
                    ),
                    "chosen": {
                        "value": 1.0,
                        "confidence_kind": "softmax_uncalibrated",
                    },
                    "confidence": .82,
                }

        class Store:
            def get_agent_config(self, agent_id):
                return {"id": str(agent_id), "target_property": "power"}

            def get_model(self, agent_id):
                return {"policy_backend": "diagonal_linucb"}

        manager = SimpleNamespace(
            engine=SimpleNamespace(
                tiny_mlp_shadow=Service(),
                models={"candidate-rl": SimpleNamespace()},
                temporal_history=object(),
            ),
            store=Store(),
            status=lambda parent_id: {"candidate_id": "candidate-rl"},
            list_status=lambda *a, **k: [{"candidate_id": "candidate-rl"}],
            promote=lambda parent_id, *a, **k: {"promoted": parent_id},
            promote_custom=lambda parent_id, *a, **k: {"promoted": parent_id},
            _row_by_candidate=lambda candidate_id: {
                "candidate_id": candidate_id,
                "offline_gate_json": json.dumps({"passed": True}),
            },
        )
        manager = install_candidate_neural_shadow(manager)
        result = manager.candidate_backend_predictor(
            {"generation_id": "g-rl", "agent_id": "candidate-rl"},
            {},
            123.0,
        )
        self.assertEqual(result["policy_backend"], "tiny_mlp")
        self.assertEqual(calls, [True])
        with self.assertRaisesRegex(ValueError, "Shadow-only"):
            manager.promote("candidate-rl")

    def test_stage7_sources_contain_no_online_exploration_or_physical_dispatch(self):
        trainer = (
            ROOT / "adaptive_ai" / "src"
            / "policy_tiny_mlp_offline_rl.py"
        ).read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "adaptive_ai" / "src"
            / "candidate_offline_rl.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("policy.update(", lifecycle)
        self.assertNotIn("executor.submit", lifecycle)
        self.assertNotIn("engine.executor", lifecycle)
        self.assertNotIn("service(", lifecycle.lower())
        self.assertIn('"online_exploration": False', trainer)
        self.assertIn(
            "Candidate -> offline gate -> Shadow A/B",
            (
                ROOT / "adaptive_ai" / "src"
                / "runtime_composition.py"
            ).read_text(encoding="utf-8"),
        )

    def test_manual_correct_chart_and_action_remain_available(self):
        workflow = (
            ROOT / "adaptive_ai" / "src" / "static"
            / "agent_workflow_ui.js"
        ).read_text(encoding="utf-8")
        candidate = (
            ROOT / "adaptive_ai" / "src" / "static"
            / "candidate_ui.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Correct points", workflow)
        self.assertIn("Apply Correct · create child Candidate", workflow)
        self.assertIn('data-wf="offline-rl"', workflow)
        self.assertIn('data-wf="correct"', candidate)
        self.assertIn('data-wf="offline-rl"', candidate)


if __name__ == "__main__":
    unittest.main()
