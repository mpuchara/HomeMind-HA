import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import *
from episode_evaluator import EpisodeEvaluator
from episode_evaluator_runtime import install_candidate, install_core, install_tournament
from rewards import RewardEngine
from storage import Store


class EpisodeEvaluatorRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "runtime.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_presence_probe_without_arrival_becomes_negative_episode(self):
        now = [10.0]
        evaluator = EpisodeEvaluator(self.store, clock=lambda: now[0])
        captured = {}
        trial = {
            "trial_id": "trial-1",
            "kind": "probe",
            "focus": "presence",
            "property": "power",
            "baseline": 0.0,
            "value": 1.0,
            "started": 0.0,
            "action_at": 0.0,
            "observation_start": 0.0,
            "observation_end": 10.0,
            "ack": 0.1,
            "outcome_sources": {"binary_sensor.kitchen": {"before": -1.0}},
        }

        class Experiments:
            def __init__(self):
                self.episode_evaluator = None
            def _get(self, aid):
                return {"active": trial}
            def _finish(self, aid, reward, reason):
                captured.update(aid=aid, reward=reward, reason=reason)

        engine = SimpleNamespace(
            experiments=Experiments(),
            executor=SimpleNamespace(reward_engine=RewardEngine()),
            _reward_pending=lambda *args, **kwargs: None,
            context=SimpleNamespace(home=None),
        )
        core = SimpleNamespace(ENGINE=engine, STORE=self.store)
        install_core(core, evaluator)
        engine.experiments._finish("light-agent", 0.02, "weak preference: observed without correction")

        self.assertEqual(captured["reward"], -0.6)
        self.assertIn("not confirmed", captured["reason"])
        rows = evaluator._policy_rows("light-agent", "experiment:trial-1")
        self.assertEqual(rows["experiment:trial-1"]["metrics"]["false_arrival_prediction"], 1)
        self.assertIsNone(rows["experiment:trial-1"]["metrics"]["unnecessary_on_seconds"])

    def test_tournament_policies_share_one_proxy_episode_without_dispatch(self):
        evaluator = EpisodeEvaluator(self.store, clock=lambda: 0.0)
        runtime = {"last_prediction": 1.0, "last_intent": {"decision_id": "decision-1"}}
        engine = SimpleNamespace(runtime={"light-agent": runtime}, state_map={})

        class Tournament:
            def __init__(self):
                self.engine = engine
                self.store = self_store
                self._shadow_runtime = {}
            def state(self, aid):
                return {"challenger_features": ["binary_sensor.hall"]}
            def observe_shadow(self, agent_cfg, states=None, changed_entities=None):
                self._shadow_runtime[agent_cfg["id"]] = {
                    "predictions": {"binary_sensor.hall": {"shadow_index": 1}}
                }
                return {"ok": True}
            def shadow_status(self, agent_cfg):
                return {"challengers": [{"entity_id": "binary_sensor.hall"}]}

        self_store = self.store
        tournament = Tournament()
        install_tournament(tournament, evaluator)
        cfg = agent(id="light-agent", target_entity="light.kitchen", target_property="power")
        off = {"light.kitchen": state("light.kitchen", "off")}
        on = {"light.kitchen": state("light.kitchen", "on")}
        with patch("episode_evaluator_runtime.time.time", side_effect=[0.0, 2.0]):
            tournament.observe_shadow(cfg, off)
            tournament.observe_shadow(cfg, on)

        comparison = evaluator.compare_policies(
            "light-agent",
            "tournament:active:light-agent:binary_sensor.hall",
            "tournament:shadow:light-agent:binary_sensor.hall",
        )
        self.assertEqual(comparison["matched_episodes"], 1)
        self.assertEqual(comparison["evidence_mode"], "automation_replay_proxy")
        self.assertEqual(comparison["independently_observed_episodes"], 0)

    def test_candidate_gate_switches_only_after_independent_episode_coverage(self):
        evaluator = EpisodeEvaluator(self.store, clock=lambda: 100.0)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """CREATE TABLE agent_candidate_generations (
                       generation_id TEXT PRIMARY KEY,
                       agent_id TEXT,
                       parent_generation_id TEXT,
                       root_agent_id TEXT
                   )"""
            )
            c.execute(
                "INSERT INTO agent_candidate_generations VALUES('child-gen','candidate','parent-gen','live')"
            )

        parent = agent(id="live", mode="shadow", training_state="qualified")
        candidate = dict(parent, id="candidate")
        self.store.get_model = lambda aid: {"version": 10}
        self.store.get_agent_config = lambda aid: dict(parent if aid == "live" else candidate)
        self.store.get_agent = lambda aid: dict(candidate)

        base = {
            "comparison_metric": "fast_timing_preference",
            "required_future_samples": 12,
            "required_future_samples_per_action": 4,
            "meaningful_opportunities": 12,
            "future_sample_count_ready": True,
            "per_action_ready": True,
            "fast_per_action_samples": {"0.0": 6, "1.0": 6},
            "parent_transition_accuracy": 1.0,
            "candidate_transition_accuracy": 1.0,
            "accuracy_safety_passed": True,
            "timing_safety_passed": True,
            "preference_confidence": 0.9,
            "preference_confidence_threshold": 0.65,
            "teach_anchor_passed": True,
            "no_new_corrections": True,
            "manual_corrections_since_generation": 0,
            "fresh_feedback_revision": True,
            "offline_gate_passed": True,
            "candidate_false_early": 0,
            "live_false_early": 0,
            "false_early_safety_passed": True,
            "promotable": True,
        }
        manager = SimpleNamespace(
            store=self.store,
            before_live_process=lambda *args: None,
            after_live_process=lambda *args: None,
            _comparison_summary=lambda *args, **kwargs: dict(base),
        )
        install_candidate(manager, evaluator)

        # Six OFF and six ON episodes. Both policies are evaluated on the exact same ids.
        for index in range(12):
            need = index % 2 == 1
            parent_on = need
            candidate_on = need
            observations = [
                {"ts": index * 10.0, "presence": True, "light_need": need, "power": need},
                {"ts": index * 10.0 + 5.0, "presence": True, "light_need": need, "power": need},
            ]
            evaluator.evaluate_episode(
                episode_id=f"independent-{index}",
                agent_id="live",
                start_ts=index * 10.0,
                end_ts=index * 10.0 + 5.0,
                observations=observations,
                policies=[
                    {"policy_key": "generation:parent-gen", "role": "candidate_parent", "executed": False,
                     "initial_power": parent_on, "decisions": [{"ts": index * 10.0, "power": parent_on}]},
                    {"policy_key": "generation:child-gen", "role": "candidate_shadow", "executed": False,
                     "initial_power": candidate_on, "decisions": [{"ts": index * 10.0, "power": candidate_on}]},
                ],
            )

        row = {
            "candidate_id": "candidate",
            "parent_agent_id": "live",
            "state": "comparing",
            "feedback_revision": 1,
            "build_revision": 1,
            "dirty": 0,
        }
        result = manager._comparison_summary(row, parent, candidate)
        self.assertEqual(result["comparison_metric"], "episode_light_power")
        self.assertEqual(result["meaningful_opportunities"], 12)
        self.assertEqual(result["fast_per_action_samples"], {"0.0": 6, "1.0": 6})
        self.assertEqual(result["episode_evidence_mode"], "independent_labels")
        self.assertIn("promotion_gates", result)
        self.assertTrue(result["promotion_gates"]["quality_regression"]["passed"])

    def test_proxy_only_candidate_episodes_do_not_relabel_automation_as_comfort(self):
        evaluator = EpisodeEvaluator(self.store)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """CREATE TABLE agent_candidate_generations (
                       generation_id TEXT PRIMARY KEY,
                       agent_id TEXT,
                       parent_generation_id TEXT,
                       root_agent_id TEXT
                   )"""
            )
            c.execute("INSERT INTO agent_candidate_generations VALUES('child','candidate','parent','live')")
        manager = SimpleNamespace(
            store=self.store,
            before_live_process=lambda *args: None,
            after_live_process=lambda *args: None,
            _comparison_summary=lambda *args, **kwargs: {
                "comparison_metric": "legacy",
                "required_future_samples": 12,
                "required_future_samples_per_action": 4,
                "meaningful_opportunities": 5,
            },
        )
        install_candidate(manager, evaluator)
        evaluator.record_automation_proxy(
            episode_id="proxy-only", agent_id="live", start_ts=0, end_ts=8,
            baseline_power=False,
            policies=[
                {"policy_key": "generation:parent", "role": "candidate_parent", "executed": False,
                 "initial_power": False, "decisions": []},
                {"policy_key": "generation:child", "role": "candidate_shadow", "executed": False,
                 "initial_power": False, "decisions": [{"ts": 0, "power": True}]},
            ],
        )
        result = manager._comparison_summary({"candidate_id": "candidate"})
        self.assertEqual(result["comparison_metric"], "legacy")
        self.assertEqual(result["episode_evidence_mode"], "automation_replay_proxy")
        self.assertEqual(result["episode_comparison"]["independently_observed_episodes"], 0)


if __name__ == "__main__":
    unittest.main()
