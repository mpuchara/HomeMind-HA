"""Regression coverage for decoupled Correct collection and Candidate creation."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_workflow_actions import _correct_label_counts, ensure_workflow_tables


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "adaptive_ai" / "src" / "static"


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def conn(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class CorrectDeferredCandidateTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = Store(Path(self.scratch.name) / "correct.db")
        ensure_workflow_tables(self.store)
        self.manager = SimpleNamespace(store=self.store)
        self.generation = {"generation_id": "root:light"}
        self.agent = {"id": "light"}
        with self.store.conn() as c:
            c.execute(
                """CREATE TABLE teaching_rl_labels (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   agent_id TEXT NOT NULL,
                   sample_ts REAL NOT NULL,
                   created_ts REAL NOT NULL,
                   desired REAL NOT NULL,
                   fingerprint TEXT NOT NULL,
                   undone_ts REAL
                 )"""
            )

    def insert_label(self, created, fingerprint="fp", undone=False):
        with self.store.conn() as c:
            c.execute(
                """INSERT INTO teaching_rl_labels
                   (agent_id,sample_ts,created_ts,desired,fingerprint,undone_ts)
                   VALUES(?,?,?,?,?,?)""",
                ("light", created, created, 1, fingerprint, created + 1 if undone else None),
            )

    def counts(self):
        with patch("agent_workflow_actions.rl_fingerprint", return_value="fp"):
            return _correct_label_counts(self.manager, self.generation, self.agent)

    def commit_operation(self, ts):
        with self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_correct_operations
                   (operation_id,root_agent_id,parent_generation_id,parent_agent_id,
                    label_ids_json,created_ts,committed_ts,status)
                   VALUES(?,?,?,?,?,?,?,'committed')""",
                (str(ts), "light", "root:light", "light", "[]", ts - 1, ts),
            )

    def test_points_accumulate_without_a_committed_candidate(self):
        self.assertEqual(self.counts(), {"correct_labels_total": 0, "correct_labels_pending": 0})
        self.insert_label(100)
        self.insert_label(101)
        self.assertEqual(self.counts(), {"correct_labels_total": 2, "correct_labels_pending": 2})

    def test_only_new_points_are_pending_after_an_explicit_build(self):
        self.insert_label(100)
        self.insert_label(101)
        self.commit_operation(101.5)
        self.assertEqual(self.counts(), {"correct_labels_total": 2, "correct_labels_pending": 0})
        self.insert_label(105)
        self.assertEqual(self.counts(), {"correct_labels_total": 3, "correct_labels_pending": 1})

    def test_undone_and_other_fingerprint_points_do_not_count(self):
        self.insert_label(100)
        self.insert_label(101, undone=True)
        self.insert_label(102, fingerprint="other")
        self.assertEqual(self.counts(), {"correct_labels_total": 1, "correct_labels_pending": 1})

    def test_correct_ui_never_dispatches_candidate_from_point_save(self):
        source = (STATIC / "agent_workflow_ui.js").read_text(encoding="utf-8")
        candidate = (STATIC / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("window.workflowCreateCorrectCandidate=", source)
        self.assertIn("data-wf=\"create-correct\"", source)
        self.assertIn("data-wf=\"create-correct\"", candidate)
        self.assertIn("post(ref,'correct-label'", source)
        self.assertNotIn("data-apply", source)
        self.assertIn("pendingRequest(key)", source)


if __name__ == "__main__":
    unittest.main()
