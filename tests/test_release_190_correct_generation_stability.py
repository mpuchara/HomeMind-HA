"""Corrections must preserve fitted parent decisions across generations."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import observation_contract as observation
import policy
import correct_classifier_finetune as incremental
import agent_candidate_balanced_correct as balanced
from test_release_189_correct_parent_contract import current_model
import test_candidate_conservative_correct as fixture


class CorrectGenerationStabilityTests(unittest.TestCase):
    def setUp(self):
        binding = observation.install_training_contract()
        self.addCleanup(binding["restore"])
        self.agent = support.agent(input_entities=["sensor.stationary", "sensor.moving"])
        self.policy = policy.MultiHorizonPolicy(self.agent, {}, {}, set(), model=current_model())
        clf = self.policy.heads[1].state_classifier
        for i in range(40):
            for action in (0, 1):
                clf.add(action, {5: .76 if action else 0., 17: (i % 5) / 5., 19: 0.}, .1)
        clf.fit()

    @staticmethod
    def sample(value, action, age=.75):
        return {"features": {0: 1., 5: value, 17: .9, 19: age}, "desired_idx": action}

    def test_two_generations_repair_weaker_presence_and_keep_recent_off(self):
        clf = self.policy.heads[1].state_classifier
        basis = copy.deepcopy((clf.center, clf.scale))
        penalties = copy.deepcopy(self.policy.heads[1].rejection_b)
        protected = [self.sample(0., 0), self.sample(.76, 1)]
        with patch.object(type(clf), "fit", side_effect=AssertionError("Correct must not refit from zero")):
            for value in (.3, .12):
                correction = self.sample(value, 1)
                self.assertEqual(incremental._margin(self.policy, correction)[0], 0)
                accepted, steps = incremental.optimize(self.policy, [correction], protected)
                self.assertTrue(accepted)
                self.assertLessEqual(steps, incremental.MAX_STEPS)
                protected.append(correction)
                for row in protected:
                    self.assertEqual(incremental._margin(self.policy, row)[0], row["desired_idx"])
        self.assertEqual((clf.center, clf.scale), basis)
        self.assertEqual(self.policy.heads[1].rejection_b, penalties)
        self.assertEqual(clf.weights[0], 0.)
        self.assertTrue(all(row[0] == 0. for row in clf.hinge_weights))
        reloaded = policy.MultiHorizonPolicy(self.agent, {}, {}, set(), model=self.policy.serialize())
        for row in protected:
            self.assertEqual(incremental._margin(reloaded, row)[0], row["desired_idx"])

    def test_impossible_correction_restores_complete_classifier_snapshot(self):
        original = copy.deepcopy(self.policy.heads[1].state_classifier.export())
        accepted, steps = incremental.optimize(self.policy, [self.sample(0., 1)], [self.sample(0., 0)])
        self.assertFalse(accepted)
        self.assertEqual(steps, incremental.MAX_STEPS)
        self.assertEqual(self.policy.heads[1].state_classifier.export(), original)
        self.assertEqual(incremental._margin(self.policy, self.sample(0., 0))[0], 0)

    def test_repeated_inherited_corrects_are_noop_and_before_fit_is_honest(self):
        candidate = dict(self.agent)
        labels = [{"id": i + 1, "sample_ts": float(i), "desired": 1., "supervision_event_id": str(i)} for i in range(7)]
        samples = {float(i): self.sample(.76, 1)["features"] for i in range(7)}
        manager = SimpleNamespace(store=Mock(), engine=SimpleNamespace(models={}, rl_teaching=SimpleNamespace(
            _label_context=lambda a, p, ts: samples[ts])))
        anchors = [dict(self.sample(0., 0), action_idx=0)]
        balance = {"correction_class_counts": {"1": 7}, "anchor_class_counts": {"0": 1}, "shortfall": 0, "required": True}
        original = copy.deepcopy(self.policy.heads[1].state_classifier.export())
        with patch.object(incremental.conservative, "_teach_rows", return_value=labels), \
             patch.object(balanced, "_select_balancing_anchors", return_value=(anchors, balance)), \
             patch.object(balanced, "_anchor_pool", return_value=[]), \
             patch.object(incremental, "recent_agreement", return_value=[]):
            first = incremental.fine_tune(manager, candidate, self.policy)
            self.assertEqual(first["classifier_unresolved_updates"], 0)
            self.assertEqual(first["classifier_already_fitted_labels"], 7)
            self.assertEqual(first["classifier_optimizer_steps"], 0)
            self.assertEqual(self.policy.heads[1].state_classifier.export(), original)
            labels.append({"id": 8, "sample_ts": 7., "desired": 1., "supervision_event_id": "new"})
            samples[7.] = self.sample(.12, 1)["features"]
            second = incremental.fine_tune(manager, candidate, self.policy)
            self.assertEqual(second["teach_fit_before_count"], 7)
            self.assertEqual(second["teach_fit_after_count"], 8)
            self.assertEqual(second["classifier_unresolved_updates"], 1)
            self.assertEqual(self.policy.heads[1].state_classifier.seen[1], original["seen"][1] + 1)
            updated = copy.deepcopy(self.policy.heads[1].state_classifier.export())
            third = incremental.fine_tune(manager, candidate, self.policy)
            self.assertEqual(third["classifier_optimizer_steps"], 0)
            self.assertEqual(self.policy.heads[1].state_classifier.export(), updated)

    def test_unready_classifier_retains_existing_training_path(self):
        self.assertTrue(incremental.applies(self.policy))
        self.policy.heads[1].state_classifier.ready = False
        self.assertFalse(incremental.applies(self.policy))

    def test_diagnostics_publish_frozen_basis_optimizer_and_true_before(self):
        import correct_margin_repair as margin
        report = {"classifier_incremental": True, "classifier_update_accepted": True,
                  "classifier_unresolved_updates": 1, "classifier_recent_stability_samples": 9}
        with patch.object(margin, "_BASE_HARD_GATE", return_value={"passed": True, "reasons": []}):
            gate = margin._margin_offline_gate({}, {}, {}, report)
        self.assertEqual(gate["correct_optimizer"], "warm_classifier_frozen_basis_with_retention")
        self.assertEqual(gate["classifier_recent_stability_samples"], 9)

    def test_recent_guards_are_bounded_observed_and_reconstructed_once(self):
        h = fixture.ConservativeCandidateTests("test_candidate_clone_is_exact_parent_snapshot_before_fine_tune")
        h.setUp()
        self.addCleanup(h.tearDown)
        end = 100000.
        with h.store.conn() as db:
            db.execute("CREATE TABLE candidate_generation_decisions(generation_id TEXT,ts REAL,current REAL,desired REAL)")
            db.executemany("INSERT INTO candidate_generation_decisions VALUES(?,?,?,?)",
                           [("candidate:parent", end - i - 20, float(i % 2), float(i % 2)) for i in range(600)])
            db.execute("INSERT INTO candidate_generation_decisions VALUES(?,?,?,?)", ("candidate:parent", end + 10, 0., 0.))
            db.execute("INSERT INTO candidate_generation_decisions VALUES(?,?,?,?)", ("candidate:parent", end - 1, 0., 1.))
        tracker = SimpleNamespace(state_map={}, history=object(), close=Mock())
        def advance(ts):
            tracker.state_map["light.bathroom"] = {"entity_id": "light.bathroom", "state": "on" if int(end - ts - 20) % 2 else "off"}
        tracker.advance = advance
        fake = SimpleNamespace(actions=[0., 1.], schema=SimpleNamespace(entities=["sensor.stationary"]),
                               features=lambda states, history, at_ts: ({1: float(states["light.bathroom"]["state"] == "on")}, {}, {"reconstruction_complete": True}))
        manager = SimpleNamespace(store=h.store, _row_by_candidate=lambda _: {"parent_agent_id": "parent"}, engine=SimpleNamespace(context=object()))
        candidate = {"id": "child", "target_entity": "light.bathroom", "target_property": "power"}
        with patch.object(incremental.time, "time", return_value=end), \
             patch.object(incremental.replay, "SQLiteTemporalTracker", return_value=tracker) as factory, \
             patch.object(incremental.conservative, "_prediction_index", side_effect=lambda p, f: (int(f[1]), 1.)):
            rows = incremental.recent_agreement(manager, candidate, fake, [])
            self.assertEqual(len(rows), 48)
            factory.assert_called_once()
            tracker.close.assert_called_once()
            fake.features = lambda *a, **kw: ({}, {}, {"reconstruction_complete": False})
            self.assertEqual(incremental.recent_agreement(manager, candidate, fake, []), [])

    def test_observed_class_collapse_requires_both_classes_and_repeated_parent_agreement(self):
        good = {"per_action": {"0": {"samples": 9, "live_correct": 9, "candidate_correct": 0},
                               "1": {"samples": 9, "live_correct": 5, "candidate_correct": 9}}}
        self.assertTrue(incremental.comparison_collapse(good))
        for change in ({"samples": 7}, {"live_correct": 8}, {"candidate_correct": 1}):
            weak = copy.deepcopy(good)
            weak["per_action"]["0"].update(change)
            self.assertFalse(incremental.comparison_collapse(weak))
        self.assertFalse(incremental.comparison_collapse({"per_action": {"0": good["per_action"]["0"]}}))

    def test_recovery_requeues_same_child_preserving_parent_and_labels(self):
        h = fixture.ConservativeCandidateTests("test_candidate_clone_is_exact_parent_snapshot_before_fine_tune")
        h.setUp()
        self.addCleanup(h.tearDown)
        from agent_candidates import AgentCandidateManager
        from agent_candidate_lineage import ensure_lineage_tables
        ensure_lineage_tables(h.store)
        h.store.save_model(h.parent["id"], current_model())
        h._add_teach(sample_ts=1791835438.)
        row = AgentCandidateManager._create_candidate(h.manager, h.parent)
        h.store.save_model(row["candidate_id"], current_model())
        parent = h.store.get_model(h.parent["id"])
        comparison = {"per_action": {"0": {"samples": 9, "live_correct": 9, "candidate_correct": 0},
                                    "1": {"samples": 9, "candidate_correct": 9}}}
        gate = {"passed": True, "correct_optimizer": "pairwise_desired_plus_wrong_arm_penalty"}
        def set_state(state, gate=gate):
            with h.store.conn() as db:
                db.execute("UPDATE agent_candidates SET state=?,comparison_json=?,offline_gate_json=?",
                           (state, json.dumps(comparison), json.dumps(gate)))
        set_state("ready")
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 0)
        set_state("comparing", {"passed": True, "correct_optimizer": "warm_classifier_frozen_basis_with_retention"})
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 0)
        set_state("comparing")
        h.queue.status_for = Mock(return_value={"state": "active"})
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 0)
        h.queue.status_for = Mock(return_value=None)
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 1)
        fresh = h.manager._candidate_row(h.parent["id"])
        self.assertEqual((fresh["candidate_id"], fresh["state"]), (row["candidate_id"], "queued"))
        self.assertFalse(json.loads(fresh["offline_gate_json"])["passed"])
        self.assertEqual(h.store.get_model(h.parent["id"]), parent)
        self.assertEqual(len(h.rl.labels(h.parent["id"])), 1)
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 0)
        h.queue.enqueue.assert_not_called()

    def test_recovery_excludes_collapsed_gen2_from_snapshot_selection(self):
        h = fixture.ConservativeCandidateTests("test_candidate_clone_is_exact_parent_snapshot_before_fine_tune")
        h.setUp()
        self.addCleanup(h.tearDown)
        import agent_candidate_lineage as lineage
        import correct_margin_repair as margin
        from agent_candidates import AgentCandidateManager
        lineage.ensure_lineage_tables(h.store)
        h.store.save_model(h.parent["id"], current_model())
        root = lineage._ensure_root(h.store, h.parent["id"], 0)
        first = AgentCandidateManager._create_candidate(h.manager, h.parent)
        h.store.save_model(first["candidate_id"], self.policy.serialize())
        g1 = lineage._register_generation(h.store, h.parent["id"], root, first["candidate_id"], 1, "feedback", "parent")
        g1_agent = h.store.get_agent_config(first["candidate_id"])
        second = AgentCandidateManager._create_candidate(h.manager, g1_agent)
        h.store.save_model(second["candidate_id"], self.policy.serialize())
        lineage._register_generation(h.store, h.parent["id"], g1, second["candidate_id"], 2, "feedback", "comparing")
        gate = {"passed": True, "correct_optimizer": "pairwise_desired_plus_wrong_arm_penalty"}
        comparison = {"per_action": {"0": {"samples": 9, "live_correct": 9, "candidate_correct": 0},
                                    "1": {"samples": 9, "candidate_correct": 9}}}
        with h.store.conn() as db:
            db.execute("UPDATE agent_candidates SET state='parent',offline_gate_json=? WHERE candidate_id=?",
                       (json.dumps(gate), first["candidate_id"]))
            db.execute("UPDATE agent_candidates SET state='comparing',offline_gate_json=?,comparison_json=? WHERE candidate_id=?",
                       (json.dumps(gate), json.dumps(comparison), second["candidate_id"]))
        original = h.store.get_model(first["candidate_id"])
        self.assertEqual(margin._resolve_correction_base(h.manager, first["candidate_id"])["agent_id"], second["candidate_id"])
        self.assertEqual(incremental.recover_collapsed_correct(h.manager), 1)
        self.assertEqual(margin._resolve_correction_base(h.manager, first["candidate_id"])["agent_id"], first["candidate_id"])
        self.assertEqual(h.store.get_model(first["candidate_id"]), original)


if __name__ == "__main__":
    unittest.main()
