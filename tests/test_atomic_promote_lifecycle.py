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
from agent_candidates import ensure_tables
from agent_candidate_atomic_promote import install as install_atomic_promote
from agent_candidate_lineage import _ensure_root, _register_generation, ensure_lineage_tables
from control_handoff import ControlHandoff
from lease_journal import LeaseJournal


class FakeHandler:
    def do_GET(self):
        return None
    def do_POST(self):
        return None
    def do_DELETE(self):
        return None


class FakeKnowledge:
    def __init__(self, infos=None):
        self.infos = list(infos or [])
        self.lock = threading.RLock()
        self.automations = list(self.infos)
    def hints_for_target(self, target):
        infos = [x for x in self.infos if target in (x.get("target_entities") or [])]
        return set(), infos


class FakeHandoff:
    def __init__(self, store, states, knowledge):
        self.journal = LeaseJournal(store)
        self._states = states
        self.knowledge = knowledge
        self.disable_calls = []
        self.restore_calls = []
    def state_map(self):
        return dict(self._states)
    def refresh(self):
        return None
    def disable_one(self, eid):
        self.disable_calls.append(eid)
        self._states[eid] = {**self._states.get(eid, {}), "entity_id": eid, "state": "off"}
    def restore_one(self, eid):
        self.restore_calls.append(eid)
        self._states[eid] = {**self._states.get(eid, {}), "entity_id": eid, "state": "on"}


class FakeExecutor:
    def __init__(self, store, states, knowledge):
        self.handoff = FakeHandoff(store, states, knowledge)
        self._locks = {}
        self.take_control = Mock(side_effect=self._take)
        self.release_control = Mock(side_effect=self._release)
        self.submit = Mock(side_effect=AssertionError("Candidate target-mode preference must never dispatch"))
    def target_lock(self, entity):
        return self._locks.setdefault(entity, threading.RLock())
    def _take(self, agent, refresh=False):
        if not self.handoff.journal.get(agent["target_entity"]):
            self.handoff.journal.save(agent, [])
        return []
    def _release(self, agent, reason="mode_change"):
        lease = self.handoff.journal.get(agent["target_entity"])
        if lease:
            for eid in lease.get("disabled_automations") or []:
                self.handoff.restore_one(eid)
            self.handoff.journal.clear(agent["target_entity"])
        return list((lease or {}).get("disabled_automations") or [])


class FakeManager:
    def __init__(self, store, engine, root_id):
        self.store = store
        self.engine = engine
        self.core = SimpleNamespace(Handler=FakeHandler)
        self.lock = threading.RLock()
        self.root_id = str(root_id)
        self.runtime = {}
    def _candidate_row(self, parent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM agent_candidates WHERE parent_agent_id=?", (str(parent_id),)).fetchone()
        return dict(row) if row else None
    def _generation(self, agent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT generation FROM agent_generation_state WHERE agent_id=?", (str(agent_id),)).fetchone()
        return int(row[0]) if row else 0
    def status(self, parent_id):
        row = self._candidate_row(parent_id)
        if not row:
            return None
        child = None
        with self.store.conn() as c:
            child = c.execute("SELECT root_agent_id,generation_id,generation_number FROM agent_candidate_generations WHERE agent_id=?", (row["candidate_id"],)).fetchone()
        return {
            "parent_agent_id": str(parent_id), "candidate_id": row["candidate_id"],
            "root_agent_id": str(child["root_agent_id"]) if child else self.root_id,
            "generation_id": str(child["generation_id"]) if child else None,
            "generation_number": int(child["generation_number"]) if child else int(row["generation"]),
            "promotable": row["state"] == "ready" and not bool(row["dirty"]),
            "comparison": {"samples": 80, "accuracy_gain": .08, "promotable": True},
        }
    def list_status(self):
        row = self._candidate_row(self.root_id)
        return [self.status(self.root_id)] if row else []
    def lineage_status(self, ref):
        return self.status(self.root_id)


class AtomicPromoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "atomic-promote.db")
        ensure_tables(self.store)
        ensure_lineage_tables(self.store)
        self.root = self.store.create_agent({
            "name": "Atomic light", "target_entity": "light.atomic", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.root["id"], {
            "version": 10, "model_revision": "live-r0", "prediction": 0.0,
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 3},
        })
        self.store.set_training_state(self.root["id"], "qualified", score=.92, samples=100, source="test", detail={})
        self.root = self.store.get_agent_config(self.root["id"])

        self.candidate = self.store.create_agent({
            "name": "Atomic light Candidate", "target_entity": "light.atomic", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.candidate["id"], {
            "version": 10, "model_revision": "candidate-r1", "prediction": 1.0,
            "schema": {"version": 11, "entities": ["binary_sensor.presence"]},
            "selection_meta": {"schema_revision": 4},
        })
        self.store.set_training_state(self.candidate["id"], "qualified", score=.96, samples=120, source="candidate", detail={"ok": True})
        self.candidate = self.store.get_agent_config(self.candidate["id"])
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_candidates(parent_agent_id,candidate_id,generation,state,reason,
                   feedback_revision,build_revision,dirty,comparison_json,updated_ts)
                   VALUES(?,?,1,'ready','test',1,1,0,?,?)""",
                (self.root["id"], self.candidate["id"], json.dumps({"samples": 80}), now),
            )
            c.execute(
                "INSERT INTO agent_generation_state(agent_id,generation,updated_ts) VALUES(?,?,?)",
                (self.root["id"], 0, now),
            )
        root_gen = _ensure_root(self.store, self.root["id"], 0)
        _register_generation(self.store, self.root["id"], root_gen, self.candidate["id"], 1, "test", "ready")

        self.states = {
            "light.atomic": {"entity_id": "light.atomic", "state": "off", "attributes": {}},
            "automation.hm": {"entity_id": "automation.hm", "state": "off", "attributes": {}},
            "automation.user_off": {"entity_id": "automation.user_off", "state": "off", "attributes": {}},
        }
        self.knowledge = FakeKnowledge([
            {"entity_id": "automation.hm", "name": "Home controller", "enabled": False,
             "target_entities": ["light.atomic"], "config_status": "fresh"},
            {"entity_id": "automation.user_off", "name": "User disabled controller", "enabled": False,
             "target_entities": ["light.atomic"], "config_status": "cached"},
        ])
        self.executor = FakeExecutor(self.store, self.states, self.knowledge)
        self.engine = SimpleNamespace(
            executor=self.executor, models={self.root["id"]: object()}, runtime={}, state_map=self.states,
            lock=threading.RLock(), wake_event=SimpleNamespace(set=lambda: None),
            runtime_for=lambda agent: {},
        )
        self.manager = install_atomic_promote(FakeManager(self.store, self.engine, self.root["id"]))

    def tearDown(self):
        self.temp.cleanup()

    def set_root_mode(self, mode):
        self.store.update_agent(self.root["id"], {"mode": mode})
        self.root = self.store.get_agent_config(self.root["id"])

    def test_shadow_promote_preserves_shadow(self):
        self.set_root_mode("shadow")
        result = self.manager.promote(self.root["id"])
        live = self.store.get_agent_config(self.root["id"])
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(live["mode"], "shadow")
        self.assertEqual(self.store.get_model(self.root["id"])["model_revision"], "candidate-r1")
        self.assertEqual(self.manager._generation(self.root["id"]), 1)
        self.executor.take_control.assert_not_called()
        self.executor.release_control.assert_not_called()

    def test_control_promote_preserves_control_and_exact_lease(self):
        self.set_root_mode("control")
        self.executor.handoff.journal.save(self.root, ["automation.hm"])
        before = self.executor.handoff.journal.get("light.atomic")
        result = self.manager.promote(self.root["id"])
        after = self.executor.handoff.journal.get("light.atomic")
        self.assertEqual(result["mode"], "control")
        self.assertTrue(result["control_lease_preserved"])
        self.assertEqual(self.store.get_agent_config(self.root["id"])["mode"], "control")
        self.assertEqual(after, before)

    def test_successful_control_promote_never_restores_automations_mid_swap(self):
        self.set_root_mode("control")
        self.executor.handoff.journal.save(self.root, ["automation.hm"])
        self.manager.promote(self.root["id"])
        self.executor.release_control.assert_not_called()
        self.executor.take_control.assert_not_called()
        self.assertEqual(self.executor.handoff.restore_calls, [])
        self.assertEqual(self.executor.handoff.disable_calls, [])
        self.assertEqual(self.states["automation.hm"]["state"], "off")

    def test_failed_promote_rolls_back_entire_live_generation_and_keeps_ownership(self):
        self.set_root_mode("control")
        self.executor.handoff.journal.save(self.root, ["automation.hm"])
        old_model = self.store.get_model(self.root["id"])
        old_lease = self.executor.handoff.journal.get("light.atomic")
        cached = self.engine.models[self.root["id"]]
        self.manager._atomic_promote_before_commit = lambda detail: (_ for _ in ()).throw(RuntimeError("injected commit failure"))
        with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
            self.manager.promote(self.root["id"])
        self.assertEqual(self.store.get_model(self.root["id"]), old_model)
        self.assertEqual(self.store.get_agent_config(self.root["id"])["mode"], "control")
        self.assertEqual(self.manager._generation(self.root["id"]), 0)
        self.assertIsNotNone(self.manager._candidate_row(self.root["id"]))
        self.assertEqual(self.executor.handoff.journal.get("light.atomic"), old_lease)
        self.assertIs(self.engine.models[self.root["id"]], cached)
        self.executor.release_control.assert_not_called()
        self.executor.take_control.assert_not_called()

    def test_candidate_target_mode_control_is_only_preference_and_never_dispatches(self):
        self.set_root_mode("shadow")
        before = self.store.get_agent_config(self.candidate["id"])
        status = self.manager.set_promotion_target_mode(self.root["id"], "control")
        after = self.store.get_agent_config(self.candidate["id"])
        self.assertEqual(status["promotion_target_mode"], "control")
        self.assertEqual(status["candidate_physical_mode"], "shadow")
        self.assertFalse(status["candidate_can_dispatch"])
        self.assertEqual(before["mode"], "shadow")
        self.assertEqual(after["mode"], "shadow")
        self.executor.submit.assert_not_called()
        self.executor.take_control.assert_not_called()
        self.executor.release_control.assert_not_called()

    def test_candidate_details_expose_current_owned_and_previous_automations(self):
        self.set_root_mode("control")
        self.states["automation.user_off"]["state"] = "on"
        self.executor.handoff.journal.save(self.root, ["automation.hm"])
        status = self.manager.status(self.root["id"])
        ownership = status["automation_ownership"]
        self.assertEqual([x["entity_id"] for x in ownership["disabled_by_homemind"]], ["automation.hm"])
        self.assertEqual([x["entity_id"] for x in ownership["currently_controlling"]], ["automation.user_off"])
        self.assertEqual({x["entity_id"] for x in ownership["previously_linked"]}, {"automation.hm", "automation.user_off"})
        self.assertTrue(ownership["ownership_valid"])


class ControlReleaseOwnershipTests(unittest.TestCase):
    def test_control_to_shadow_restores_only_automations_homemind_disabled(self):
        temp = tempfile.TemporaryDirectory()
        try:
            store = storage.Store(Path(temp.name) / "handoff.db")
            root = store.create_agent({
                "name": "Lease light", "target_entity": "light.lease", "target_property": "power",
                "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
                "exploration_step": 1,
            })
            store.set_training_state(root["id"], "qualified", score=.95, samples=100, source="test", detail={})
            store.update_agent(root["id"], {"mode": "control"})
            root = store.get_agent_config(root["id"])
            states = {
                "light.lease": {"entity_id": "light.lease", "state": "off", "attributes": {}},
                "automation.was_on": {"entity_id": "automation.was_on", "state": "on", "attributes": {}},
                "automation.user_disabled": {"entity_id": "automation.user_disabled", "state": "off", "attributes": {}},
            }
            knowledge = FakeKnowledge([
                {"entity_id": "automation.was_on", "target_entities": ["light.lease"], "enabled": True},
                {"entity_id": "automation.user_disabled", "target_entities": ["light.lease"], "enabled": False},
            ])
            disabled, restored = [], []
            def disable(eid):
                disabled.append(eid)
                states[eid] = {**states[eid], "state": "off"}
            def restore(eid):
                restored.append(eid)
                states[eid] = {**states[eid], "state": "on"}
            handoff = ControlHandoff(
                store, lambda: dict(states), lambda: None, lambda: None, knowledge,
                disable, restore,
            )
            handoff.acquire(root, refresh_scan=False)
            lease = handoff.journal.get("light.lease")
            self.assertEqual(lease["disabled_automations"], ["automation.was_on"])
            self.assertEqual(disabled, ["automation.was_on"])
            handoff.release(root, "mode_change_to_shadow")
            self.assertEqual(restored, ["automation.was_on"])
            self.assertEqual(states["automation.was_on"]["state"], "on")
            self.assertEqual(states["automation.user_disabled"]["state"], "off")
            self.assertIsNone(handoff.journal.get("light.lease"))
        finally:
            temp.cleanup()


class PromoteUiContractTests(unittest.TestCase):
    def test_candidate_ui_offers_shadow_and_control_as_target_modes(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("Promote as Shadow", source)
        self.assertIn("Promote as Control", source)
        self.assertIn("promotion_target_mode", source)
        self.assertIn("candidate_physical_mode", source)

    def test_live_ui_describes_automation_ownership_categories(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "adaptive_ai/src/static/promotion_lifecycle_ui.js").read_text(encoding="utf-8")
        self.assertIn("Currently controlling target", source)
        self.assertIn("Disabled by HomeMind", source)
        self.assertIn("Previously linked", source)
        self.assertIn("only automations HomeMind disabled", source)


if __name__ == "__main__":
    unittest.main()
