"""0.14.85 Stage-6 trusted Automatic Correct outcome/reward contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import tempfile
import time
import unittest

from automatic_correct_rewards import (
    AutomaticRewardJournal,
    install as install_automatic_correct,
)
from provenance_runtime import install as install_provenance
from storage import Store
from support import state
import test_executor as executor_fixture


ROOT = Path(__file__).resolve().parents[1]


def reward_payload(**overrides):
    base = {
        "resolution_key": "decision:d-1",
        "agent_id": "agent-1",
        "generation_id": "generation:g1",
        "decision_id": "d-1",
        "trial_id": None,
        "action_index": 1,
        "action_value": 1.0,
        "action_ts": 1000.0,
        "observation_start": 1000.0,
        "observation_end": 1090.0,
        "target_entity": "light.kitchen",
        "target_property": "power",
        "area_id": "kitchen",
        "observation_schema_id": "obs-v1:test",
        "observation_mask_id": "mask-test",
        "observation": {
            "timestamp": 1000.0,
            "feature_ids": ["time:hour_sin"],
            "values": [0.5],
        },
        "observation_mask": {
            "schema_id": "obs-v1:test",
            "mask_id": "mask-test",
            "feature_ids": ["time:hour_sin"],
        },
        "prediction_inputs": ["binary_sensor.pir"],
        "background_dependencies": ["sensor.helper"],
        "outcome_sources": {},
        "reward_sources": [],
        "metadata": {"test": True},
    }
    base.update(overrides)
    return base


class AutomaticRewardJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hm-auto-reward-")
        self.path = Path(self.temp.name) / "reward.db"
        self.store = Store(self.path)
        self.now = 1200.0
        self.journal = AutomaticRewardJournal(
            self.store, clock=lambda: self.now
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_decision_dedup_returns_existing_row(self):
        first, inserted = self.journal.start(reward_payload())
        self.assertTrue(inserted)
        second, inserted = self.journal.start(
            reward_payload(resolution_key="alternate-key")
        )
        self.assertFalse(inserted)
        self.assertEqual(second["resolution_key"], first["resolution_key"])
        with self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM automatic_reward_experiences"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_one_resolution_per_trial_even_with_different_decision(self):
        first, inserted = self.journal.start(
            reward_payload(
                resolution_key="trial:t-1",
                trial_id="t-1",
                decision_id="d-1",
            )
        )
        self.assertTrue(inserted)
        second, inserted = self.journal.start(
            reward_payload(
                resolution_key="trial:t-1-retry",
                trial_id="t-1",
                decision_id="d-2",
            )
        )
        self.assertFalse(inserted)
        self.assertEqual(second["resolution_key"], first["resolution_key"])
        self.assertEqual(second["trial_id"], "t-1")

    def test_resolution_is_exactly_once(self):
        self.journal.start(reward_payload())
        first, changed = self.journal.resolve(
            "decision:d-1",
            status="trusted",
            outcome="explicit_user_reversal",
            proposed_reward=-1.0,
            trusted_reward=-1.0,
            confidence=1.0,
            attribution_reason="exact target user reversal",
            source_reliability=1.0,
        )
        self.assertTrue(changed)
        second, changed = self.journal.resolve(
            "decision:d-1",
            status="trusted",
            outcome="different",
            proposed_reward=1.0,
            trusted_reward=1.0,
            confidence=1.0,
        )
        self.assertFalse(changed)
        self.assertEqual(second["outcome"], first["outcome"])
        self.assertEqual(second["trusted_reward"], -1.0)

    def test_restart_marks_unresolved_window_unknown_not_positive(self):
        self.journal.start(reward_payload())
        self.now = 1300.0
        fresh = AutomaticRewardJournal(
            Store(self.path), clock=lambda: self.now
        )
        row = fresh.get("decision:d-1")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["outcome"], "unknown")
        self.assertIsNone(row["trusted_reward"])
        self.assertIn("restart", row["unknown_reason"])

    def test_summary_is_ram_backed_after_warmup(self):
        self.journal.start(reward_payload())
        self.journal.resolve(
            "decision:d-1",
            status="unknown",
            outcome="no_override_observed",
            proposed_reward=.15,
            confidence=.2,
            unknown_reason="silence is not evidence",
        )
        original_conn = self.journal.store.conn
        self.journal.store.conn = Mock(
            side_effect=AssertionError(
                "periodic Automatic Correct summary must not query SQLite"
            )
        )
        try:
            summary = self.journal.summary("agent-1")
        finally:
            self.journal.store.conn = original_conn
        self.assertEqual(summary["counts"]["unknown"], 1)
        self.assertFalse(summary["learning_enabled"])
        self.assertEqual(
            summary["latest"]["outcome"], "no_override_observed"
        )


class AutomaticCorrectIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_fixture.ExecutorTests()
        self.fixture.setUp()
        self.core = SimpleNamespace(
            STORE=self.fixture.store,
            ENGINE=self.fixture.e,
        )
        install_provenance(self.core)
        self.e = self.fixture.e
        self.a = self.fixture.a
        self.e.entity_registry = {
            self.a["target_entity"]: {"area_id": "kitchen"},
            "binary_sensor.pir": {"area_id": "kitchen"},
            "binary_sensor.hall": {"area_id": "hall"},
            "binary_sensor.helper": {"area_id": "kitchen"},
        }
        self.e.state_map["binary_sensor.pir"] = state(
            "binary_sensor.pir", "off", device_class="motion"
        )
        self.e.state_map["binary_sensor.hall"] = state(
            "binary_sensor.hall", "off", device_class="motion"
        )
        self.e.state_map["binary_sensor.helper"] = state(
            "binary_sensor.helper", "off"
        )
        self.e.context.configure(
            self.e.state_map, entities=self.e.entity_registry
        )
        self.service = install_automatic_correct(self.core)

    def tearDown(self):
        self.fixture.tearDown()

    def _seed(self, **overrides):
        payload = reward_payload(
            agent_id=self.a["id"],
            target_entity=self.a["target_entity"],
            **overrides,
        )
        row, inserted = self.service.journal.start(payload)
        self.assertTrue(inserted)
        return row

    def _event(self, eid, value, *, event_time, area, origin="unknown",
               user_id=None, device_class="motion"):
        st = state(eid, value, device_class=device_class)
        st["context"] = {
            "id": f"ctx-{eid}-{event_time}",
            "parent_id": None,
            "user_id": user_id,
        }
        self.e.state_map[eid] = st
        self.e.entity_registry[eid] = {"area_id": area}
        event_id, _ = self.e.provenance.record_event(
            eid, st, event_time=event_time,
            received_time=event_time + .01,
            source="ha_state_changed", origin=origin,
        )
        self.e._provenance_latest_events[eid] = (
            float(event_time), event_id, origin
        )
        return event_id

    def test_accepted_action_captures_stage2_observation_and_weak_reward_does_not_update_policy(self):
        intent = self.fixture.intent()
        result = self.e.executor.submit(intent, {0: 1.0}, 1)
        self.assertEqual(result["status"], "ACCEPTED")

        rows = self.service.recent(self.a["id"])
        self.assertEqual(len(rows), 1)
        captured = rows[0]
        self.assertEqual(captured["status"], "pending")
        self.assertEqual(captured["decision_id"], intent.intent_id)
        self.assertTrue(captured["observation_schema_id"])
        self.assertTrue(captured["observation_mask_id"])
        self.assertEqual(
            captured["observation"]["timestamp"],
            captured["action_ts"],
        )
        self.assertEqual(
            captured["observation"]["mask_id"],
            captured["observation_mask_id"],
        )

        model_before = self.e.policy(self.a).serialize()
        rt = self.e.runtime[self.a["id"]]
        pending = rt["pending"]
        changed = self.e._reward_pending(
            self.a, rt, .15, "weak acceptance after settling"
        )
        model_after = self.e.policy(self.a).serialize()

        self.assertTrue(changed)
        self.assertEqual(model_after, model_before)
        self.assertIsNone(rt.get("pending"))
        resolved = self.service.journal.get(
            "decision:" + intent.intent_id
        )
        self.assertEqual(resolved["status"], "unknown")
        self.assertEqual(resolved["proposed_reward"], .15)
        self.assertIsNone(resolved["trusted_reward"])
        self.assertEqual(
            resolved["outcome"], "no_override_observed"
        )
        self.assertIn(
            "insufficient evidence", resolved["attribution_reason"]
        )
        self.assertEqual(
            pending["decision_id"], intent.intent_id
        )

    def test_exact_target_user_reversal_is_trusted_negative_without_reward_learning(self):
        row = self._seed(
            resolution_key="decision:user-reversal",
            decision_id="user-reversal",
        )
        self._event(
            self.a["target_entity"], "off",
            event_time=row["action_ts"] + 1,
            area="kitchen", origin="user", user_id="human",
            device_class=None,
        )
        rt = {
            "pending": {
                "decision_id": "user-reversal",
                "action_index": 1,
                "action_value": 1.0,
                "features": {0: 1.0},
                "started_ts": row["action_ts"],
            },
            "reward_components_pending": {
                "manual_correction": -1.0
            },
        }
        model_before = self.e.policy(self.a).serialize()
        self.e._reward_pending(
            self.a, rt, -1.0, "manual correction",
            user_id="human",
        )
        model_after = self.e.policy(self.a).serialize()
        resolved = self.service.journal.get(
            "decision:user-reversal"
        )
        self.assertEqual(model_after, model_before)
        self.assertEqual(resolved["status"], "trusted")
        self.assertEqual(resolved["trusted_reward"], -1.0)
        self.assertEqual(
            resolved["outcome"], "explicit_user_reversal"
        )
        self.assertEqual(
            resolved["source_entity_id"],
            self.a["target_entity"],
        )
        self.assertEqual(resolved["source_origin"], "user")
        self.assertEqual(resolved["source_reliability"], 1.0)

    def test_same_area_presence_inside_window_is_trusted(self):
        row = self._seed(
            resolution_key="decision:presence",
            decision_id="presence",
            prediction_inputs=["binary_sensor.pir"],
        )
        event_id = self._event(
            "binary_sensor.pir", "on",
            event_time=row["action_ts"] + 2,
            area="kitchen",
        )
        rt = {
            "pending": {
                "decision_id": "presence",
                "action_index": 1,
                "action_value": 1.0,
                "features": {},
                "started_ts": row["action_ts"],
            },
            "reward_components_pending": {
                "confirmed_anticipation": .45
            },
        }
        resolved = self.service.resolve_runtime(
            self.a, rt, .6, "anticipation outcome"
        )
        self.assertEqual(resolved["status"], "trusted")
        self.assertEqual(resolved["trusted_reward"], .6)
        self.assertEqual(
            resolved["source_entity_id"], "binary_sensor.pir"
        )
        self.assertEqual(resolved["source_event_id"], event_id)
        self.assertGreaterEqual(resolved["confidence"], .9)

    def test_other_room_presence_is_rejected_not_rewarded(self):
        row = self._seed(
            resolution_key="decision:hall",
            decision_id="hall",
            prediction_inputs=["binary_sensor.hall"],
        )
        self._event(
            "binary_sensor.hall", "on",
            event_time=row["action_ts"] + 2,
            area="hall",
        )
        rt = {
            "pending": {
                "decision_id": "hall",
                "action_index": 1,
                "action_value": 1.0,
                "features": {},
                "started_ts": row["action_ts"],
            }
        }
        resolved = self.service.resolve_runtime(
            self.a, rt, .6, "anticipation outcome"
        )
        self.assertEqual(resolved["status"], "rejected")
        self.assertIsNone(resolved["trusted_reward"])
        self.assertIn("another area", resolved["unknown_reason"])

    def test_unrelated_binary_helper_cannot_confirm_presence(self):
        row = self._seed(
            resolution_key="decision:helper",
            decision_id="helper",
            prediction_inputs=["binary_sensor.helper"],
        )
        self._event(
            "binary_sensor.helper", "on",
            event_time=row["action_ts"] + 2,
            area="kitchen", device_class=None,
        )
        rt = {
            "pending": {
                "decision_id": "helper",
                "action_index": 1,
                "action_value": 1.0,
                "features": {},
                "started_ts": row["action_ts"],
            }
        }
        resolved = self.service.resolve_runtime(
            self.a, rt, .6, "anticipation outcome"
        )
        self.assertEqual(resolved["status"], "unknown")
        self.assertIsNone(resolved["trusted_reward"])

    def test_missing_target_area_stays_unknown(self):
        row = self._seed(
            resolution_key="decision:no-area",
            decision_id="no-area",
            area_id=None,
            prediction_inputs=["binary_sensor.pir"],
        )
        self._event(
            "binary_sensor.pir", "on",
            event_time=row["action_ts"] + 2,
            area="kitchen",
        )
        rt = {
            "pending": {
                "decision_id": "no-area",
                "action_index": 1,
                "action_value": 1.0,
                "features": {},
                "started_ts": row["action_ts"],
            }
        }
        resolved = self.service.resolve_runtime(
            self.a, rt, .6, "anticipation outcome"
        )
        self.assertEqual(resolved["status"], "unknown")
        self.assertIsNone(resolved["trusted_reward"])
        self.assertIn("area", resolved["unknown_reason"])

    def test_experiment_uses_only_explicit_outcome_sources(self):
        row = self._seed(
            resolution_key="trial:trial-strict",
            trial_id="trial-strict",
            decision_id="experiment-decision",
            prediction_inputs=[
                "binary_sensor.pir",
                "binary_sensor.hall",
            ],
            outcome_sources={
                "binary_sensor.pir": {
                    "area_id": "kitchen",
                    "role": "binary",
                }
            },
        )
        self._event(
            "binary_sensor.hall", "on",
            event_time=row["action_ts"] + 2,
            area="hall",
        )
        trial = {
            "trial_id": "trial-strict",
            "decision_id": "experiment-decision",
            "action_at": row["action_ts"],
            "value": 1.0,
            "focus": "presence",
            "kind": "probe",
            "outcome_sources": {
                "binary_sensor.pir": {
                    "area_id": "kitchen",
                    "role": "binary",
                }
            },
        }
        resolved = self.service.resolve_experiment(
            self.a["id"], trial, .6,
            "presence confirmed after decision",
            {"reward": .6},
        )
        self.assertEqual(resolved["status"], "rejected")
        self.assertIsNone(resolved["trusted_reward"])

    def test_runtime_summary_exposes_stage6_diagnostics(self):
        self._seed(
            resolution_key="decision:diag",
            decision_id="diag",
        )
        runtime = self.e.runtime_for(self.a)
        diag = runtime["automatic_correct"]
        self.assertFalse(diag["learning_enabled"])
        self.assertEqual(diag["counts"]["pending"], 1)
        self.assertEqual(
            diag["latest"]["decision_id"], "diag"
        )


class Stage6SourceAndUiContracts(unittest.TestCase):
    def test_new_stage6_module_has_no_policy_update_or_physical_dispatch(self):
        source = (
            ROOT / "adaptive_ai" / "src"
            / "automatic_correct_rewards.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("policy.update(", source)
        self.assertNotIn("save_model(", source)
        self.assertNotIn("executor._service(", source)
        self.assertIn('"policy_updates": False', source)
        self.assertIn('"physical_authority": False', source)

    def test_manual_correct_chart_contract_stays_present(self):
        workflow = (
            ROOT / "adaptive_ai" / "src" / "static"
            / "agent_workflow_ui.js"
        ).read_text(encoding="utf-8")
        runtime_http = (
            ROOT / "adaptive_ai" / "src" / "runtime_http.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Correct points", workflow)
        self.assertIn("candidate_desired", workflow)
        self.assertIn("parent_desired", workflow)
        self.assertIn("manual-correction", runtime_http)
        self.assertIn("teaching", runtime_http)

    def test_agent_ui_displays_required_automatic_correct_diagnostics(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        for label in (
            "Automatic Correct:",
            "Automatic Correct confidence/source:",
            "Automatic Correct attribution:",
            "Automatic Correct trial/action:",
            "Automatic Correct unknown/rejected:",
            "Automatic Correct buffer:",
            "reward learning OFF",
        ):
            self.assertIn(label, source)


if __name__ == "__main__":
    unittest.main()
