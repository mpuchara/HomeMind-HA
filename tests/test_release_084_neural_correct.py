"""0.14.84 Stage-5 neural Manual Correct contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import math
import tempfile
import unittest

import candidate_neural_correct as neural_correct
import agent_candidate_lineage as lineage
from agent_candidates import ensure_tables as ensure_candidate_tables
from context import archived_state, context_scalar
from observation_space import ObservationMask, observation_schema_id
from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_correct import (
    incremental_correct_finetune,
    mixed_training_rows,
)
from policy_tiny_mlp_training import train_supervised
from storage import Store
from policy_tiny_mlp_training import build_training_artifact
from tiny_mlp_shadow import publish_training_artifact


FEATURE_IDS = ("feature:x", "feature:bias")


def observation(x):
    return {"feature_ids": list(FEATURE_IDS), "values": [float(x), 1.0]}


def rows(count=160, *, offset=0):
    out = []
    for idx in range(int(count)):
        logical = idx + int(offset)
        x = -1.0 + 2.0 * (logical % 80) / 79.0
        out.append({
            "observation": observation(x),
            "action_idx": 0 if x < 0.0 else 1,
            "weight": 1.0,
            "timestamp": float(logical),
        })
    return out


def trained_backend():
    model = TinyMLPBackend(
        actions=(0.0, 1.0),
        horizons=(1,),
        feature_ids=FEATURE_IDS,
        schema_id=observation_schema_id(),
        mask_id="stage5-unit-mask",
        hidden=(8, 4),
        init_seed=1484,
    )
    train_supervised(
        model,
        rows(240),
        max_epochs=16,
        batch_size=16,
        learning_rate=.03,
        gradient_clip=1.0,
        early_stop_patience=4,
    )
    return model


AGENT = {
    "id": "agent-stage5",
    "target_entity": "light.stage5",
    "target_property": "power",
    "min_value": 0.0,
    "max_value": 1.0,
    "deadband": .5,
}


class TinyMLPCorrectMathTests(unittest.TestCase):
    def test_mixture_keeps_explicit_labels_and_bounded_20_to_30_percent_weight(self):
        corrections = [
            {"observation": observation(-.1), "action_idx": 1, "weight": 1.0, "label_id": 1},
            {"observation": observation(.1), "action_idx": 0, "weight": 1.0, "label_id": 2},
        ]
        replay = rows(120)
        mixed, detail = mixed_training_rows(
            corrections, replay, correction_fraction=.25, max_samples=128
        )
        self.assertTrue(mixed)
        self.assertEqual(detail["correction_source_samples"], 2)
        self.assertGreaterEqual(detail["effective_correction_fraction"], .20)
        self.assertLessEqual(detail["effective_correction_fraction"], .30)
        self.assertLessEqual(len(mixed), 128)
        self.assertEqual(
            {row.get("label_id") for row in mixed if row.get("source") == "manual_correct"},
            {1, 2},
        )

    def test_incremental_correct_clones_parent_and_never_mutates_source(self):
        parent = trained_backend()
        parent_before = parent.serialize()
        corrections = [{
            # Keep this label close to the learned boundary; the test is about immutable
            # parent + incremental child semantics, not a brittle exact optimizer step.
            "observation": observation(.04),
            "action_idx": 1,
            "weight": 1.0,
            "label_id": 10,
            "timestamp": 1000.0,
        }]
        replay = [row for row in rows(120) if abs(row["observation"]["values"][0]) > .15]
        holdout = rows(80, offset=1000)
        child, report = incremental_correct_finetune(
            parent,
            agent=AGENT,
            correction_samples=corrections,
            replay_samples=replay,
            holdout_samples=holdout,
            nearby_observations=[observation(-.2), observation(.2)],
            max_epochs=4,
            learning_rate=.002,
            min_correction_fit=0.0,
            min_parent_agreement=0.0,
            min_nearby_agreement=0.0,
            max_regression_fraction=1.0,
            max_accuracy_regression=1.0,
            max_parent_relative_l2=2.0,
        )
        self.assertEqual(parent.serialize(), parent_before)
        self.assertNotEqual(child.model_revision, parent.model_revision)
        self.assertEqual(report["parent_model_checksum"], parent_before["model_checksum"])
        self.assertEqual(report["mode"], "incremental_supervised_finetune")
        self.assertFalse(report["online_reward_updates"])
        self.assertFalse(report["physical_authority"])

    def test_parent_distance_gate_blocks_overlarge_incremental_move(self):
        parent = trained_backend()
        corrections = [{
            "observation": observation(-.8),
            "action_idx": 1,
            "weight": 1.0,
            "label_id": 20,
            "timestamp": 2000.0,
        }]
        child, report = incremental_correct_finetune(
            parent,
            agent=AGENT,
            correction_samples=corrections,
            replay_samples=[],
            holdout_samples=rows(80, offset=2000),
            max_epochs=8,
            learning_rate=.03,
            min_correction_fit=0.0,
            min_parent_agreement=0.0,
            min_nearby_agreement=0.0,
            max_regression_fraction=1.0,
            max_accuracy_regression=1.0,
            max_parent_relative_l2=0.0,
        )
        self.assertIsNot(child, parent)
        self.assertFalse(report["passed"])
        self.assertIn("parent_distance_limit_exceeded", report["reasons"])

    def test_online_reward_update_remains_disabled_after_finetune(self):
        parent = trained_backend()
        child, _ = incremental_correct_finetune(
            parent,
            agent=AGENT,
            correction_samples=[{
                "observation": observation(.2),
                "action_idx": 1,
                "weight": 1.0,
                "timestamp": 1.0,
            }],
            replay_samples=[],
            holdout_samples=rows(24),
            max_epochs=1,
            min_correction_fit=0.0,
            min_parent_agreement=0.0,
            min_nearby_agreement=0.0,
            max_regression_fraction=1.0,
            max_accuracy_regression=1.0,
            max_parent_relative_l2=2.0,
        )
        with self.assertRaisesRegex(RuntimeError, "offline supervised trainer"):
            child.update(1, 1, observation(.2), 1.0)


class _NoHomeContext:
    options = {}

    def relevant_entities(self):
        return []

    def resolved_registry(self):
        return {}

    def area_for(self, entity_id):
        return None

    def prepare_home_reliability(self, home, area, ts):
        return None

    def augment_home_forecast(self, home, area, base, ts, **kwargs):
        return dict(base or {})


class HistoricalCorrectReconstructionTests(unittest.TestCase):
    def test_selected_point_uses_pre_timestamp_sensor_value_not_future_state(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage5-asof-") as root:
            store = Store(Path(root) / "stage5.db")
            selected_ts = 10_000.0
            store.archive_batch([
                ("sensor.stage5_context", selected_ts - 1.0, "1", {}, None, "test"),
                ("sensor.stage5_context", selected_ts + 1.0, "9", {}, None, "test"),
            ])
            feature_id = "entity:sensor.stage5_context:value"
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=(feature_id,),
                features=({
                    "id": feature_id,
                    "name": "Stage5 context · value",
                    "kind": "entity",
                    "entity_id": "sensor.stage5_context",
                    "area_id": None,
                    "descriptor": "value",
                    "lag_seconds": 0.0,
                },),
                selected_entities=("sensor.stage5_context",),
                global_feature_count=0,
                missing_feature_count=0,
            )
            manager = SimpleNamespace(
                store=store,
                engine=SimpleNamespace(context=_NoHomeContext()),
            )
            parent = dict(AGENT)
            parent["id"] = "candidate-parent"
            candidate = dict(AGENT)
            candidate["id"] = "candidate-child"

            with patch.dict(neural_correct.OPTIONS, {
                "tiny_mlp_correct_replay_samples": 0,
                "tiny_mlp_correct_holdout_samples": 0,
                "tiny_mlp_correct_nearby_seconds": "",
            }, clear=False):
                dataset = neural_correct._reconstruct_dataset(
                    manager,
                    parent,
                    candidate,
                    mask,
                    [{
                        "id": 77,
                        "sample_ts": selected_ts,
                        "desired": 1.0,
                        "previous_desired": 0.0,
                    }],
                    (0.0, 1.0),
                )

            self.assertEqual(len(dataset["corrections"]), 1)
            sample = dataset["corrections"][0]
            self.assertEqual(sample["label_id"], 77)
            self.assertEqual(sample["observation"]["timestamp"], selected_ts)
            self.assertEqual(sample["observation"]["missing_feature_count"], 0)

            with store.conn() as c:
                prior = dict(c.execute(
                    "SELECT * FROM entity_history WHERE entity_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                    ("sensor.stage5_context", selected_ts),
                ).fetchone())
                future = dict(c.execute(
                    "SELECT * FROM entity_history WHERE entity_id=? AND ts>? ORDER BY ts LIMIT 1",
                    ("sensor.stage5_context", selected_ts),
                ).fetchone())
            expected_prior = context_scalar(
                "sensor.stage5_context", archived_state(prior), parent
            )
            expected_future = context_scalar(
                "sensor.stage5_context", archived_state(future), parent
            )
            observed = float(sample["observation"]["values"][0])
            self.assertAlmostEqual(observed, float(expected_prior), places=7)
            self.assertNotAlmostEqual(observed, float(expected_future), places=4)

            audit = dataset["sample_audit"][0]
            self.assertTrue(audit["usable"])
            self.assertEqual(audit["sample_ts"], selected_ts)
            self.assertEqual(audit["desired"], 1.0)
            self.assertEqual(audit["original_decision"], 0.0)

    def test_missing_source_feature_is_audited_unusable_not_fabricated(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage5-missing-") as root:
            store = Store(Path(root) / "stage5.db")
            selected_ts = 20_000.0
            feature_id = "entity:sensor.missing:value"
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=(feature_id,),
                features=({
                    "id": feature_id,
                    "name": "Missing · value",
                    "kind": "entity",
                    "entity_id": "sensor.missing",
                    "area_id": None,
                    "descriptor": "value",
                    "lag_seconds": 0.0,
                },),
                selected_entities=("sensor.missing",),
                global_feature_count=0,
                missing_feature_count=0,
            )
            manager = SimpleNamespace(
                store=store,
                engine=SimpleNamespace(context=_NoHomeContext()),
            )
            with patch.dict(neural_correct.OPTIONS, {
                "tiny_mlp_correct_replay_samples": 0,
                "tiny_mlp_correct_holdout_samples": 0,
                "tiny_mlp_correct_nearby_seconds": "",
            }, clear=False):
                dataset = neural_correct._reconstruct_dataset(
                    manager,
                    dict(AGENT),
                    dict(AGENT),
                    mask,
                    [{
                        "id": 88,
                        "sample_ts": selected_ts,
                        "desired": 0.0,
                        "previous_desired": 1.0,
                    }],
                    (0.0, 1.0),
                )
            self.assertEqual(dataset["corrections"], [])
            self.assertEqual(len(dataset["sample_audit"]), 1)
            self.assertFalse(dataset["sample_audit"][0]["usable"])
            self.assertIn(
                "missing_source_features",
                dataset["sample_audit"][0]["unusable_reason"],
            )


class NeuralCorrectLineageSelectionTests(unittest.TestCase):
    def test_only_exact_candidate_generation_neural_artifact_is_eligible_parent(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage5-lineage-") as root:
            store = Store(Path(root) / "stage5.db")
            lineage.ensure_lineage_tables(store)
            live = store.create_agent({
                "name": "Root", "target_entity": "light.stage5", "target_property": "power",
                "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
                "exploration_step": 1, "input_entities": ["*"],
            })
            candidate = store.create_agent({
                "name": "Root · Candidate", "target_entity": "light.stage5", "target_property": "power",
                "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
                "exploration_step": 1, "input_entities": ["*"],
            })
            store.save_model(live["id"], {
                "version": 10, "model_revision": "root-ridge", "schema": {"version": 1},
            })
            store.save_model(candidate["id"], {
                "version": 10, "model_revision": "candidate-ridge", "schema": {"version": 1},
            })
            root_generation = lineage._ensure_root(store, live["id"], 0)
            candidate_generation = lineage._register_generation(
                store, live["id"], root_generation, candidate["id"], 1,
                "test", "comparing",
            )

            feature_id = "time:hour_sin"
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=(feature_id,),
                features=({
                    "id": feature_id, "name": feature_id, "kind": "global",
                    "entity_id": None, "area_id": None,
                    "descriptor": "hour_sin", "lag_seconds": 0.0,
                },),
                selected_entities=(),
                global_feature_count=1,
                missing_feature_count=0,
            )
            model = TinyMLPBackend(
                actions=(0.0, 1.0), horizons=(1,), feature_ids=(feature_id,),
                schema_id=mask.schema_id, mask_id=mask.mask_id, hidden=(4, 2), init_seed=1484,
            )
            model.trained = True
            model.training_samples = 20
            artifact = build_training_artifact(
                agent=candidate,
                policy=SimpleNamespace(
                    tournament_revision="candidate-ridge",
                    model_revision="candidate-ridge",
                ),
                mask=mask,
                backend=model,
                trainer={"trained": True},
                tournament={
                    "passed": True,
                    "selected_backend": "tiny_mlp",
                    "samples": 20,
                    "mlp_score": .9,
                },
            )
            publish_training_artifact(store, artifact)

            manager = SimpleNamespace(
                store=store,
                _row_by_candidate=lambda candidate_id: {
                    "candidate_id": str(candidate_id),
                    "offline_gate_json": '{"passed":true}',
                },
            )
            selected = neural_correct._source_neural_record(manager, candidate["id"])
            self.assertIsNotNone(selected)
            self.assertEqual(selected[0]["generation_id"], candidate_generation["generation_id"])
            self.assertEqual(selected[1]["selected_backend"], "tiny_mlp")

            # Root Live is deliberately never treated as a neural Correct parent in
            # Stage 5; neural Active authority is still disabled until controlled rollout.
            self.assertIsNone(neural_correct._source_neural_record(manager, live["id"]))


class NeuralCorrectBatchPersistenceTests(unittest.TestCase):
    def test_same_operation_batch_can_be_restarted_without_duplicate_or_unique_error(self):
        with tempfile.TemporaryDirectory(prefix="hm-stage5-batch-") as root:
            store = Store(Path(root) / "stage5.db")
            ensure_candidate_tables(store)
            lineage.ensure_lineage_tables(store)
            neural_correct._ensure_schema(store)
            parent = store.create_agent({
                "name": "Parent", "target_entity": "light.stage5", "target_property": "power",
                "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
                "exploration_step": 1, "input_entities": ["*"],
            })
            child = store.create_agent({
                "name": "Child", "target_entity": "light.stage5", "target_property": "power",
                "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
                "exploration_step": 1, "input_entities": ["*"],
            })
            root_generation = lineage._ensure_root(store, parent["id"], 0)
            child_generation = lineage._register_generation(
                store, parent["id"], root_generation, child["id"], 1,
                "test", "building",
            )
            feature_id = "time:hour_sin"
            mask = ObservationMask(
                schema_id=observation_schema_id(),
                mask_version=1,
                feature_ids=(feature_id,),
                features=({
                    "id": feature_id, "name": feature_id, "kind": "global",
                    "entity_id": None, "area_id": None,
                    "descriptor": "hour_sin", "lag_seconds": 0.0,
                },),
                selected_entities=(),
                global_feature_count=1,
                missing_feature_count=0,
            )
            model = TinyMLPBackend(
                actions=(0.0, 1.0), horizons=(1,), feature_ids=(feature_id,),
                schema_id=mask.schema_id, mask_id=mask.mask_id, hidden=(4, 2), init_seed=1484,
            )
            manager = SimpleNamespace(store=store)
            edge = {"parent_agent_id": parent["id"], "candidate_id": child["id"]}
            first = neural_correct._create_batch(
                manager, edge, root_generation, model, mask,
                batch_id="correct-operation-stable",
            )
            with store.lock, store.conn() as db:
                db.execute(
                    """INSERT INTO tiny_mlp_correct_samples
                       (batch_id,label_id,sample_ts,desired,usable)
                       VALUES(?,?,?,?,?)""",
                    (first, 1, 100.0, 1.0, 1),
                )
            second = neural_correct._create_batch(
                manager, edge, root_generation, model, mask,
                batch_id="correct-operation-stable",
            )
            self.assertEqual(first, second)
            with store.conn() as db:
                batches = db.execute(
                    "SELECT COUNT(*) FROM tiny_mlp_correct_batches WHERE batch_id=?",
                    (second,),
                ).fetchone()[0]
                samples = db.execute(
                    "SELECT COUNT(*) FROM tiny_mlp_correct_samples WHERE batch_id=?",
                    (second,),
                ).fetchone()[0]
            self.assertEqual(batches, 1)
            self.assertEqual(samples, 0)
            self.assertEqual(
                lineage._row(store, agent_id=child["id"])["generation_id"],
                child_generation["generation_id"],
            )


class Stage5SourceAndUiContracts(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def source(self, name):
        return (self.ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_ridge_correct_path_is_preserved_and_neural_layer_is_additive(self):
        source = self.source("candidate_neural_correct.py")
        self.assertIn("return original_start(row)", source)
        self.assertIn("_source_neural_record", source)
        self.assertIn("incremental_correct_finetune", source)
        self.assertIn("schema_upgrade_rebuild", source)
        self.assertIn("rebuild_reason", source)
        self.assertNotIn("from action", source.lower())
        self.assertNotIn("engine.executor", source)

    def test_candidate_ui_surfaces_correct_path_without_changing_chart_contract(self):
        candidate = self.source("static/candidate_ui.js")
        chart = self.source("static/agent_workflow_ui.js")
        self.assertIn("Correct path", candidate)
        self.assertIn("Rebuild reason", candidate)
        self.assertIn("Parent agreement after Correct", candidate)
        self.assertIn("Neural parent distance", candidate)
        self.assertIn("candidate_desired", chart)
        self.assertIn("parent_desired", chart)
        self.assertIn("Correct points", chart)


if __name__ == "__main__":
    unittest.main()
