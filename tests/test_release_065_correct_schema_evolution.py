"""0.14.65 Stage-3 residual-targeted schema evolution regression tests."""

import json
import unittest
from unittest.mock import patch

from support import ROOT

import correct_data_foundation as foundation
from context import ExplicitFeatureSchema
from correct_schema_evolution import (
    _schema_limit,
    _schema_offline_gate,
    migrate_model_schema,
    rank_residual_context,
)


SRC = ROOT / "adaptive_ai" / "src"
FIXTURE = ROOT / "tests" / "fixtures" / "correct_46_nonconflicting.json"


def _fast_candidate():
    return {
        "target_entity": "switch.fixture_light",
        "target_property": "power",
        "input_entities": ["*"],
    }


def _fixture_rows():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = []
    for index, label in enumerate(fixture["labels"]):
        broad = label["broad_context"]
        snapshot = {
            "sensor.fixture_moving_energy": {
                "v": broad["sensor.fixture_moving_energy"],
                "recent_delta": 0.0,
                "role": foundation.ROLE_LOCAL,
            },
            "binary_sensor.fixture_boundary": {
                "v": broad["binary_sensor.fixture_boundary"],
                "recent_delta": 0.0,
                "role": foundation.ROLE_BOUNDARY,
            },
            "sensor.fixture_humidity": {
                "v": broad["sensor.fixture_humidity"],
                "recent_delta": 0.0,
                "role": foundation.ROLE_RELIABILITY,
            },
        }
        rows.append({
            "supervision_event_id": label["supervision_event_id"],
            "sample_ts": label["sample_ts"],
            "desired_idx": int(label["desired"]),
            # Exercise residual weighting on both classes, not merely the ON class.
            "unresolved": (index % 3) == 0 or int(label["desired"]) == 1,
            "snapshot": snapshot,
        })
    return rows


class CorrectSchemaEvolutionTests(unittest.TestCase):
    def test_46_point_fixture_promotes_moving_energy_not_humidity(self):
        rows = _fixture_rows()
        ranking, rejected = rank_residual_context(
            rows,
            ["sensor.fixture_stationary_energy"],
            _fast_candidate(),
        )
        self.assertTrue(ranking)
        self.assertEqual(ranking[0]["entity_id"], "sensor.fixture_moving_energy")
        self.assertEqual(ranking[0]["role"], foundation.ROLE_LOCAL)
        self.assertGreaterEqual(ranking[0]["cv_balanced_accuracy"], 0.62)
        rejected_by_id = {row["entity_id"]: row for row in rejected}
        self.assertIn("sensor.fixture_humidity", rejected_by_id)
        self.assertEqual(
            rejected_by_id["sensor.fixture_humidity"]["reason"],
            "semantic_role_not_eligible_for_automatic_schema_promotion",
        )

    def test_fast_target_reliability_context_cannot_be_presence_schema_even_if_perfect(self):
        rows = []
        for index in range(24):
            desired = index % 2
            rows.append({
                "supervision_event_id": f"r-{index}",
                "sample_ts": float(index),
                "desired_idx": desired,
                "unresolved": True,
                "snapshot": {
                    "sensor.bathroom_humidity": {
                        "v": 90.0 if desired else 40.0,
                        "recent_delta": 10.0 if desired else -10.0,
                        "role": foundation.ROLE_RELIABILITY,
                    }
                },
            })
        ranking, rejected = rank_residual_context(rows, [], _fast_candidate())
        self.assertEqual(ranking, [])
        self.assertEqual(rejected[0]["entity_id"], "sensor.bathroom_humidity")
        self.assertEqual(rejected[0]["role"], foundation.ROLE_RELIABILITY)

    def test_nonfast_target_may_use_reliability_context_without_calling_it_presence(self):
        rows = []
        for index in range(24):
            desired = index % 2
            rows.append({
                "supervision_event_id": f"hvac-{index}",
                "sample_ts": float(index),
                "desired_idx": desired,
                "unresolved": True,
                "snapshot": {
                    "sensor.zone_temperature": {
                        "v": 27.0 if desired else 20.0,
                        "recent_delta": 1.0 if desired else -1.0,
                        "role": foundation.ROLE_RELIABILITY,
                    }
                },
            })
        candidate = {
            "target_entity": "climate.fixture",
            "target_property": "temperature",
            "input_entities": ["*"],
        }
        ranking, _ = rank_residual_context(rows, [], candidate)
        self.assertTrue(ranking)
        self.assertEqual(ranking[0]["role"], foundation.ROLE_RELIABILITY)

    def test_requested_input_filter_is_authoritative(self):
        rows = _fixture_rows()
        candidate = _fast_candidate()
        candidate["input_entities"] = ["binary_sensor.fixture_boundary"]
        ranking, _ = rank_residual_context(
            rows, ["sensor.fixture_stationary_energy"], candidate
        )
        self.assertNotIn(
            "sensor.fixture_moving_energy",
            [row["entity_id"] for row in ranking],
        )

    def test_schema_migration_remaps_old_interaction_weight_by_label(self):
        dims = 64
        old_schema = ExplicitFeatureSchema(
            dims, ["sensor.a", "sensor.b"]
        )
        old_labels = {
            labels[0]: index
            for index, labels in old_schema.labels().items()
            if labels
        }
        a = [[1.0] * dims for _ in range(2)]
        b = [[0.0] * dims for _ in range(2)]
        ctx_sum = [[0.0] * dims for _ in range(2)]
        ctx_sq = [[0.0] * dims for _ in range(2)]
        for label, index in old_labels.items():
            b[0][index] = 1000.0 + index
            a[0][index] = 10.0 + index
        for index in range(dims - 7, dims):
            b[0][index] = 2000.0 + index
        raw = {
            "version": 10,
            "dims": dims,
            "actions": [0.0, 1.0],
            "horizons": [1],
            "schema": old_schema.export(),
            "selection_meta": {},
            "heads": {
                "1": {
                    "version": 5,
                    "dims": dims,
                    "actions": [0.0, 1.0],
                    "a": a,
                    "b": b,
                    "counts": [40.0, 20.0],
                    "reward_sums": [30.0, 15.0],
                    "ctx_sum": ctx_sum,
                    "ctx_sq": ctx_sq,
                    "total_updates": 60.0,
                    "last_decay_ts": 100.0,
                    "validation_weight": 25.0,
                    "validation_correct_weight": 20.0,
                    "validation_samples": 25.0,
                    "validation_pred_weight": [10.0, 15.0],
                    "validation_pred_correct_weight": [8.0, 12.0],
                }
            },
        }
        migrated = migrate_model_schema(
            raw,
            ["sensor.a", "sensor.b", "sensor.c"],
            evolution_meta={"test": True},
        )
        new_schema = ExplicitFeatureSchema.from_export(migrated["schema"], dims)
        new_labels = {
            labels[0]: index
            for index, labels in new_schema.labels().items()
            if labels
        }
        old_interaction = "interaction:sensor.a×sensor.b"
        self.assertIn(old_interaction, old_labels)
        self.assertIn(old_interaction, new_labels)
        self.assertNotEqual(
            old_labels[old_interaction],
            new_labels[old_interaction],
            "adding an entity should move the interaction slot in this schema",
        )
        head = migrated["heads"]["1"]
        self.assertEqual(
            head["b"][0][new_labels[old_interaction]],
            1000.0 + old_labels[old_interaction],
        )
        self.assertEqual(
            head["a"][0][new_labels["sensor.a:value"]],
            10.0 + old_labels["sensor.a:value"],
        )
        self.assertEqual(head["b"][0][new_labels["sensor.c:value"]], 0.0)
        self.assertEqual(head["a"][0][new_labels["sensor.c:value"]], 1.0)
        for index in range(dims - 7, dims):
            self.assertEqual(head["b"][0][index], 2000.0 + index)
        self.assertEqual(head["validation_samples"], 0.0)
        self.assertEqual(head["validation_pred_weight"], [0.0, 0.0])

    def test_fast_schema_growth_is_bounded_by_normal_fast_capacity(self):
        class Policy:
            dims = 128
        self.assertEqual(_schema_limit(Policy(), _fast_candidate()), 8)

    def test_runtime_order_is_margin_then_schema_then_residual_diagnostics(self):
        source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        margin = source.index("manager = install_correct_margin_repair")
        schema = source.index("manager = install_correct_schema_evolution")
        residual = source.index("manager = install_correct_data_foundation")
        self.assertLess(margin, schema)
        self.assertLess(schema, residual)
        self.assertIn(
            '"schema_evolution": getattr(manager, "correct_schema_evolution_contract"',
            source,
        )

    def test_missing_context_is_an_explicit_offline_gate_status(self):
        report = {
            "schema_evolution_status": "missing_context",
            "schema_evolution_reason": "no_semantically_eligible_cross_validated_context_separates_residual_supervision",
        }
        with patch(
            "correct_schema_evolution._BASE_MARGIN_GATE",
            return_value={"passed": True, "status": "passed", "reasons": []},
        ):
            gate = _schema_offline_gate({}, {}, {}, report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "missing_context")
        self.assertTrue(gate["missing_context"])
        self.assertIn(
            "Correct residuals require additional discriminative context",
            gate["reasons"],
        )

    def test_candidate_ui_surfaces_missing_context_and_schema_enrichment(self):
        source = (SRC / "static" / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("<b>Missing context</b>", source)
        self.assertIn("<b>Correct schema enriched:</b>", source)
        self.assertIn("c.schema_evolution_selected", source)
        self.assertNotIn("fetch(", source[source.index("const schemaSelected"):source.index("const customEligible")])

    def test_schema_evolution_stays_off_realtime_hot_path(self):
        engine = (SRC / "engine.py").read_text(encoding="utf-8")
        source = (SRC / "correct_schema_evolution.py").read_text(encoding="utf-8")
        self.assertNotIn("correct_schema_evolution", engine)
        self.assertNotIn("ActionIntent(", source)
        self.assertNotIn(".executor.", source)
        self.assertIn('"hot_path": False', source)

    def test_pre_01463_historical_fallback_is_bounded_per_entity_not_per_label(self):
        source = (SRC / "correct_schema_evolution.py").read_text(encoding="utf-8")
        self.assertIn("for entity_id in entity_ids:", source)
        self.assertIn("bisect_right(times, sample_ts)", source)
        self.assertNotIn(
            "foundation._historical_states(core.STORE, entity_ids, sample_ts)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
