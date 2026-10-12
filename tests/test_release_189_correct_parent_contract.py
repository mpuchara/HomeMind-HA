"""Correct must load the installed observation contract after cold composition."""
import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock

import support
# These imports deliberately precede observation-contract installation.
import correct_schema_evolution as evolution
import policy
import replay
import observation_contract as observation
from policy_backend import model_checksum, serialize_backend_model, verify_model_checksum
import test_candidate_conservative_correct as fixture
from agent_candidates import AgentCandidateManager


def current_model(contract=4):
    schema = policy.ExplicitFeatureSchema(128, ["sensor.stationary", "sensor.moving"],
                                          feature_contract_version=contract)
    return serialize_backend_model(
        {"version": policy.MultiHorizonPolicy.VERSION, "dims": 128,
         "schema": schema.export(), "actions": [0., 1.], "heads": {}},
        policy_backend="diagonal_linucb", backend_version=policy.MultiHorizonPolicy.VERSION,
    )


class InstalledCorrectContractTests(unittest.TestCase):
    def setUp(self):
        self.binding = observation.install_training_contract()
        self.addCleanup(self.binding["restore"])

    def test_cold_import_then_install_accepts_current_parent(self):
        self.assertTrue(evolution._stable_model_schema_compatible(current_model()))
        self.assertEqual(evolution._schema_contract_detail(current_model())["required_schema_version"], 12)

    def test_checksum_corruption_still_fails_closed(self):
        raw = current_model()
        raw["schema"]["entities"].append("sensor.corruption")
        self.assertFalse(evolution._stable_model_schema_compatible(raw))

    def test_obsolete_schema_still_requires_isolated_rebuild(self):
        raw = current_model()
        raw["schema"]["version"] = 11
        raw["model_checksum"] = model_checksum(raw)
        self.assertFalse(evolution._stable_model_schema_compatible(raw))

    def test_incompatible_dimensions_do_not_accept_a_fresh_policy_reset(self):
        raw = current_model()
        raw["dims"] = raw["schema"]["dims"] = 64
        raw["model_checksum"] = model_checksum(raw)
        self.assertFalse(evolution._stable_model_schema_compatible(raw))

    def test_context_growth_preserves_all_released_feature_contracts(self):
        for contract in (1, 2, 3, 4):
            with self.subTest(contract=contract):
                raw = current_model(contract)
                changed = evolution.migrate_model_schema(raw, ["sensor.new", "sensor.stationary", "sensor.moving"])
                self.assertEqual(changed["schema"]["feature_contract_version"], contract)
                self.assertTrue(verify_model_checksum(changed))
                self.assertEqual(raw, current_model(contract))

    def test_context_growth_preserves_classifier_rejections_and_entire_home_tail(self):
        agent = support.agent(input_entities=["sensor.stationary", "sensor.moving"])
        parent = policy.MultiHorizonPolicy(agent, {}, {}, set(), model=current_model())
        head = parent.heads[1]
        for i in range(12):
            for action in (0, 1):
                head.update(action, {0: 1., 5: float(action), 17: i / 12., 127: 1.}, 1.)
        classifier = head.state_classifier
        classifier.ready = True
        classifier.weights[5] = 2.
        classifier.weights[17] = -.5
        classifier.weights[127] = .2
        classifier.hinge_weights[0][17] = .3
        for arm in (0, 1):
            for index in range(128):
                head.rejection_b[arm][index] = -(index + 1.) / 1000.
                head.b[arm][index] = arm * 1000. + index
        raw = parent.serialize()
        untouched = copy.deepcopy(raw)
        changed = evolution.migrate_model_schema(raw, ["sensor.new", "sensor.stationary", "sensor.moving"])
        self.assertEqual(raw, untouched)
        self.assertTrue(verify_model_checksum(changed))
        new = policy.MultiHorizonPolicy(agent, {}, {}, set(), model=changed)
        self.assertEqual(new.heads[1].state_classifier.weights[17], 2.)
        self.assertEqual(new.heads[1].state_classifier.weights[29], -.5)
        self.assertEqual(new.heads[1].state_classifier.weights[5], 0.)
        self.assertEqual(new.heads[1].state_classifier.hinge_weights[0][29], .3)
        self.assertEqual(new.heads[1].rejection_b[0][17], head.rejection_b[0][5])
        self.assertEqual(new.heads[1].rejection_b[0][5], 0.)
        for index in range(128 - observation.HOME_TAIL, 128):
            self.assertEqual(changed["heads"]["1"]["b"][1][index], raw["heads"]["1"]["b"][1][index])
        for action in (0, 1):
            self.assertEqual(new.heads[1].state_classifier.rows[action][0]["x"][17],
                             classifier.rows[action][0]["x"][5])
        old_x = {0: 1., 5: .7, 17: .2, 127: 1.}
        new_x = {0: 1., 17: .7, 29: .2, 127: 1.}
        self.assertAlmostEqual(classifier.score(old_x), new.heads[1].state_classifier.score(new_x))

    def _recovery_fixture(self, state="insufficient_evidence"):
        h = fixture.ConservativeCandidateTests("test_candidate_clone_is_exact_parent_snapshot_before_fine_tune")
        h.setUp()
        self.addCleanup(h.tearDown)
        h.store.save_model(h.parent["id"], current_model())
        h._add_teach(sample_ts=1791835438.)
        row = AgentCandidateManager._create_candidate(h.manager, h.parent)
        with h.store.lock, h.store.conn() as db:
            db.execute("UPDATE agent_candidates SET reason='schema_upgrade_rebuild',state=?,offline_gate_json=?",
                       (state, json.dumps({"status": "insufficient_evidence", "benchmark_samples": 0})))
        return h, row

    def test_recovery_requeues_existing_bad_child_without_changing_parent_or_labels(self):
        h, row = self._recovery_fixture()
        parent = h.store.get_model(h.parent["id"])
        self.assertEqual(evolution._recover_compatible_correct_builds(h.manager), 1)
        fresh = h.manager._candidate_row(h.parent["id"])
        self.assertEqual(fresh["candidate_id"], row["candidate_id"])
        self.assertEqual((fresh["reason"], fresh["state"]), ("feedback", "queued"))
        self.assertEqual(json.loads(fresh["offline_gate_json"]), {})
        self.assertEqual(h.store.get_model(h.parent["id"]), parent)
        self.assertEqual(len(h.rl.labels(h.parent["id"])), 1)
        self.assertEqual(evolution._recover_compatible_correct_builds(h.manager), 0)
        h.queue.enqueue.assert_not_called()

    def test_recovery_does_not_interrupt_active_training(self):
        h, row = self._recovery_fixture("queued")
        h.queue.status_for = Mock(return_value={"state": "active"})
        self.assertEqual(evolution._recover_compatible_correct_builds(h.manager), 0)
        self.assertEqual(h.manager._candidate_row(h.parent["id"])["reason"], "schema_upgrade_rebuild")

    def test_recovery_cancels_only_pending_detour(self):
        h, row = self._recovery_fixture("queued")
        h.queue.status_for = Mock(return_value={"state": "queued"})
        h.queue.cancel = Mock(return_value=True)
        self.assertEqual(evolution._recover_compatible_correct_builds(h.manager), 1)
        h.queue.cancel.assert_called_once_with(row["candidate_id"])

    def test_approved_candidate_is_not_requeued(self):
        h, _row = self._recovery_fixture("ready")
        self.assertEqual(evolution._recover_compatible_correct_builds(h.manager), 0)


class RealCandidatePipelineTests(unittest.TestCase):
    def test_five_on_corrections_repair_snapshot_and_keep_off_history(self):
        # A fresh interpreter exercises the real cold import order, independent of
        # module-level hooks installed by earlier tests in unittest discovery.
        result = subprocess.run([sys.executable, __file__, "--integration"],
                                cwd=support.ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(receipt["passed"], receipt)


def integration():
    binding = observation.install_training_contract()
    h = fixture.ConservativeCandidateTests("test_candidate_clone_is_exact_parent_snapshot_before_fine_tune")
    h.setUp()
    try:
        from agent_candidate_lineage import ensure_lineage_tables
        ensure_lineage_tables(h.store)
        agent = h.store.get_agent_config(h.parent["id"])
        parent = policy.MultiHorizonPolicy(agent, {}, {}, set(), model=current_model())
        for i in range(160):
            for action in (0, 1):
                parent.update(1, action, {0: 1., 5: .76 if action else 0., 17: (i % 5) / 5.}, 1.)
        corrected = {0: 1., 5: .3, 17: .9}
        before_prediction = parent.predict(corrected)[0]["value"]
        h.store.save_model(agent["id"], parent.serialize())
        h.engine.policy = lambda a: policy.MultiHorizonPolicy(
            a, {}, {}, set(), model=h.store.get_model(a["id"]))
        h.rl._label_context = lambda *args: dict(corrected)
        for i in range(5):
            h._add_teach(sample_ts=1791835438. + i * 20.)
        for i in range(24):
            action = i % 2
            features = {0: 1., 5: .76 if action else 0., 17: (i % 5) / 5.}
            ts = 1791840000. + i
            h.store.archive_upsert("light.bathroom", ts, "on" if action else "off", {}, None, "test")
            with h.store.conn() as db:
                history_id = db.execute("SELECT id FROM entity_history WHERE entity_id=? AND ts=?",
                                        ("light.bathroom", ts)).fetchone()[0]
            with h.store.lock, h.store.conn() as db:
                db.execute("""INSERT INTO historical_experiences(agent_id,target_history_id,created_at,
                           action_index,action_value,reward,dwell_seconds,features_json,user_id)
                           VALUES(?,?,?,?,?,?,?,?,NULL)""",
                           (agent["id"], history_id, "2026-10-13T00:00:00+00:00", action, action,
                            1., 1., json.dumps(features)))
        evolution.install(h.core, h.manager)
        status = h.manager.enqueue(agent["id"], "feedback")
        original = h.store.get_model(agent["id"])
        assert h.store.get_model(status["candidate_id"]) == original
        h.manager._start_build(h.manager._candidate_row(agent["id"]))
        status = h.manager.status(agent["id"])
        child = h.engine.policy(h.store.get_agent_config(status["candidate_id"]))
        empty_predictions = [child.predict({0: 1., 5: 0., 17: i / 5.})[0]["value"] for i in range(5)]
        receipt = {
            "parent_prediction_before": before_prediction,
            "candidate_corrected_prediction": child.predict(corrected)[0]["value"],
            "empty_predictions": empty_predictions,
            "parent_unchanged": original == h.store.get_model(agent["id"]),
            "historical_rebuild_calls": h.queue.enqueue.call_count,
            "gate": status["offline_gate"],
        }
        receipt["passed"] = bool(before_prediction == 0. and receipt["candidate_corrected_prediction"] == 1.
                                 and empty_predictions == [0.] * 5 and receipt["parent_unchanged"]
                                 and not receipt["historical_rebuild_calls"] and receipt["gate"]["passed"])
        print(json.dumps(receipt))
        assert receipt["passed"], receipt
    finally:
        h.tearDown()
        binding["restore"]()


if __name__ == "__main__":
    if "--integration" in sys.argv:
        integration()
    else:
        unittest.main()
