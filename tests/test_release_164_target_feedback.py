"""Fresh target lookup and real manual-feedback lifecycle parity."""
import copy
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import agent, state
import storage
import agent_candidates as candidates
import manual_feedback_lifecycle as lifecycle


class TargetFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agents.db"
        self.store = storage.Store(self.path)
        candidates.install_store_overlay(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, target="light.bathroom", **changes):
        payload = agent(target_entity=target) | changes
        result = self.store.create_agent(payload)
        with self.store.conn() as c:
            c.execute("UPDATE agents SET enabled=?,training_state=?,created_at=? WHERE id=?",
                      (int(payload["enabled"]), payload["training_state"],
                       payload.get("created_at", "same-time"), result["id"]))
        return self.store.get_agent_config(result["id"])


class TargetConfig164Tests(TargetFixture):
    def test_indexed_query_matches_full_enumeration_for_all_properties_and_states(self):
        for i, status in enumerate(["waiting", "paused", "training", "qualified", "needs_retrain"]):
            self.create(training_state=status, target_property="power" if i % 2 else "brightness")
        self.create(enabled=False)
        self.create("light.other")
        expected = [a for a in self.store.list_agent_configs() if a["target_entity"] == "light.bathroom"]
        self.assertEqual(self.store.list_agent_configs_for_target("light.bathroom"), expected)
        with self.store.conn() as c:
            plan = c.execute("EXPLAIN QUERY PLAN SELECT * FROM agents WHERE target_entity=? ORDER BY created_at",
                             ("light.bathroom",)).fetchall()
        self.assertIn("idx_agents_target_created", " ".join(str(tuple(r)) for r in plan))
        self.assertNotIn("TEMP B-TREE", " ".join(str(tuple(r)) for r in plan))

    def test_missing_entity_never_decodes_unrelated_large_or_malformed_details(self):
        self.create("light.other")
        with self.store.conn() as c:
            c.execute("UPDATE agents SET benchmark_detail_json=?", ('{bad unrelated JSON',))
        with patch.object(self.store, "_agent_dict", side_effect=AssertionError("unrelated decode")):
            self.assertEqual(self.store.list_agent_configs_for_target("sensor.radar"), [])

    def test_only_matching_rows_are_decoded(self):
        selected = self.create()
        for i in range(6):
            self.create("light.other_" + str(i))
        decoder = self.store._agent_dict
        with patch.object(self.store, "_agent_dict", wraps=decoder) as decode:
            rows = self.store.list_agent_configs_for_target("light.bathroom")
        self.assertEqual([r["id"] for r in rows], [selected["id"]])
        self.assertEqual(decode.call_count, 1)

    def test_external_writes_are_fresh_without_touching_process_revision(self):
        selected = self.create()
        revision = self.store._agent_index_revision
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE agents SET enabled=0,mode='paused',training_state='needs_retrain',benchmark_detail_json=? WHERE id=?",
                      ('{"new": [1, 2]}', selected["id"]))
        got = self.store.list_agent_configs_for_target("light.bathroom")[0]
        self.assertFalse(got["enabled"])
        self.assertEqual(got["training_state"], "needs_retrain")
        self.assertEqual(got["benchmark_detail"], {"new": [1, 2]})
        self.assertEqual(self.store._agent_index_revision, revision)
        got["benchmark_detail"]["new"].append(3)
        self.assertEqual(self.store.list_agent_configs_for_target("light.bathroom")[0]["benchmark_detail"], {"new": [1, 2]})

    def test_target_membership_changes_are_fresh(self):
        selected = self.create()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE agents SET target_entity='light.moved' WHERE id=?", (selected["id"],))
        self.assertEqual(self.store.list_agent_configs_for_target("light.bathroom"), [])
        self.assertEqual(self.store.list_agent_configs_for_target("light.moved")[0]["id"], selected["id"])
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("DELETE FROM agents WHERE id=?", (selected["id"],))
        self.assertEqual(self.store.list_agent_configs_for_target("light.moved"), [])

    def test_candidate_scope_hides_surrogates_and_restores_after_exception(self):
        root, child = self.create(), self.create()
        with self.store.conn() as c:
            c.execute("INSERT INTO agent_candidates(parent_agent_id,candidate_id,updated_ts) VALUES(?,?,1)",
                      (root["id"], child["id"]))
        candidates.refresh_candidate_ids_cache(self.store)
        self.assertEqual([a["id"] for a in self.store.list_agent_configs_for_target("light.bathroom")], [root["id"]])
        with self.assertRaises(RuntimeError):
            with candidates.candidate_training_scope():
                self.assertEqual(len(self.store.list_agent_configs_for_target("light.bathroom")), 2)
                raise RuntimeError("scope exit")
        self.assertEqual([a["id"] for a in self.store.list_agent_configs_for_target("light.bathroom")], [root["id"]])
        with self.store.conn() as c:
            c.execute("DELETE FROM agent_candidates")
        candidates.refresh_candidate_ids_cache(self.store)
        self.assertEqual(len(self.store.list_agent_configs_for_target("light.bathroom")), 2)

    def test_startup_migrates_existing_database_idempotently(self):
        selected = self.create()
        with self.store.conn() as c:
            c.execute("DROP INDEX idx_agents_target_created")
        reopened = storage.Store(self.path)
        again = storage.Store(self.path)
        self.assertEqual(reopened.list_agent_configs_for_target("light.bathroom"),
                         again.list_agent_configs_for_target("light.bathroom"))
        self.assertEqual(again.get_agent_config(selected["id"])["id"], selected["id"])
        with again.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='idx_agents_target_created'").fetchone()[0], 1)


class ManualBridge164Tests(TargetFixture):
    def setUp(self):
        super().setUp()
        self.journal = Mock()
        self.journal.record.return_value = {"feedback_id": "manual-164"}
        self.journal.set_status.return_value = {"feedback_id": "manual-164"}
        self.engine = SimpleNamespace(
            lock=threading.RLock(), state_map={}, runtime={}, manual_feedback_journal=self.journal,
            own_command_echo=Mock(return_value=False), set_manual_hold=Mock(),
        )
        def accept(data):
            self.engine.state_map[data["entity_id"]] = data["new_state"]
        self.engine.on_state_changed = accept
        self.core = SimpleNamespace(ENGINE=self.engine, STORE=self.store, runtime_available=lambda: True)
        lifecycle.install_runtime(self.core)

    def event(self, target="light.bathroom", value="on", **context):
        before = state(target, "off")
        after = state(target, value) | {"context": context}
        self.engine.state_map[target] = before
        self.engine.on_state_changed({"entity_id": target, "old_state": before, "new_state": after})

    def test_explicit_unrelated_sensor_updates_do_not_scan_or_decode_agents(self):
        self.create()
        with patch.object(self.store, "list_agent_configs", side_effect=AssertionError("full scan")), \
             patch.object(self.store, "_agent_dict", side_effect=AssertionError("decode")):
            self.event("sensor.radar", "25", user_id="operator")
        self.journal.record.assert_not_called()

    def test_ambiguous_automation_and_parent_contexts_do_not_query_or_learn(self):
        self.create(training_state="waiting")
        with patch.object(self.store, "list_agent_configs_for_target", side_effect=AssertionError("lookup")):
            self.event()
            self.event(user_id="operator", parent_id="automation")
        self.journal.record.assert_not_called()

    def test_same_user_fact_matches_frozen_all_agent_bridge_for_all_lifecycle_states(self):
        # Run the real wrapper twice with identical data; only the retrieval algorithm differs.
        for status in ["waiting", "paused", "needs_retrain", "qualified", "training"]:
            with self.subTest(status=status):
                chosen = self.create(training_state=status)
                self.create("light.other", training_state="waiting")
                results = []
                original_lookup = self.store.list_agent_configs_for_target
                for lookup in [lambda target: self.store.list_agent_configs(), original_lookup]:
                    self.journal.reset_mock()
                    self.engine.set_manual_hold.reset_mock()
                    self.engine.runtime.clear()
                    with patch.object(self.store, "list_agent_configs_for_target", side_effect=lookup), \
                         patch.object(lifecycle, "observe_linked_context", return_value={"observed": True}) as linked, \
                         patch.object(lifecycle.time, "time", return_value=1791579300.0):
                        self.event(user_id="operator")
                    results.append(([(call[0], copy.deepcopy(call.args), copy.deepcopy(call.kwargs))
                                     for call in self.journal.mock_calls],
                                    [(copy.deepcopy(call.args[1:]), copy.deepcopy(call.kwargs))
                                     for call in linked.mock_calls],
                                    [copy.deepcopy(call.args) for call in self.engine.set_manual_hold.mock_calls],
                                    copy.deepcopy(self.engine.runtime)))
                self.assertEqual(results[0], results[1])
                self.store.delete_agent(chosen["id"])

    def test_disabled_qualified_training_and_own_echo_cannot_record_manual_label(self):
        self.create(enabled=False, training_state="waiting")
        self.create(training_state="qualified")
        self.create(training_state="training")
        self.create(training_state="paused")
        self.engine.own_command_echo.return_value = True
        with patch.object(lifecycle, "observe_linked_context") as linked:
            self.event(user_id="operator")
        self.journal.record.assert_not_called()
        linked.assert_not_called()

    def test_real_bridge_records_each_matching_property_without_live_model_update(self):
        a = self.create(training_state="waiting", target_property="power")
        b = self.create(training_state="paused", target_property="brightness")
        with patch.object(lifecycle, "observe_linked_context", return_value={}), \
             patch.object(self.store, "save_model", side_effect=AssertionError("Live weights")):
            old = state("light.bathroom", "off", brightness=0)
            new = state("light.bathroom", "on", brightness=255) | {"context": {"user_id": "operator"}}
            self.engine.state_map["light.bathroom"] = old
            self.engine.on_state_changed({"entity_id": "light.bathroom", "new_state": new})
        self.assertEqual({call.kwargs["agent_id"] for call in self.journal.record.call_args_list},
                         {a["id"], b["id"]})
        self.assertEqual(self.journal.record.call_count, 2)

    def test_lookup_failure_retains_trace_error_and_no_label(self):
        with patch.object(self.store, "list_agent_configs_for_target", side_effect=sqlite3.OperationalError("locked")), \
             patch.object(lifecycle, "RUNTIME_DEBUG") as debug, \
             patch.object(lifecycle, "TELEMETRY") as telemetry:
            debug.enabled = True
            debug.begin.return_value = "trace"
            with self.assertRaises(sqlite3.OperationalError):
                self.event(user_id="operator")
            debug.end.assert_called_once_with("trace", status="error", error_type="OperationalError")
            telemetry.observe.assert_called_once()
        self.journal.record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
