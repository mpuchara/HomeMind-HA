"""Regression coverage for one-click Correct schema recovery."""
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import ROOT  # noqa: F401 - add adaptive_ai/src to sys.path

from agent_workflow_actions import (
    LIVE_SCHEMA_INCOMPATIBLE,
    _live_schema_recovery,
)
from teaching_rl import fingerprint


class FakeStore:
    def __init__(self, model):
        self.model = model
        self.event = Mock()
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE agent_correct_operations (
               operation_id TEXT PRIMARY KEY,
               candidate_id TEXT,
               status TEXT NOT NULL,
               detail_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )

    def conn(self):
        return self._conn

    def get_model(self, agent_id):
        return self.model


class FakeQueue:
    def __init__(self, status=None):
        self.status = status
        self.enqueued = []

    def status_for(self, agent_id):
        return self.status

    def enqueue(self, agent_id, **kwargs):
        row = {
            "state": "queued",
            "agent_id": agent_id,
            **kwargs,
        }
        self.status = row
        self.enqueued.append(row)
        return row


class CorrectSchemaAutoRecoveryTests(unittest.TestCase):
    def generation(self):
        return {
            "generation_id": "root:light",
            "generation_type": "live",
            "root_agent_id": "light",
            "agent_id": "light",
        }

    def agent(self):
        return {
            "id": "light",
            "target_entity": "switch.light",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "input_entities": ["sensor.old"],
        }

    def manager(self, model, *, queue_status=None, candidate=None):
        store = FakeStore(model)
        queue = FakeQueue(queue_status)
        manager = SimpleNamespace(
            store=store,
            _queue=lambda: queue,
            _candidate_row=Mock(return_value=candidate),
            discard=Mock(return_value={"ok": True, "discarded": True}),
        )
        return manager, queue

    def test_failed_schema_candidate_is_retired_then_live_rebuild_is_queued(self):
        failed = {
            "state": "failed",
            "last_error": LIVE_SCHEMA_INCOMPATIBLE + " Train/Rebuild Live first.",
        }
        manager, queue = self.manager({"legacy": True}, candidate=failed)
        with manager.store.conn() as c:
            c.execute(
                """INSERT INTO agent_correct_operations
                   (operation_id,candidate_id,status,detail_json)
                   VALUES(?,?,?,?)""",
                ("old-request", "candidate-1", "committed", "{}"),
            )
        failed["candidate_id"] = "candidate-1"
        with patch("agent_workflow_actions._live_schema_compatible", return_value=False):
            result = _live_schema_recovery(
                manager, self.generation(), self.agent()
            )

        self.assertTrue(result["deferred"])
        self.assertEqual(result["phase"], "rebuilding_live_schema")
        manager.discard.assert_called_once_with("light")
        self.assertEqual(len(queue.enqueued), 1)
        self.assertEqual(queue.enqueued[0]["reason"], "full_rebuild")
        self.assertEqual(
            queue.enqueued[0]["rebuild_reason"],
            "incompatible_persisted_model",
        )
        self.assertGreaterEqual(manager.store.event.call_count, 2)
        with manager.store.conn() as c:
            operation = c.execute(
                "SELECT status,detail_json FROM agent_correct_operations WHERE operation_id=?",
                ("old-request",),
            ).fetchone()
        self.assertEqual(operation["status"], "failed")
        self.assertIn("reopened_for_schema_recovery", operation["detail_json"])

    def test_request_waits_while_rebuild_has_temporarily_cleared_live_model(self):
        active = {
            "state": "active",
            "agent_id": "light",
            "reason": "full_rebuild",
        }
        manager, queue = self.manager(None, queue_status=active)
        result = _live_schema_recovery(
            manager, self.generation(), self.agent()
        )
        self.assertTrue(result["deferred"])
        self.assertEqual(result["phase"], "rebuilding_live_schema")
        self.assertEqual(result["training_queue"]["state"], "active")
        self.assertEqual(queue.enqueued, [])
        manager.discard.assert_not_called()

    def test_compatible_live_model_needs_no_recovery(self):
        manager, queue = self.manager({"current": True})
        with patch("agent_workflow_actions._live_schema_compatible", return_value=True):
            result = _live_schema_recovery(
                manager, self.generation(), self.agent()
            )
        self.assertIsNone(result)
        self.assertEqual(queue.enqueued, [])
        manager.discard.assert_not_called()

    def test_correct_fingerprint_survives_input_schema_rebuild(self):
        before = self.agent()
        after = dict(before)
        after["input_entities"] = [
            "sensor.new_presence",
            "sensor.espen4_stationary_energy",
        ]
        self.assertEqual(fingerprint(before), fingerprint(after))


if __name__ == "__main__":
    unittest.main()
