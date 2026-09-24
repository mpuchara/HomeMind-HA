import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from workflow_request_queue import (
    STATE_ACCEPTED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PROCESSING,
    WorkflowRequestQueue,
)


class WorkflowRequestQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "workflow-requests.db")
        self.commit = Mock(return_value={
            "ok": True,
            "child_generation_id": "candidate:g1",
            "coalesced": False,
        })
        self.manager = SimpleNamespace(
            store=self.store,
            workflow_correct_commit=self.commit,
        )
        self.queue = WorkflowRequestQueue(
            self.manager, start_worker=False, poll_seconds=0.01
        )

    def tearDown(self):
        self.queue.stop()
        self.temp.cleanup()

    def test_correct_request_is_durable_before_candidate_orchestration(self):
        accepted = self.queue.enqueue_correct("root:abc", "req-1")
        self.assertEqual(accepted["state"], STATE_ACCEPTED)
        self.assertTrue(accepted["durable"])
        self.commit.assert_not_called()

        with self.store.conn() as c:
            row = c.execute(
                "SELECT state,generation_ref,action FROM agent_workflow_requests WHERE request_id=?",
                ("req-1",),
            ).fetchone()
        self.assertEqual(row["state"], STATE_ACCEPTED)
        self.assertEqual(row["generation_ref"], "root:abc")
        self.assertEqual(row["action"], "correct")

    def test_request_id_is_idempotent_and_cannot_be_reused_for_another_generation(self):
        first = self.queue.enqueue_correct("root:abc", "stable-id")
        second = self.queue.enqueue_correct("root:abc", "stable-id")
        self.assertEqual(first["request_id"], second["request_id"])
        with self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM agent_workflow_requests WHERE request_id=?",
                ("stable-id",),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaisesRegex(ValueError, "different workflow request"):
            self.queue.enqueue_correct("root:def", "stable-id")

    def test_worker_creates_candidate_after_http_admission_and_persists_result(self):
        self.queue.enqueue_correct("root:abc", "req-2")
        self.assertTrue(self.queue.process_once())
        self.commit.assert_called_once_with("root:abc", request_id="req-2")
        status = self.queue.status("req-2")
        self.assertEqual(status["state"], STATE_DONE)
        self.assertEqual(status["result"]["child_generation_id"], "candidate:g1")
        self.assertIsNotNone(status["finished_ts"])

    def test_processing_request_is_recovered_after_restart(self):
        self.queue.enqueue_correct("root:abc", "req-3")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "UPDATE agent_workflow_requests SET state=? WHERE request_id=?",
                (STATE_PROCESSING, "req-3"),
            )
        recovered = WorkflowRequestQueue(
            self.manager, start_worker=False, poll_seconds=0.01
        )
        try:
            self.assertEqual(recovered.status("req-3")["state"], STATE_ACCEPTED)
        finally:
            recovered.stop()

    def test_failed_orchestration_is_visible_and_does_not_lose_request(self):
        self.commit.side_effect = ValueError("Add at least one Correct point")
        self.queue.enqueue_correct("root:abc", "req-4")
        self.assertTrue(self.queue.process_once())
        status = self.queue.status("req-4")
        self.assertEqual(status["state"], STATE_FAILED)
        self.assertIn("Correct point", status["error"])


class WorkflowRequestUiContractTests(unittest.TestCase):
    def source(self, name):
        root = Path(__file__).resolve().parents[1]
        return (root / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_correct_dialog_opens_before_backend_status_read(self):
        source = self.source("static/agent_workflow_ui.js")
        block = source.split("window.openWorkflowCorrect=async generationRef=>{", 1)[1].split(
            "function zoom", 1
        )[0]
        self.assertIn("shell();dialog.showModal();", block)
        self.assertIn("await refreshSubjectAndLoad();", block)
        self.assertNotIn("subject=await status(ref)", block)

    def test_correct_apply_uses_client_request_id_and_durable_status_poll(self):
        source = self.source("static/agent_workflow_ui.js")
        self.assertIn("requestStorageKey", source)
        self.assertIn("sessionStorage.setItem", source)
        self.assertIn("request_id:requestId", source)
        self.assertIn("api/agent-workflow-requests/", source)
        self.assertIn("Correct jest zapisany trwale", source)

    def test_runtime_composition_installs_request_queue_after_generation_workflow(self):
        source = self.source("runtime_composition.py")
        self.assertIn(
            "from workflow_request_queue import install as install_workflow_request_queue",
            source,
        )
        self.assertIn(
            "manager = install_agent_workflow_actions(manager)\n"
            "        manager = install_workflow_request_queue(manager)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
