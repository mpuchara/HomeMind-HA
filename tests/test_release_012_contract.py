import json
import tempfile
import unittest
from pathlib import Path

from support import *
from context import ExplicitFeatureSchema
from context_schema_probation import ProbationIntegrationTests
from context_tournament_primary_protection import PrimaryProtectionTests
from context_tournament_quality import SensorQualityMathTests
from context_tournament_requalification import PromotionShadowRequalificationTests
from context_tournament_shadow import ContextTournamentShadowTests
from policy import MultiHorizonPolicy
from settings import APP_VERSION
from storage import Store
from teach_rl_rebenchmark import TeachRLRebenchmarkTests


def _run_existing(case, cls, method):
    suite = cls(method)
    result = unittest.TestResult()
    suite.run(result)
    if result.errors:
        raise AssertionError(result.errors[0][1])
    if result.failures:
        raise AssertionError(result.failures[0][1])
    case.assertEqual(result.testsRun, 1)


class Release012RequiredScenarios(unittest.TestCase):
    def test_negative_reward_reduces_predicted_action_calibration(self):
        from test_policy_rewards import PolicyTests
        _run_existing(self, PolicyTests, "test_negative_validation_reduces_action_confidence")

    def test_two_teach_labels_cannot_promote_sensor(self):
        from test_teaching_rl import TeachRLTests
        _run_existing(self, TeachRLTests, "test_supervised_scores_do_not_start_from_two_contrasting_labels")

    def test_insufficient_binary_balance_cannot_promote_sensor(self):
        from test_teaching_rl import TeachRLTests
        _run_existing(self, TeachRLTests, "test_binary_feature_evidence_requires_five_per_class")

    def test_manual_correction_still_immediately_updates_action(self):
        from test_history_teaching import HistoryTeachingTests
        _run_existing(self, HistoryTeachingTests, "test_one_wrong_decision_overrules_thousands_of_old_samples")

    def test_challenger_never_dispatches_service(self):
        from test_context_tournament import ContextTournamentTests
        _run_existing(self, ContextTournamentTests, "test_install_exposes_tournament_in_agent_runtime_without_dispatching_actions")

    def test_challenger_with_no_gain_is_rejected(self):
        from test_context_tournament_promotion import PromotionMathTests
        _run_existing(self, PromotionMathTests, "test_promotion_requires_every_gate")

    def test_challenger_with_stable_gain_is_promoted(self):
        from test_context_tournament_promotion import PromotionIntegrationTests
        _run_existing(self, PromotionIntegrationTests, "test_ready_challenger_is_promoted_without_exceeding_fast_limit")

    def test_primary_sensor_requires_larger_gain(self):
        _run_existing(self, PrimaryProtectionTests, "test_primary_sensor_is_not_replaced_by_six_point_gain")

    def test_new_schema_enters_shadow(self):
        _run_existing(self, PromotionShadowRequalificationTests, "test_only_promoted_control_agent_moves_to_shadow")

    def test_failed_new_schema_rolls_back(self):
        _run_existing(self, ProbationIntegrationTests, "test_underperforming_promoted_policy_restores_exact_previous_model")

    def test_old_schema_preserved_for_rollback(self):
        _run_existing(self, ProbationIntegrationTests, "test_underperforming_promoted_policy_restores_exact_previous_model")

    def test_sensor_unavailability_penalizes_challenger(self):
        _run_existing(self, SensorQualityMathTests, "test_flaky_sensor_cannot_displace_equally_relevant_stable_sensor")

    def test_prequential_sample_is_scored_before_learning(self):
        from test_prequential_replay import PrequentialReplayTests
        _run_existing(self, PrequentialReplayTests, "test_each_future_event_is_scored_before_learning")

    def test_teach_finetune_invalidates_control_qualification(self):
        _run_existing(self, TeachRLRebenchmarkTests, "test_final_teach_policy_invalidates_old_control_proof_but_keeps_shadow")

    def test_schema_change_does_not_affect_other_agents(self):
        _run_existing(self, PromotionShadowRequalificationTests, "test_only_promoted_control_agent_moves_to_shadow")

    def test_restart_preserves_tournament_state(self):
        _run_existing(self, ContextTournamentShadowTests, "test_shadow_model_persists_without_persisting_pending_prediction")


class Release012MigrationContract(unittest.TestCase):
    def test_release_keeps_linucb_and_feature_schema_persistence_compatible(self):
        self.assertEqual(APP_VERSION, "0.13.1")
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
                source="migration-test", detail={"counts": {"samples": 80, "correct": 74}},
            )
            raw_model = {
                "version": MultiHorizonPolicy.VERSION,
                "schema": {"version": ExplicitFeatureSchema.VERSION, "entities": []},
                "horizons": [1],
                "models": {},
            }
            store.save_model(agent["id"], raw_model)
            store.add_feedback(agent["id"], 1, 1.0, 1.0, "keep", {}, "test")
            before = store.get_agent_config(agent["id"])
            before_model = store.get_model(agent["id"])
            with store.conn() as c:
                before_feedback = c.execute("SELECT COUNT(*) FROM feedback WHERE agent_id=?", (agent["id"],)).fetchone()[0]

            from context_tournament import ContextTournament
            tournament = ContextTournament(store, SimpleNamespace(runtime={}, models={}, lock=threading.RLock()))
            self.assertIsNotNone(tournament)

            after = store.get_agent_config(agent["id"])
            after_model = store.get_model(agent["id"])
            with store.conn() as c:
                after_feedback = c.execute("SELECT COUNT(*) FROM feedback WHERE agent_id=?", (agent["id"],)).fetchone()[0]
            self.assertEqual(before["id"], after["id"])
            self.assertEqual(before["training_state"], after["training_state"])
            self.assertEqual(before_model, after_model)
            self.assertEqual(before_feedback, after_feedback)

    def test_tournament_migrations_remain_additive(self):
        root = Path(__file__).resolve().parents[1]
        files = [
            root / "adaptive_ai/src/context_tournament.py",
            root / "adaptive_ai/src/context_tournament_shadow.py",
            root / "adaptive_ai/src/context_tournament_metrics.py",
            root / "adaptive_ai/src/context_schema_history.py",
            root / "adaptive_ai/src/context_schema_probation.py",
        ]
        forbidden = ("DROP TABLE", "DELETE FROM agents", "DELETE FROM feedback", "DELETE FROM entity_history")
        for path in files:
            text = path.read_text(encoding="utf-8")
            for phrase in forbidden:
                self.assertNotIn(phrase, text, f"{phrase} found in {path.name}")


if __name__ == "__main__":
    unittest.main()
