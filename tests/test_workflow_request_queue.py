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

    def test_worker_survives_an_external_sqlite_writer_during_claim(self):
        import sqlite3
        import time

        self.queue.enqueue_correct("root:abc", "busy")
        writer = sqlite3.connect(self.store.path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            self.queue.start()
            # The claim connection's 250 ms busy window expires while we hold
            # the independent SQLite writer. The durable row stays accepted.
            time.sleep(0.4)
            self.assertTrue(self.queue.is_alive())
            self.assertEqual(self.queue.status("busy")["state"], STATE_ACCEPTED)
            self.commit.assert_not_called()
            writer.rollback()
            deadline = time.monotonic() + 4.0
            while self.queue.status("busy")["state"] != STATE_DONE:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.025)
            self.commit.assert_called_once_with("root:abc", request_id="busy")
        finally:
            writer.close()
            self.queue.stop()
            self.queue.join(4.0)

    def test_transient_busy_during_orchestration_reuses_the_same_request(self):
        import sqlite3

        response = self.commit.return_value
        self.commit.side_effect = [sqlite3.OperationalError("database is locked"), response]
        self.queue.enqueue_correct("root:abc", "retry-busy")
        self.assertTrue(self.queue.process_once())
        self.assertEqual(self.queue.status("retry-busy")["state"], STATE_ACCEPTED)
        self.assertTrue(self.queue.process_once())
        self.assertEqual(self.queue.status("retry-busy")["state"], STATE_DONE)
        self.assertEqual([call.kwargs["request_id"] for call in self.commit.call_args_list],
                         ["retry-busy", "retry-busy"])

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
        self.assertIn("from workflow_request_queue import (", source)
        self.assertIn("install as install_workflow_request_queue", source)
        self.assertIn("register_routes as register_workflow_request_routes", source)
        workflow = source.index("manager = install_agent_workflow_actions(")
        request_queue = source.index("manager = install_workflow_request_queue(")
        self.assertLess(workflow, request_queue)
        self.assertIn("legacy_http=False, start_worker=False", source[request_queue:request_queue + 120])
        fast = self.source("fast_queue_main.py")
        self.assertIn('if not getattr(core, "_final_explicit_http_only", False):\n        candidates.start()', fast)
        final_guards = source.index("manager = install_performance_f22_order_guard(manager)")
        self.assertLess(final_guards, source.index("manager.workflow_requests.start()"))
        self.assertLess(final_guards, source.index("manager.start()"))


if __name__ == "__main__":
    unittest.main()
