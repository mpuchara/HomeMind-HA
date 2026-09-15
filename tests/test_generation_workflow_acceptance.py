import copy
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from agent_candidates import AgentCandidateManager, ensure_tables, install_store_overlay
from agent_candidate_config_guard import install as install_config_guard
from agent_candidate_balance import install as install_balance
from agent_candidate_debounce import install as install_debounce
from agent_candidate_teach_status import install as install_teach_status
from agent_candidate_lifecycle_hardening import install as install_lifecycle_hardening
from agent_candidate_conservative_correct import install as install_conservative_correct
from agent_candidate_lineage import install as install_lineage
from agent_candidate_lineage_retention import install as install_lineage_retention
from agent_candidate_lineage_guards import install as install_lineage_guards
from agent_candidate_shadow_runtime import install as install_shadow_runtime
from agent_candidate_shadow_context import install as install_shadow_context
from agent_candidate_atomic_promote import install as install_atomic_promote
from context_tournament import ContextTournament
from experiments import Experiments
from lease_journal import LeaseJournal
from teaching_rl import fingerprint


CONTROL_PROOF = {
    "balanced": True,
    "counts": {
        "samples": 200,
        "correct": 200,
        "per_action": {
            "0": {"samples": 100, "correct": 100},
            "1": {"samples": 100, "correct": 100},
        },
    },
}


class FakeHandler:
    def do_GET(self):
        return None

    def do_POST(self):
        return None

    def do_DELETE(self):
        return None


class FakeQueue:
    def __init__(self):
        self.calls = []

    def status_for(self, agent_id):
        return None

    def cancel(self, agent_id):
        return False

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        call = {
            "agent_id": str(agent_id), "rebuild": bool(rebuild),
            "reason": str(reason), "state": "queued", "position": 1,
        }
        self.calls.append(call)
        return dict(call)


class FakeTeaching:
    def teach(self, engine, agent, desired=None, sample_ts=None):
        return {"ok": True, "desired_value": float(desired if desired is not None else 0.0)}

    def undo(self, engine, agent):
        return {"ok": True}

    def match(self, *args, **kwargs):
        return None

    def point_context(self, engine, agent, sample_ts):
        return dict(engine.state_map), {}, {}


class FakeRLTeaching:
    MAX_LABELS = 256

    def __init__(self, store):
        self.store = store
        with store.lock, store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT NOT NULL,
                    undone_ts REAL
                );
                CREATE TABLE IF NOT EXISTS teaching_rl_jobs (
                    agent_id TEXT PRIMARY KEY,
                    requested_ts REAL NOT NULL,
                    state TEXT NOT NULL,
                    original_inputs_json TEXT NOT NULL,
                    pre_schema_json TEXT NOT NULL,
                    selected_inputs_json TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def labels(self, agent_id):
        with self.store.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT * FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL ORDER BY sample_ts,id""",
                (str(agent_id),),
            ).fetchall()]

    def _label_context(self, agent, policy, sample_ts):
        # The Correct point is deliberately isolated from held-out regression rows.
        return {0: 1.0, 1: 1.0}

    def add_label(self, agent, desired, sample_ts):
        return {"ok": True}

    def undo(self, agent):
        return {"ok": True}

    def status(self, agent_id):
        return {"state": "idle", "report": {}}


class DummySchema:
    def __init__(self, entities):
        self.entities = list(entities or [])
        self.version = 11

    def labels(self):
        return {0: ["bias"], 1: ["presence"], 2: ["other"]}


class AcceptancePolicy:
    VERSION = 10

    def __init__(self, store, agent):
        self.store = store
        self.agent = agent
        self.model = copy.deepcopy(store.get_model(agent["id"]) or {})
        self.actions = [0.0, 1.0]
        self.horizons = [1.0]
        self.model_revision = self.model.get("model_revision") or "acceptance-r0"
        self.selection_meta = dict(self.model.get("selection_meta") or {})
        self.schema = DummySchema((self.model.get("schema") or {}).get("entities") or [])
        self.mapping = {
            str(k): int(v)
            for k, v in (self.model.get("toy_mapping") or {"1": 0, "2": 0, "3": 1}).items()
        }

    @staticmethod
    def _key(features):
        if float(features.get(1, 0.0)) >= .5:
            return "1"
        if float(features.get(2, 0.0)) >= .5:
            return "2"
        return "3"

    def features(self, state_map, history, at_ts=None):
        presence = str((state_map.get("binary_sensor.presence") or {}).get("state") or "off").lower() == "on"
        other = str((state_map.get("binary_sensor.other") or {}).get("state") or "off").lower() == "on"
        return {0: 1.0, 1: 1.0 if presence else 0.0, 2: 1.0 if other else 0.0}, self.schema.labels(), {}

    def predict(self, features):
        value = float(self.mapping.get(self._key(features), 0))
        chosen = {
            "index": int(value >= .5), "value": value,
            "mean": .9, "support": .95, "novelty": .05,
        }
        return chosen, .95, [chosen], 1.0, .95, .05

    def update(self, horizon, action_idx, features, reward):
        # Conservative Correct supplies a negative observation for the old prediction and
        # a strong positive observation for the explicit desired action. Only the positive
        # label changes this deterministic fixture.
        if float(reward) > 0:
            self.mapping[self._key(features)] = int(action_idx)

    def serialize(self):
        raw = copy.deepcopy(self.model)
        raw.update({
            "version": 10,
            "model_revision": self.model_revision,
            "toy_mapping": dict(self.mapping),
            "schema": {"version": 11, "entities": list(self.schema.entities)},
            "selection_meta": dict(self.selection_meta),
        })
        return raw


class FakeKnowledge:
    def __init__(self, target):
        self.lock = threading.RLock()
        self.automations = [
            {
                "entity_id": "automation.was_on", "name": "Was on",
                "target_entities": [target], "enabled": True, "config_status": "fresh",
            },
            {
                "entity_id": "automation.user_off", "name": "User off",
                "target_entities": [target], "enabled": False, "config_status": "fresh",
            },
        ]

    def hints_for_target(self, target):
        return set(), [x for x in self.automations if target in (x.get("target_entities") or [])]


class FakeHandoff:
    def __init__(self, store, states, knowledge):
        self.journal = LeaseJournal(store)
        self.states = states
        self.knowledge = knowledge
        self.disable_calls = []
        self.restore_calls = []

    def state_map(self):
        return dict(self.states)

    def refresh(self):
        return None

    def disable_one(self, entity_id):
        self.disable_calls.append(str(entity_id))
        self.states[str(entity_id)] = {
            **self.states.get(str(entity_id), {}),
            "entity_id": str(entity_id), "state": "off",
        }

    def restore_one(self, entity_id):
        self.restore_calls.append(str(entity_id))
        self.states[str(entity_id)] = {
            **self.states.get(str(entity_id), {}),
            "entity_id": str(entity_id), "state": "on",
        }


class FakeExecutor:
    def __init__(self, store, states, knowledge):
        self.handoff = FakeHandoff(store, states, knowledge)
        self._locks = {}
        self.service = Mock(side_effect=AssertionError("Candidate must never call a HA service"))
        self._service = Mock(side_effect=AssertionError("Candidate must never call a HA service"))
        self.submit = Mock(side_effect=AssertionError("Candidate must never submit an ActionIntent"))
        self.take_calls = []
        self.release_calls = []

    def target_lock(self, entity_id):
        return self._locks.setdefault(str(entity_id), threading.RLock())

    def take_control(self, agent, refresh=False):
        self.take_calls.append((str(agent["id"]), bool(refresh)))
        target = str(agent["target_entity"])
        changed = []
        _, infos = self.handoff.knowledge.hints_for_target(target)
        for info in infos:
            entity_id = str(info["entity_id"])
            if (self.handoff.states.get(entity_id) or {}).get("state") == "on":
                self.handoff.disable_one(entity_id)
                changed.append(entity_id)
        self.handoff.journal.save(agent, changed)
        return changed

    def release_control(self, agent, reason="mode_change"):
        self.release_calls.append((str(agent["id"]), str(reason)))
        target = str(agent["target_entity"])
        lease = self.handoff.journal.get(target)
        owned = list((lease or {}).get("disabled_automations") or [])
        for entity_id in owned:
            self.handoff.restore_one(entity_id)
        self.handoff.journal.clear(target)
        return owned


class GenerationWorkflowAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "generation-acceptance.db")
        install_store_overlay(self.store)
        ensure_tables(self.store)
        self.rl = FakeRLTeaching(self.store)

        self.root = self.store.create_agent({
            "name": "Generation acceptance light",
            "target_entity": "light.acceptance",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
            "input_entities": ["binary_sensor.presence"],
        })
        self.root_model = {
            "version": 10,
            "model_revision": "g0-r0",
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 1},
            "toy_mapping": {"1": 0, "2": 0, "3": 0},
            "weights": {"parent_knowledge": [1, 2, 3, 4]},
        }
        self.store.save_model(self.root["id"], copy.deepcopy(self.root_model))
        self.store.set_training_state(
            self.root["id"], "qualified", score=1.0, samples=200,
            source="acceptance", detail=CONTROL_PROOF,
        )
        self.store.update_agent(self.root["id"], {"mode": "shadow"})
        self.root = self.store.get_agent_config(self.root["id"])

        self.states = {
            "light.acceptance": {"entity_id": "light.acceptance", "state": "off", "attributes": {}, "context": {}},
            "binary_sensor.presence": {"entity_id": "binary_sensor.presence", "state": "off", "attributes": {"device_class": "occupancy"}},
            "binary_sensor.other": {"entity_id": "binary_sensor.other", "state": "off", "attributes": {"device_class": "motion"}},
            "binary_sensor.new_presence": {"entity_id": "binary_sensor.new_presence", "state": "off", "attributes": {"device_class": "motion"}},
            "automation.was_on": {"entity_id": "automation.was_on", "state": "on", "attributes": {}},
            "automation.user_off": {"entity_id": "automation.user_off", "state": "off", "attributes": {}},
        }
        self.knowledge = FakeKnowledge("light.acceptance")
        self.executor = FakeExecutor(self.store, self.states, self.knowledge)
        self.queue = FakeQueue()
        self.engine = SimpleNamespace(
            teaching=FakeTeaching(),
            rl_teaching=self.rl,
            models={}, runtime={}, executor=self.executor,
            temporal_history=None, entity_registry={}, context_relevance={},
            state_map=self.states, lock=threading.RLock(),
            wake_event=SimpleNamespace(set=lambda: None),
            own_command_echo=lambda *args, **kwargs: False,
            runtime_for=lambda agent: {},
        )

        def policy(agent):
            aid = str(agent["id"])
            current = self.engine.models.get(aid)
            if current is None:
                current = AcceptancePolicy(self.store, agent)
                self.engine.models[aid] = current
            return current

        self.engine.policy = policy

        def base_process(agent, state_map, changed_entities=None):
            now = time.time()
            rt = self.engine.runtime.setdefault(str(agent["id"]), {})
            # The exact-context Candidate wrapper samples this field from inside features().
            rt["last_inference_ts"] = now
            pol = self.engine.policy(agent)
            features, _, _ = pol.features(state_map, self.engine.temporal_history, at_ts=now)
            chosen, confidence, *_ = pol.predict(features)
            rt["last_prediction"] = float(chosen["value"])
            rt["last_confidence"] = float(confidence)
            rt["last_change_origin"] = "external"
            return chosen

        self.engine.process_agent = base_process
        self.engine.experiments = Experiments(self.store, clock=lambda: time.time())
        self.engine.context_tournament = ContextTournament(self.store, self.engine)
        self.core = SimpleNamespace(
            STORE=self.store, ENGINE=self.engine, Handler=FakeHandler,
            TRAINING_QUEUE=self.queue, HISTORY=None,
        )

        manager = AgentCandidateManager(self.core, start_worker=False)
        manager = install_config_guard(manager)
        manager = install_balance(manager)
        manager = install_debounce(manager)
        manager = install_teach_status(manager)
        manager = install_lifecycle_hardening(manager)
        manager = install_conservative_correct(manager)
        manager = install_lineage(manager)
        manager = install_lineage_retention(manager)
        manager = install_lineage_guards(manager)
        manager = install_shadow_runtime(manager)
        manager = install_shadow_context(manager)
        self.manager = install_atomic_promote(manager)

        self._add_held_out_history()
        self._add_correct_label()

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _add_correct_label(self):
        agent = self.store.get_agent_config(self.root["id"])
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?,NULL)""",
                (agent["id"], 1010.0, 1000.0, 1.0, 0.0, fingerprint(agent)),
            )

    def _add_held_out_history(self):
        # 12 balanced held-out outcomes. The Correct context at t=1000 is intentionally
        # absent from this set so offline regression cannot train on its own test row.
        for i in range(12):
            actual = i % 2
            features = {0: 1.0, 2: 1.0 if actual == 0 else 0.0, 3: 1.0 if actual else 0.0}
            ts = 2000.0 + i
            self.store.archive_upsert(
                "light.acceptance", ts, "on" if actual else "off", {}, None, "acceptance"
            )
            with self.store.conn() as c:
                history_id = c.execute(
                    "SELECT id FROM entity_history WHERE entity_id=? AND ts=?",
                    ("light.acceptance", ts),
                ).fetchone()[0]
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """INSERT INTO historical_experiences
                       (agent_id,target_history_id,created_at,action_index,action_value,reward,
                        dwell_seconds,features_json,user_id)
                       VALUES(?,?,?,?,?,?,?,?,NULL)""",
                    (
                        self.root["id"], history_id, "2026-01-01T00:00:00+00:00",
                        actual, float(actual), 1.0, 1.0,
                        json.dumps({str(k): v for k, v in features.items()}),
                    ),
                )

    def _set_state(self, entity_id, state):
        self.states[entity_id] = {
            **self.states.get(entity_id, {"entity_id": entity_id, "attributes": {}}),
            "entity_id": entity_id, "state": state,
        }
        self.engine.state_map = self.states

    def _process(self):
        root = self.store.get_agent_config(self.root["id"])
        return self.engine.process_agent(root, dict(self.states))

    def _collect_alternating_pairs(self, cycles=20):
        # Seed a prediction for OFF -> ON. Presence indicates the *next* external outcome.
        self._set_state("light.acceptance", "off")
        self._set_state("binary_sensor.presence", "on")
        self._process()
        current = 0
        for _ in range(cycles * 2):
            outcome = 1 - current
            self._set_state("light.acceptance", "on" if outcome else "off")
            next_outcome = 1 - outcome
            self._set_state("binary_sensor.presence", "on" if next_outcome else "off")
            self._process()
            current = outcome

    def _mark_targeted_sensor_promoted(self, g2):
        model = self.store.get_model(g2["agent_id"])
        self.assertIsNotNone(model)
        entities = list((model.get("schema") or {}).get("entities") or [])
        if "binary_sensor.new_presence" not in entities:
            entities.append("binary_sensor.new_presence")
        model["schema"] = {"version": 11, "entities": entities}
        model["model_revision"] = "g2-targeted-r1"
        model.setdefault("selection_meta", {})["schema_revision"] = 2
        self.store.save_model(g2["agent_id"], model)
        self.engine.models.pop(g2["agent_id"], None)
        agent = self.store.get_agent_config(g2["agent_id"])
        self.engine.context_tournament.sync_agent(
            agent, active_features=entities,
            feature_scores={"binary_sensor.new_presence": 1.0}, evaluated_at=time.time(),
        )

    def test_g0_correct_g1_targeted_explore_g2_promote_control_then_shadow(self):
        # G0 -> Correct -> G1. Creation must be an exact direct-parent snapshot and the
        # conservative fine-tune may mutate only G1, never G0.
        g0_model_before = copy.deepcopy(self.store.get_model(self.root["id"]))
        correct = self.manager.workflow_correct_commit(self.root["id"])
        g1 = self.manager.lineage_status(correct["child_generation_id"])
        self.assertEqual(g1["generation_number"], 1)
        self.assertEqual(g1["parent_generation_id"], f"root:{self.root['id']}")
        self.assertEqual(self.store.get_model(g1["agent_id"]), g0_model_before)
        self.assertTrue(self.manager._start_build(self.manager._candidate_row(self.root["id"])))
        self.assertEqual(self.store.get_model(self.root["id"]), g0_model_before)
        self.assertTrue(self.manager.status(self.root["id"])["offline_gate_passed"])
        self.assertEqual(self.store.get_model(g1["agent_id"])["toy_mapping"]["1"], 1)

        # Real observed Shadow event for G0/G1. Correct chart must read the stored event,
        # compare only the direct parent, and remain unchanged if today's G1 policy changes.
        self._set_state("light.acceptance", "off")
        self._set_state("binary_sensor.presence", "on")
        self._process()
        with self.store.conn() as c:
            observed = c.execute(
                """SELECT MIN(ts),MAX(ts) FROM candidate_generation_decisions
                   WHERE generation_id=?""",
                (g1["generation_id"],),
            ).fetchone()
        self.assertIsNotNone(observed[0])
        start, end = float(observed[0]) - .1, float(observed[1]) + .1
        chart_before = self.manager.workflow_correct_history(g1["generation_id"], start, end)
        self.assertEqual(chart_before["parent_generation_id"], f"root:{self.root['id']}")
        self.assertFalse(chart_before["policy_replay_used"])
        recorded_desired = [x["value"] for x in chart_before["series"]["candidate_desired"]["points"]]
        self.assertTrue(recorded_desired)
        g1_model = self.store.get_model(g1["agent_id"])
        changed = copy.deepcopy(g1_model)
        changed["toy_mapping"]["1"] = 0
        self.store.save_model(g1["agent_id"], changed)
        self.engine.models.pop(g1["agent_id"], None)
        chart_after = self.manager.workflow_correct_history(g1["generation_id"], start, end)
        self.assertEqual(
            [x["value"] for x in chart_after["series"]["candidate_desired"]["points"]],
            recorded_desired,
        )
        self.store.save_model(g1["agent_id"], g1_model)
        self.engine.models.pop(g1["agent_id"], None)

        # Future G1 vs G0 uses one shared event/outcome and enough balanced future evidence
        # to make the edge promotable without ever dispatching from Candidate.
        self._collect_alternating_pairs(20)
        g1_status = self.manager.status(self.root["id"])
        self.assertTrue(g1_status["promotable"])
        with self.store.conn() as c:
            g1_pairs = [dict(r) for r in c.execute(
                """SELECT * FROM candidate_generation_pairs
                   WHERE child_generation_id=? ORDER BY outcome_ts""",
                (g1["generation_id"],),
            ).fetchall()]
        self.assertGreaterEqual(len(g1_pairs), 40)
        self.assertTrue(all(r["parent_generation_id"] == f"root:{self.root['id']}" for r in g1_pairs))

        # Targeted Explore from G1 must create G2 from the exact G1 snapshot. The selected
        # sensor is initially only a challenger; after the existing Tournament makes it
        # active, Explore finalizes the same offline safety gate before future A/B.
        g1_model_before_g2 = copy.deepcopy(self.store.get_model(g1["agent_id"]))
        explore = self.manager.workflow_explore(g1["generation_id"], {
            "mode": "targeted_sensor", "sensor_entity": "binary_sensor.new_presence",
        })
        g2 = self.manager.lineage_status(explore["child_generation_id"])
        self.assertEqual(g2["generation_number"], 2)
        self.assertEqual(g2["parent_generation_id"], g1["generation_id"])
        self.assertEqual(self.store.get_model(g2["agent_id"]), g1_model_before_g2)
        tournament = self.engine.context_tournament.state(g2["agent_id"])
        self.assertIn("binary_sensor.new_presence", tournament["challenger_features"])
        self.assertNotIn("binary_sensor.new_presence", tournament["active_features"])
        self._mark_targeted_sensor_promoted(g2)
        explore_status = self.manager.workflow_explore_status(g1["generation_id"])
        self.assertEqual(explore_status["session"]["status"], "complete")
        self.assertTrue(self.manager.status(g1["agent_id"])["offline_gate_passed"])

        # G2 vs G1 must be direct-parent paired evidence. Both policies now predict the
        # alternating future correctly; root G0 is present only as Live runtime context and
        # is never substituted as G2's comparison parent.
        self._collect_alternating_pairs(20)
        g2_status = self.manager.status(g1["agent_id"])
        self.assertTrue(g2_status["promotable"])
        with self.store.conn() as c:
            g2_pairs = [dict(r) for r in c.execute(
                """SELECT * FROM candidate_generation_pairs
                   WHERE child_generation_id=? ORDER BY outcome_ts""",
                (g2["generation_id"],),
            ).fetchall()]
        self.assertGreaterEqual(len(g2_pairs), 40)
        self.assertTrue(all(r["parent_generation_id"] == g1["generation_id"] for r in g2_pairs))

        # Promote G2 as Control. The exact Candidate generation_id becomes Live atomically;
        # G0 observed history stays under its old immutable generation_id. Control takeover
        # owns only the automation that was ON before takeover.
        result = self.manager.promote(g2["generation_id"], "control")
        self.assertEqual(result["generation"], 2)
        self.assertEqual(result["generation_id"], g2["generation_id"])
        self.assertEqual(result["mode"], "control")
        live = self.store.get_agent_config(self.root["id"])
        self.assertEqual(live["mode"], "control")
        self.assertEqual(self.store.get_model(self.root["id"])["model_revision"], "g2-targeted-r1")
        self.assertIsNone(self.store.get_agent_config(g2["agent_id"]))
        with self.store.conn() as c:
            live_gen = dict(c.execute(
                """SELECT * FROM agent_candidate_generations
                   WHERE generation_type='live' AND agent_id=?""",
                (self.root["id"],),
            ).fetchone())
            old_g0 = dict(c.execute(
                "SELECT * FROM agent_candidate_generations WHERE generation_id=?",
                (f"root:{self.root['id']}",),
            ).fetchone())
        self.assertEqual(live_gen["generation_id"], g2["generation_id"])
        self.assertEqual(live_gen["generation_number"], 2)
        self.assertIsNone(old_g0["agent_id"])
        lease = self.executor.handoff.journal.get("light.acceptance")
        self.assertEqual(lease["disabled_automations"], ["automation.was_on"])
        self.assertEqual(self.states["automation.was_on"]["state"], "off")
        self.assertEqual(self.states["automation.user_off"]["state"], "off")

        # Control -> Shadow restores exactly HomeMind's lease and nothing that the user had
        # already disabled. The logical Root remains G2 and ownership metadata is cleared.
        restored = self.executor.release_control(live, reason="acceptance_control_to_shadow")
        self.store.update_agent(self.root["id"], {"mode": "shadow"})
        self.assertEqual(restored, ["automation.was_on"])
        self.assertEqual(self.states["automation.was_on"]["state"], "on")
        self.assertEqual(self.states["automation.user_off"]["state"], "off")
        self.assertIsNone(self.executor.handoff.journal.get("light.acceptance"))
        self.assertEqual(self.store.get_agent_config(self.root["id"])["mode"], "shadow")

        # Architecture invariant: Candidate paths never reached any physical dispatch API.
        self.executor.submit.assert_not_called()
        self.executor.service.assert_not_called()
        self.executor._service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
