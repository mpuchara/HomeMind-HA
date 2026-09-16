import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_candidate_balanced_correct import _balanced_fine_tune, _balanced_offline_gate
from teaching_rl import fingerprint


class FakeRLTeaching:
    def __init__(self, candidate, count=4):
        fp = fingerprint(candidate)
        self.rows = [
            {
                "id": i + 1,
                "agent_id": candidate["id"],
                "created_ts": 1010.0 + i,
                "sample_ts": 1000.0 + i,
                "desired": 1.0,
                "previous_desired": 0.0,
                "fingerprint": fp,
                "undone_ts": None,
            }
            for i in range(count)
        ]

    def labels(self, agent_id):
        return [dict(row) for row in self.rows if str(row["agent_id"]) == str(agent_id)]

    def _label_context(self, agent, policy, sample_ts):
        # Error-only feedback: parent currently says OFF here, user marks Desired=ON.
        return {0: 1.0, 1: 1.0, 10: (float(sample_ts) % 7.0) / 7.0}


class RecordingBinaryPolicy:
    """Tiny policy that records update mass while preserving explicit class contexts."""

    def __init__(self, store, agent):
        self.store = store
        self.agent = agent
        raw = store.get_model(agent["id"]) or {}
        self.actions = [0.0, 1.0]
        self.horizons = [1]
        self.model_revision = raw.get("model_revision") or "parent-revision"
        self.corrected = bool(raw.get("corrected", False))
        self.updates = []

    def predict(self, features):
        if float(features.get(1, 0.0)) > 0.5:
            value = 1.0 if self.corrected else 0.0
        elif float(features.get(2, 0.0)) > 0.5:
            value = 0.0
        else:
            value = 1.0
        return {"value": value}, 0.9, [], 1, 1.0, 0.0

    def update(self, horizon, action_idx, features, reward):
        self.updates.append((int(action_idx), float(reward), dict(features)))
        if float(reward) > 0 and int(action_idx) == 1 and float(features.get(1, 0.0)) > 0.5:
            self.corrected = True

    def serialize(self):
        raw = dict(self.store.get_model(self.agent["id"]) or {})
        raw.update({
            "model_revision": self.model_revision,
            "corrected": bool(self.corrected),
            "schema": raw.get("schema") or {"version": 11, "entities": ["binary_sensor.room_presence"]},
        })
        return raw


class BalancedCorrectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "balanced-correct.db")
        self.candidate = self.store.create_agent({
            "name": "Candidate",
            "target_entity": "light.room",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": 1,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.room_presence"],
        })
        self.store.save_model(self.candidate["id"], {
            "model_revision": "parent-revision",
            "schema": {"version": 11, "entities": ["binary_sensor.room_presence"]},
            "corrected": False,
        })
        self.candidate = self.store.get_agent_config(self.candidate["id"])
        self.rl = FakeRLTeaching(self.candidate, count=4)
        self.policy = RecordingBinaryPolicy(self.store, self.candidate)
        self.engine = SimpleNamespace(
            rl_teaching=self.rl,
            models={},
            policy=lambda agent: self.policy,
        )
        self.manager = SimpleNamespace(store=self.store, engine=self.engine)
        self._add_history()

    def tearDown(self):
        self.temp.cleanup()

    def _add_history(self):
        # Six correct OFF and six correct ON observations.  The four one-sided ON error
        # labels should therefore be paired with four nearest correct OFF stability anchors.
        for i in range(6):
            self._experience(2000.0 + i, 0, {0: 1.0, 2: 1.0, 10: i / 7.0})
            self._experience(3000.0 + i, 1, {0: 1.0, 3: 1.0, 10: i / 7.0})

    def _experience(self, ts, action_idx, features):
        self.store.archive_upsert(
            "light.room", ts, "on" if action_idx else "off", {}, None, "test"
        )
        with self.store.conn() as c:
            history_id = c.execute(
                "SELECT id FROM entity_history WHERE entity_id='light.room' AND ts=?", (ts,)
            ).fetchone()[0]
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO historical_experiences
                   (agent_id,target_history_id,created_at,action_index,action_value,reward,dwell_seconds,features_json,user_id)
                   VALUES(?,?,?,?,?,?,?,?,NULL)""",
                (
                    self.candidate["id"], history_id, "2026-09-16T00:00:00+00:00",
                    int(action_idx), float(action_idx), 1.0, 30.0,
                    json.dumps({str(k): v for k, v in features.items()}),
                ),
            )

    def test_one_sided_on_errors_are_balanced_with_correct_off_history(self):
        report = _balanced_fine_tune(self.manager, self.candidate)

        self.assertEqual(report["balance_mode"], "error_only_labels_plus_context_matched_historical_anchors")
        self.assertEqual(report["correction_class_counts"], {"0": 0, "1": 4})
        self.assertEqual(report["stability_anchor_class_counts"], {"0": 4, "1": 0})
        self.assertEqual(report["stability_anchor_shortfall"], {"0": 0, "1": 0})
        self.assertEqual(report["correction_rounds"], 1)
        self.assertEqual(report["teach_fit_before_count"], 0)
        self.assertEqual(report["teach_fit_after_count"], 4)
        self.assertEqual(report["stability_anchor_total"], 4)
        self.assertEqual(report["stability_anchor_retained"], 4)
        self.assertEqual(report["positive_updates_by_class"]["1"], 4)
        self.assertEqual(report["anchor_updates_by_class"]["0"], 4)
        self.assertEqual(report["negative_updates"], 4)
        self.assertEqual(report["balanced_support_predicted_class_coverage"], 2)

        positive_on = sum(1 for action, reward, _ in self.policy.updates if action == 1 and reward > 0)
        positive_off = sum(1 for action, reward, _ in self.policy.updates if action == 0 and reward > 0)
        self.assertEqual(positive_on, positive_off)

    def test_offline_gate_rejects_a_correction_that_does_not_fix_any_marked_error(self):
        stats = {
            "samples": 40,
            "score": .90,
            "balanced": True,
            "actual_class_coverage": 2,
            "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        report = {
            "mode": "conservative_snapshot_finetune",
            "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
            "teach_fit_before": 0.0,
            "teach_fit_after": 0.0,
            "teach_fit_before_count": 0,
            "teach_fit_after_count": 0,
            "teach_fit_total": 4,
            "correction_class_counts": {"0": 0, "1": 4},
            "stability_anchor_class_counts": {"0": 4, "1": 0},
            "stability_anchor_shortfall": {"0": 0, "1": 0},
            "stability_anchor_total": 4,
            "stability_anchor_retained": 4,
            "class_balance_required": True,
        }
        gate = _balanced_offline_gate({}, stats, stats, report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "failed")
        self.assertIn("Correct did not improve any marked error", gate["reasons"])

    def test_missing_opposite_class_anchors_becomes_insufficient_evidence(self):
        stats = {
            "samples": 40,
            "score": .90,
            "balanced": True,
            "actual_class_coverage": 2,
            "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        report = {
            "mode": "conservative_snapshot_finetune",
            "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
            "teach_fit_before": 0.0,
            "teach_fit_after": 1.0,
            "teach_fit_before_count": 0,
            "teach_fit_after_count": 4,
            "teach_fit_total": 4,
            "correction_class_counts": {"0": 0, "1": 4},
            "stability_anchor_class_counts": {"0": 1, "1": 0},
            "stability_anchor_shortfall": {"0": 3, "1": 0},
            "stability_anchor_total": 1,
            "stability_anchor_retained": 1,
            "class_balance_required": True,
        }
        gate = _balanced_offline_gate({}, stats, stats, report)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "insufficient_evidence")
        self.assertIn("insufficient opposite-class historical stability anchors", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
