"""Explicit acceptance contract for HomeMind 0.12 Sensor Tournament.

The project already has focused unit/integration coverage for each subsystem.  These tests
bind the exact release-acceptance scenario names to those production-backed regressions so
a future refactor cannot accidentally make the 0.12 checklist disappear while individual
module tests are renamed.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from context import ExplicitFeatureSchema
from context_tournament import ContextTournament
from policy import MultiHorizonPolicy
from settings import APP_VERSION
from storage import Store
from teaching_rl import RLTeaching, fingerprint

from test_context_schema_probation import ProbationIntegrationTests
from test_context_tournament_primary_protection import PrimaryProtectionTests
from test_context_tournament_promotion import PromotionIntegrationTests, PromotionMathTests
from test_context_tournament_quality import SensorQualityMathTests
from test_context_tournament_requalification import PromotionShadowRequalificationTests
from test_context_tournament_shadow import ContextTournamentShadowTests
from test_desired_teaching import DesiredTeachingTests
from test_policy_rewards import PolicyRewardTests
from test_prequential_replay import PrequentialReplayTests
from test_teach_rl_rebenchmark import TeachRLRebenchmarkTests
from test_teaching_rl import TeachRLTests


ROOT = Path(__file__).resolve().parents[1]


def _run_existing(owner, case_class, method_name):
    """Run an existing full unittest scenario with its normal setUp/tearDown/cleanups."""
    result = unittest.TestResult()
    case_class(methodName=method_name).run(result)
    if result.failures or result.errors:
        details = []
        for _, trace in result.failures + result.errors:
            details.append(trace)
        owner.fail("\n".join(details))


class Release012RequiredScenarios(unittest.TestCase):
    def test_negative_reward_reduces_predicted_action_calibration(self):
        _run_existing(
            self, PolicyRewardTests,
            "test_negative_validation_reduces_action_confidence",
        )

    def test_two_teach_labels_cannot_promote_sensor(self):
        _run_existing(
            self, TeachRLTests,
            "test_insufficient_teach_evidence_preserves_existing_schema",
        )

    def test_insufficient_binary_balance_cannot_promote_sensor(self):
        _run_existing(
            self, TeachRLTests,
            "test_binary_feature_evidence_requires_five_per_class",
        )

    def test_manual_correction_still_immediately_updates_action(self):
        # Physical correction still dispatches immediately; the companion explicit label
        # scenario proves the same event immediately changes the base policy decision.
        _run_existing(
            self, DesiredTeachingTests,
            "test_current_correction_still_toggles_physical_current_in_shadow",
        )
        _run_existing(
            self, DesiredTeachingTests,
            "test_binary_teaching_toggles_desired_not_opposite_current",
        )

    def test_challenger_never_dispatches_service(self):
        _run_existing(
            self, ContextTournamentShadowTests,
            "test_shadow_runs_without_rebuilding_policy_or_touching_schema",
        )
        _run_existing(
            self, ContextTournamentShadowTests,
            "test_shadow_status_is_diagnostics_only",
        )

    def test_challenger_with_no_gain_is_rejected(self):
        _run_existing(
            self, PromotionMathTests,
            "test_promotion_requires_every_gate",
        )

    def test_challenger_with_stable_gain_is_promoted(self):
        _run_existing(
            self, PromotionIntegrationTests,
            "test_ready_challenger_is_promoted_without_exceeding_fast_limit",
        )

    def test_primary_sensor_requires_larger_gain(self):
        _run_existing(
            self, PrimaryProtectionTests,
            "test_primary_sensor_is_not_replaced_by_six_point_gain",
        )

    def test_new_schema_enters_shadow(self):
        _run_existing(
            self, PromotionShadowRequalificationTests,
            "test_only_promoted_control_agent_moves_to_shadow",
        )

    def test_failed_new_schema_rolls_back(self):
        _run_existing(
            self, ProbationIntegrationTests,
            "test_underperforming_promoted_policy_restores_exact_previous_model",
        )

    def test_old_schema_preserved_for_rollback(self):
        # The integration scenario asserts both previous_schema and the exact serialized
        # previous model revision before exercising the automatic rollback.
        _run_existing(
            self, ProbationIntegrationTests,
            "test_underperforming_promoted_policy_restores_exact_previous_model",
        )

    def test_sensor_unavailability_penalizes_challenger(self):
        _run_existing(
            self, SensorQualityMathTests,
            "test_flaky_sensor_cannot_displace_equally_relevant_stable_sensor",
        )

    def test_prequential_sample_is_scored_before_learning(self):
        _run_existing(
            self, PrequentialReplayTests,
            "test_each_future_event_is_scored_before_it_is_learned",
        )

    def test_teach_finetune_invalidates_control_qualification(self):
        _run_existing(
            self, TeachRLRebenchmarkTests,
            "test_final_teach_policy_invalidates_old_control_proof_but_keeps_shadow",
        )

    def test_schema_change_does_not_affect_other_agents(self):
        _run_existing(
            self, PromotionShadowRequalificationTests,
            "test_only_promoted_control_agent_moves_to_shadow",
        )

    def test_restart_preserves_tournament_state(self):
        _run_existing(
            self, ContextTournamentShadowTests,
            "test_shadow_model_persists_without_persisting_pending_prediction",
        )


class Release012MigrationContract(unittest.TestCase):
    def test_release_keeps_linucb_and_feature_schema_persistence_compatible(self):
        self.assertEqual(APP_VERSION, "0.12.0")
        self.assertEqual(MultiHorizonPolicy.VERSION, 10)
        self.assertEqual(ExplicitFeatureSchema.VERSION, 11)

    def test_additive_tournament_tables_preserve_existing_user_data(self):
        with tempfile.TemporaryDirectory(prefix="hm-012-migration-") as root:
            store = Store(Path(root) / "adaptive_ai.db")
            agent = store.create_agent({
                "name": "Migration fixture",
                "target_entity": "light.fixture",
                "target_property": "power",
                "min_value": 0,
                "max_value": 1,
                "deadband": 0.5,
                "confidence_threshold": 0.78,
                "action_interval": 1,
                "exploration_step": 1,
                "input_entities": ["*"],
                "mode": "shadow",
            })
            store.set_training_state(
                agent["id"], "qualified", score=0.91, samples=80,
                source="pre-012-benchmark",
                detail={"counts": {"samples": 80, "correct": 73}},
            )
            raw_model = {
                "version": 10,
                "schema": {"version": 11, "dims": 128,
                           "entities": ["binary_sensor.fixture"]},
                "selection_meta": {},
                "dims": 128,
                "actions": [0.0, 1.0],
                "horizons": [1],
                "heads": {},
                "model_revision": "pre-012",
            }
            store.save_model(agent["id"], raw_model)
            store.archive_batch([
                ("binary_sensor.fixture", time.time() - 10, "on", {"device_class": "occupancy"}, None, "pre-012")
            ])
            store.add_feedback(
                agent["id"], 0, 0.0, -1.0, "pre-012 feedback", {0: 1.0}, "user"
            )

            teaching = RLTeaching(store, SimpleNamespace())
            with store.lock, store.conn() as c:
                c.execute(
                    "INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) "
                    "VALUES(?,?,?,?,?,?)",
                    (agent["id"], time.time(), time.time() - 5, 1.0, 0.0, fingerprint(agent)),
                )

            before_agent = store.get_agent_config(agent["id"])
            before_model = store.get_model(agent["id"])
            before_feedback = list(store.list_feedback(agent["id"], 20))
            before_archive = dict(store.archive_stats())
            before_labels = teaching.labels(agent["id"])

            # 0.12 tournament storage is created additively on the same SQLite database.
            fake_engine = SimpleNamespace(
                state_map={}, entity_registry={}, context_relevance={}, models={},
                runtime={}, lock=threading.RLock(),
            )
            ContextTournament(store, fake_engine)

            after_agent = store.get_agent_config(agent["id"])
            after_model = store.get_model(agent["id"])
            after_feedback = list(store.list_feedback(agent["id"], 20))
            after_archive = dict(store.archive_stats())
            after_labels = teaching.labels(agent["id"])

            self.assertEqual(after_agent["id"], before_agent["id"])
            self.assertEqual(after_agent["benchmark_score"], before_agent["benchmark_score"])
            self.assertEqual(after_agent["benchmark_samples"], before_agent["benchmark_samples"])
            self.assertEqual(after_agent["benchmark_source"], before_agent["benchmark_source"])
            self.assertEqual(after_model["model_revision"], before_model["model_revision"])
            self.assertEqual(after_model["schema"], before_model["schema"])
            self.assertEqual(after_feedback, before_feedback)
            self.assertEqual(after_archive["n"], before_archive["n"])
            self.assertEqual(after_labels, before_labels)

            with store.conn() as c:
                names = {
                    row[0] for row in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            self.assertIn("context_tournament_state", names)
            self.assertIn("context_tournament_shadow", names)
            self.assertIn("teaching_rl_labels", names)

    def test_tournament_migrations_remain_additive(self):
        files = (
            "adaptive_ai/src/context_tournament.py",
            "adaptive_ai/src/context_tournament_promotion.py",
            "adaptive_ai/src/context_tournament_quality.py",
            "adaptive_ai/src/context_schema_history.py",
            "adaptive_ai/src/context_schema_probation.py",
            "adaptive_ai/src/context_tournament_events.py",
        )
        for rel in files:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("DROP TABLE", text.upper(), rel)
            if "CREATE TABLE" in text.upper():
                self.assertIn("CREATE TABLE IF NOT EXISTS", text.upper(), rel)


if __name__ == "__main__":
    unittest.main()
