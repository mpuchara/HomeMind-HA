from pathlib import Path
import unittest

import support  # noqa: F401
from context import ExplicitFeatureSchema
from correct_schema_evolution import _stable_model_schema_compatible
from policy import MultiHorizonPolicy

ROOT = Path(__file__).resolve().parents[1]


class CandidateLegacySchemaRecoveryTests(unittest.TestCase):
    def compatible_model(self):
        dims = 128
        return {
            "version": MultiHorizonPolicy.VERSION,
            "dims": dims,
            "schema": ExplicitFeatureSchema(
                dims, ["binary_sensor.room_presence"]
            ).export(),
            "actions": [0.0, 1.0],
            "horizons": [1],
            "heads": {},
        }

    def test_current_policy_and_schema_contract_is_compatible(self):
        self.assertTrue(_stable_model_schema_compatible(self.compatible_model()))

    def test_missing_schema_dims_is_not_silently_reinterpreted(self):
        raw = self.compatible_model()
        raw["schema"] = {
            "version": ExplicitFeatureSchema.VERSION,
            "entities": ["binary_sensor.room_presence"],
        }
        self.assertFalse(_stable_model_schema_compatible(raw))

    def test_old_policy_version_requires_isolated_rebuild(self):
        raw = self.compatible_model()
        raw["version"] = MultiHorizonPolicy.VERSION - 1
        self.assertFalse(_stable_model_schema_compatible(raw))

    def test_correct_routes_incompatible_base_to_schema_upgrade_rebuild(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "correct_schema_evolution.py"
        ).read_text(encoding="utf-8")
        self.assertIn('_SCHEMA_UPGRADE_REASON = "schema_upgrade_rebuild"', source)
        self.assertIn("not _stable_model_schema_compatible(raw_model)", source)
        self.assertIn("manager._start_build = start_build", source)
        self.assertIn("agent_candidate_schema_upgrade_rebuild", source)

    def test_previous_01470_failure_is_requeued_without_discarding_candidate(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "agent_candidates.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Stable correction base schema is incompatible", source)
        self.assertIn("reason='schema_upgrade_rebuild'", source)
        self.assertIn("state='queued'", source)
        self.assertNotIn(
            "DELETE FROM agent_candidates\n                   WHERE state='failed'",
            source,
        )

    def test_schema_upgrade_rebuild_gets_full_rebuild_offline_gate(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "agent_candidate_conservative_correct.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"schema_upgrade_rebuild"', source)
        self.assertIn("reason not in _FULL_REBUILD_REASONS", source)


if __name__ == "__main__":
    unittest.main()
