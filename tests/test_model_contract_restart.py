import tempfile
import unittest
from pathlib import Path

from storage import Store
from observation_contract import _repair_current_contract_state, POLICY_VERSION, SCHEMA_VERSION


def agent_payload():
    return {
        "name": "Restart contract",
        "target_entity": "light.restart_contract",
        "target_property": "power",
        "min_value": 0,
        "max_value": 1,
        "confidence_threshold": 0.78,
        "deadband": 0.5,
        "action_interval": 1,
        "exploration_step": 1,
        "input_entities": ["*"],
    }


def current_model():
    return {
        "version": POLICY_VERSION,
        "schema": {"version": SCHEMA_VERSION, "entities": ["binary_sensor.motion"]},
        "actions": [0.0, 1.0],
        "heads": {},
    }


class ModelContractRestartTests(unittest.TestCase):
    def make_db(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name) / "adaptive_ai.db"

    def completed_agent(self, store, score=0.95, samples=40, class_coverage=True):
        agent = store.create_agent(agent_payload())
        store.save_model(agent["id"], current_model())
        store.set_training_state(
            agent["id"], "qualified" if class_coverage and score > 0.78 else "paused",
            score=score,
            samples=samples,
            source="recorded-behaviour",
            detail={
                "threshold": 0.78,
                "minimum_samples": 12,
                "class_coverage": class_coverage,
                "reason": "fixture completed benchmark",
            },
            shadow_after_completion=True,
        )
        with store.lock, store.conn() as c:
            c.execute(
                "UPDATE agents SET training_window_start_ts=?,training_window_end_ts=?,"
                "training_cursor_ts=?,training_progress=1 WHERE id=?",
                (100.0, 200.0, 200.0, agent["id"]),
            )
        return agent["id"]

    def test_current_policy_schema_survives_store_restart(self):
        path = self.make_db()
        first = Store(path)
        aid = self.completed_agent(first)
        before = first.get_agent_config(aid)
        self.assertEqual(before["training_state"], "qualified")
        self.assertEqual(before["mode"], "shadow")

        restarted = Store(path)
        after = restarted.get_agent_config(aid)
        self.assertEqual(after["training_state"], "qualified")
        self.assertEqual(after["mode"], "shadow")
        self.assertEqual(after["training_progress"], 1.0)
        self.assertIsNotNone(after["training_cursor_ts"])

    def test_storage_still_quarantines_pre_modern_legacy_model(self):
        path = self.make_db()
        first = Store(path)
        agent = first.create_agent(agent_payload())
        with first.lock, first.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)",
                (agent["id"], '{"version":9,"schema":{"version":10}}', "legacy"),
            )

        restarted = Store(path)
        row = restarted.get_agent_config(agent["id"])
        self.assertEqual(row["training_state"], "needs_retrain")
        self.assertEqual(row["mode"], "paused")

    def test_01421_false_invalidation_is_repaired_to_qualified_shadow(self):
        path = self.make_db()
        store = Store(path)
        aid = self.completed_agent(store, score=0.95, samples=40, class_coverage=True)
        with store.lock, store.conn() as c:
            c.execute(
                "UPDATE agents SET training_state='needs_retrain',mode='paused',"
                "training_cursor_ts=NULL,training_progress=0 WHERE id=?",
                (aid,),
            )
        broken = store.get_agent_config(aid)
        self.assertEqual(broken["training_state"], "needs_retrain")
        self.assertIsNotNone(broken["benchmark_score"])

        restored = _repair_current_contract_state(store, broken)
        self.assertEqual(restored, "qualified")
        row = store.get_agent_config(aid)
        self.assertEqual(row["training_state"], "qualified")
        self.assertEqual(row["mode"], "shadow")
        self.assertEqual(row["training_cursor_ts"], 200.0)
        self.assertEqual(row["training_progress"], 1.0)

    def test_failed_benchmark_repairs_to_paused_shadow_not_control_qualified(self):
        path = self.make_db()
        store = Store(path)
        aid = self.completed_agent(store, score=0.60, samples=40, class_coverage=True)
        with store.lock, store.conn() as c:
            c.execute(
                "UPDATE agents SET training_state='needs_retrain',mode='paused',"
                "training_cursor_ts=NULL,training_progress=0 WHERE id=?",
                (aid,),
            )
        broken = store.get_agent_config(aid)
        restored = _repair_current_contract_state(store, broken)
        self.assertEqual(restored, "paused")
        row = store.get_agent_config(aid)
        self.assertEqual(row["training_state"], "paused")
        self.assertEqual(row["mode"], "shadow")

    def test_real_config_change_is_not_auto_repaired(self):
        path = self.make_db()
        store = Store(path)
        aid = self.completed_agent(store)
        store.update_agent(aid, {"min_value": 0.1})
        changed = store.get_agent_config(aid)
        self.assertEqual(changed["training_state"], "needs_retrain")
        self.assertIsNone(changed["benchmark_score"])

        restored = _repair_current_contract_state(store, changed)
        self.assertIsNone(restored)
        row = store.get_agent_config(aid)
        self.assertEqual(row["training_state"], "needs_retrain")
        self.assertEqual(row["mode"], "paused")


if __name__ == "__main__":
    unittest.main()
