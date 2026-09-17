import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_conservative_correct import _copy_parent_snapshot, install as install_conservative_correct
from agent_candidate_lineage import install as install_lineage
from agent_explore import FREE_TRAIN_REASON, install as install_explore
from agent_workflow_actions import install as install_workflow
from context_tournament import ContextTournament
from experiments import Experiments
from test_agent_explore import FakeExecutor, FakeHandler, FakeQueue, FakeRLTeaching, FakeTeaching, DummyPolicy
from trial_knowledge import install as install_trial_knowledge


class TrialPolicy(DummyPolicy):
    """Test backend matching the production MultiHorizonPolicy action contract."""
    def __init__(self, store, agent):
        super().__init__(store, agent)
        self.actions = list(self.heads[1.0].actions)

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        rows = list(self.model.get("trial_updates") or [])
        rows.append({
            "horizon": int(horizon),
            "action_index": int(action_idx),
            "reward": float(reward),
            "features": {str(k): float(v) for k, v in features.items()},
        })
        self.model["trial_updates"] = rows
        self.model["trial_reward_sum"] = float(self.model.get("trial_reward_sum") or 0) + float(reward)
        by_action = dict(self.model.get("trial_reward_by_action") or {})
        key = str(int(action_idx))
        by_action[key] = float(by_action.get(key) or 0) + float(reward)
        self.model["trial_reward_by_action"] = by_action
        self.model_revision = "trial-" + str(uuid.uuid4())


class FixedRng:
    def __init__(self, value):
        self.value = float(value)

    def random(self):
        return self.value


class TrialKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "trial.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Trial light",
            "target_entity": "light.trial",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .2,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.presence"],
        })
        self.root_model = {
            "version": 10,
            "model_revision": "root-r1",
            "prediction": 0.0,
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 4},
            "weights": {"root": 1},
            "trial_reward_sum": 0.0,
        }
        self.store.save_model(self.root["id"], self.root_model)
        self.store.set_training_state(
            self.root["id"], "qualified", score=.95, samples=100, source="test",
            detail={
                "balanced": True,
                "class_coverage": True,
                "counts": {
                    "samples": 100,
                    "correct": 95,
                    "per_action": {
                        "0": {"samples": 50, "correct": 48},
                        "1": {"samples": 50, "correct": 47},
                    },
                },
                "per_action_accuracy": {"0": .96, "1": .94},
            },
        )
        self.store.update_agent(self.root["id"], {"mode": "control"})
        self.root = self.store.get_agent(self.root["id"])

        self.executor = FakeExecutor()
        self.queue = FakeQueue()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(),
            rl_teaching=FakeRLTeaching(self.store),
            models={},
            runtime={},
            executor=self.executor,
            temporal_history=None,
            entity_registry={},
            context_relevance={},
            state_map={
                "light.trial": {
                    "entity_id": "light.trial", "state": "off", "attributes": {}, "context": {}
                },
                "binary_sensor.presence": {
                    "entity_id": "binary_sensor.presence", "state": "on",
                    "attributes": {"device_class": "occupancy"},
                },
            },
            own_command_echo=lambda *args, **kwargs: False,
            process_agent=lambda *args, **kwargs: None,
            wake_event=SimpleNamespace(set=lambda: None),
            lock=threading.RLock(),
        )
        self.engine.policy = lambda agent: TrialPolicy(self.store, agent)
        self.engine.experiments = Experiments(self.store, clock=lambda: time.time())
        self.engine.context_tournament = ContextTournament(self.store, self.engine)
        core = SimpleNamespace(
            STORE=self.store,
            ENGINE=self.engine,
            Handler=FakeHandler,
            TRAINING_QUEUE=self.queue,
            HISTORY=None,
        )
        manager = AgentCandidateManager(core, start_worker=False)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        manager = install_workflow(manager)
        manager = install_explore(manager)
        self.manager = install_trial_knowledge(manager)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def model(self, aid):
        return self.store.get_model(aid)

    def start_free(self, enabled=False, info=True):
        self.engine.experiments.configure(self.root, {"enabled": enabled})
        return self.manager.workflow_explore(self.root["id"], {
            "mode": "free",
            "information_exploration": info,
            "config": {
                "focus": "presence",
                "intensity": .2,
                "interval": 300,
                "daily_budget": 1,
                "observation_seconds": 10,
                "max_step": 1,
            },
        })

    def trial(self, ack=True):
        now = time.time()
        data = self.engine.experiments._get(self.root["id"])
        return {
            "trial_id": str(uuid.uuid4()),
            "kind": "probe",
            "arm": 1,
            "value": 1.0,
            "baseline": 0.0,
            "index": 1,
            "x": {"bias": 1.0},
            "prediction_inputs": {"binary_sensor.presence": 1.0},
            "background_dependencies": {},
            "outcome_sources": {
                "binary_sensor.presence": {"role": "presence", "before": -1.0}
            },
            "snapshot": {"binary_sensor.presence": 1.0},
            "focus": "presence",
            "revision": data["revision"],
            "policy_version": 10,
            "model_revision": "root-r1",
            "target": "light.trial",
            "property": "power",
            "confidence": .95,
            "support": .9,
            "novelty": .1,
            "gap": 0.0,
            "gain": .1,
            "started": now,
            "deadline": now + 20,
            "action_at": now,
            "observation_start": now,
            "observation_end": now + 10,
            "ack": now if ack else None,
            "window": 10,
            "trial_record": {
                "hypothesis": {
                    "id": "earlier_on", "catalog_version": 1,
                    "information_exploration": False,
                },
                "policy_features": {"0": 1.0, "1": .75},
                "horizon": 1.0,
                "model_versions": {
                    "trial_record_version": 1,
                    "policy_version": 10,
                    "model_revision": "root-r1",
                    "schema_version": 11,
                },
                "action_set": [
                    {"role": "reference", "index": 0, "value": 0.0, "propensity": .25},
                    {"role": "probe", "index": 1, "value": 1.0, "propensity": .75},
                ],
                "assigned_action": {"kind": "probe", "index": 1, "value": 1.0},
                "assigned_propensity": .75,
                "baseline_index": 0,
                "selection_reason": "legacy_bounded_trial",
            },
        }

    def finish_and_train(self, reward):
        started = self.start_free()
        child_gid = started["child_generation_id"]
        child = self.manager.lineage_status(child_gid)
        trial = self.trial()
        self.engine.experiments._start(self.root["id"], trial)
        self.engine.experiments._finish(self.root["id"], reward, "labelled test outcome")
        row = self.manager._candidate_row(self.root["id"])
        self.assertEqual(row["reason"], FREE_TRAIN_REASON)
        self.assertTrue(self.manager._start_build(row))
        return child_gid, child, trial

    def test_positive_trial_updates_only_direct_child_and_not_live_residual(self):
        live_before = copy.deepcopy(self.model(self.root["id"]))
        child_gid, child, trial = self.finish_and_train(.6)
        child_model = self.model(child["agent_id"])
        self.assertAlmostEqual(child_model["trial_reward_sum"], .6)
        self.assertAlmostEqual(child_model["trial_reward_by_action"]["1"], .6)
        self.assertEqual(self.model(self.root["id"]), live_before)
        self.assertEqual(self.engine.experiments._get(self.root["id"])["learners"], {})
        record = self.manager.trial_journal.get(trial["trial_id"])
        self.assertEqual(record["learning_applied_generation_id"], child_gid)
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_negative_trial_remains_negative_and_history_is_not_replayed(self):
        live_before = copy.deepcopy(self.model(self.root["id"]))
        _gid, child, trial = self.finish_and_train(-1.0)
        child_model = self.model(child["agent_id"])
        self.assertAlmostEqual(child_model["trial_reward_sum"], -1.0)
        self.assertAlmostEqual(child_model["trial_reward_by_action"]["1"], -1.0)
        self.assertEqual(self.model(self.root["id"]), live_before)
        record = self.manager.trial_journal.get(trial["trial_id"])
        self.assertEqual(json.loads(record["episode_result_json"])["reward"], -1.0)
        session = self.manager.workflow_explore_status(self.root["id"])["session"]
        self.assertFalse(session["result"]["trial_training"]["ordinary_history_replayed"])

    def test_restart_retry_does_not_apply_same_trial_twice(self):
        _gid, child, _trial = self.finish_and_train(.6)
        first = copy.deepcopy(self.model(child["agent_id"]))
        self.assertTrue(self.manager._start_build(self.manager._candidate_row(self.root["id"])))
        second = self.model(child["agent_id"])
        self.assertEqual(second, first)
        self.assertEqual(len(second.get("trial_updates") or []), 1)

    def test_unlabelled_trial_does_not_update_policy(self):
        started = self.start_free()
        child = self.manager.lineage_status(started["child_generation_id"])
        before = copy.deepcopy(self.model(child["agent_id"]))
        trial = self.trial(ack=False)
        self.engine.experiments._start(self.root["id"], trial)
        self.engine.experiments._finish(self.root["id"], None, "no device acknowledgement")
        session = self.manager.workflow_explore_status(self.root["id"])["session"]
        self.assertEqual(session["status"], "no_evidence")
        record = self.manager.trial_journal.get(trial["trial_id"])
        self.assertIsNone(record["reward"])
        self.assertIsNone(record["learning_applied_generation_id"])
        self.assertEqual(self.model(child["agent_id"]), before)

    def test_previous_experiment_config_is_restored(self):
        self.engine.experiments.configure(
            self.root, {"enabled": False, "focus": "devices", "daily_budget": 4}
        )
        previous = copy.deepcopy(self.engine.experiments.status(self.root["id"])["config"])
        started = self.manager.workflow_explore(self.root["id"], {
            "mode": "free",
            "config": {
                "focus": "presence", "intensity": .2, "interval": 300,
                "daily_budget": 1, "observation_seconds": 10, "max_step": 1,
            },
        })
        trial = self.trial()
        self.engine.experiments._start(self.root["id"], trial)
        self.engine.experiments._finish(self.root["id"], .2, "measured")
        self.assertEqual(self.engine.experiments.status(self.root["id"])["config"], previous)
        self.assertEqual(
            started["session"]["result"]["knowledge_integration"],
            "explicit_child_training_from_trial_records",
        )

    def test_generation_trial_update_rolls_back_to_exact_parent_snapshot(self):
        _gid, child, _trial = self.finish_and_train(.6)
        self.assertNotEqual(self.model(child["agent_id"]), self.model(self.root["id"]))
        _copy_parent_snapshot(self.manager, self.root["id"], child["agent_id"])
        self.assertEqual(self.model(child["agent_id"]), self.model(self.root["id"]))

    def test_off_policy_report_refuses_uncovered_action(self):
        result = self.start_free()
        trial = self.trial()
        trial["trial_record"]["action_set"][0]["propensity"] = 1.0
        trial["trial_record"]["action_set"][1]["propensity"] = 0.0
        trial["trial_record"]["assigned_action"] = {
            "kind": "reference", "index": 0, "value": 0.0
        }
        trial["trial_record"]["assigned_propensity"] = 1.0
        trial.update(kind="reference", index=0, value=0.0)
        self.engine.experiments._start(self.root["id"], trial)
        report = self.manager.trial_journal.off_policy_report(
            result["session"]["session_id"], 1.0
        )
        self.assertFalse(report["supported"])
        self.assertEqual(report["reason"], "no_propensity_coverage")
        self.assertIsNone(report["estimate"])

    def test_information_exploration_needs_no_positive_base_gradient(self):
        result = self.start_free(info=True)
        self.engine.experiments.rng = FixedRng(.9)
        policy = TrialPolicy(self.store, self.root)
        chosen = {
            "index": 0, "value": 0.0, "mean": 0.0,
            "support": .95, "novelty": .05,
        }
        arms = [
            dict(chosen, uncertainty=.05),
            {
                "index": 1, "value": 1.0, "mean": 0.0,
                "support": .95, "novelty": .05, "uncertainty": .8,
            },
        ]
        prepared = self.engine.experiments.propose(
            self.root,
            policy,
            dict(self.engine.state_map),
            {},
            {0: 1.0, 1: 1.0},
            {0: ["bias"], 1: ["binary_sensor.presence:value"]},
            chosen,
            .95,
            arms,
            1.0,
            {},
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["kind"], "probe")
        meta = prepared["trial_record"]
        self.assertEqual(meta["hypothesis"]["id"], "earlier_on")
        self.assertTrue(meta["hypothesis"]["information_exploration"])
        self.assertAlmostEqual(meta["assigned_propensity"], .75)
        self.assertIsNone(prepared["gain"])
        self.assertEqual(
            result["session"]["result"]["hypothesis_catalog"], ["earlier_on"]
        )
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()

    def test_trial_record_has_versioned_assignment_dispatch_ack_and_outcome(self):
        self.start_free()
        trial = self.trial()
        self.engine.experiments._start(self.root["id"], trial)
        self.manager.trial_journal.dispatch(
            trial["trial_id"], time.time(), intent_id="intent-1", desired_value=1.0
        )
        self.manager.trial_journal.ack(trial["trial_id"], time.time(), 1.0)
        self.engine.experiments._finish(self.root["id"], .6, "confirmed")
        record = self.manager.trial_journal.get(trial["trial_id"])
        self.assertEqual(record["record_version"], 1)
        self.assertEqual(json.loads(record["hypothesis_json"])["id"], "earlier_on")
        self.assertTrue(json.loads(record["context_json"])["policy_features"])
        self.assertEqual(json.loads(record["model_versions_json"])["policy_version"], 10)
        self.assertEqual(len(json.loads(record["action_set_json"])), 2)
        self.assertAlmostEqual(record["propensity"], .75)
        self.assertEqual(json.loads(record["dispatch_json"])["intent_id"], "intent-1")
        self.assertEqual(json.loads(record["ack_json"])["status"], "acknowledged")
        self.assertEqual(json.loads(record["episode_result_json"])["reward"], .6)
        self.assertEqual(record["termination_reason"], "confirmed")


if __name__ == "__main__":
    unittest.main()
