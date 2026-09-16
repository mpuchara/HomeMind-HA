import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_candidates import ensure_tables
from agent_candidate_conservative_correct import _ensure_gate_column
from agent_candidate_lineage import ensure_lineage_tables
from agent_candidate_user_promotion import _active_observation_row, _temporary_offline_gate_pass


class CandidateFutureEvidenceLeafTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "future-leaf.db")
        ensure_tables(self.store)
        _ensure_gate_column(self.store)
        ensure_lineage_tables(self.store)
        self.manager = SimpleNamespace(store=self.store)
        self.manager._candidate_row = self._candidate_row
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_candidate_generations
                   (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                    generation_type,parent_type,config_fingerprint,created_ts,updated_ts,lifecycle_state)
                   VALUES(?,?,?,?,?,'candidate',?,?,?,?,?)""",
                ("candidate:g1", "root-live", "g1-agent", "root:root-live", 1,
                 "live", "fp1", now, now, "parent"),
            )
            c.execute(
                """INSERT INTO agent_candidate_generations
                   (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                    generation_type,parent_type,config_fingerprint,created_ts,updated_ts,lifecycle_state)
                   VALUES(?,?,?,?,?,'candidate',?,?,?,?,?)""",
                ("candidate:g2", "root-live", "g2-agent", "candidate:g1", 2,
                 "candidate", "fp2", now + 1, now + 1, "offline_blocked"),
            )
            c.execute(
                """INSERT INTO agent_candidates
                   (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                    dirty,queued_ts,comparison_json,offline_gate_json,updated_ts)
                   VALUES(?,?,?,?,?,0,0,0,?,'{}',?,?)""",
                ("root-live", "g1-agent", 1, "parent", "candidate_correct", now,
                 '{"passed":true,"status":"passed"}', now),
            )
            c.execute(
                """INSERT INTO agent_candidates
                   (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                    dirty,queued_ts,comparison_json,offline_gate_json,updated_ts)
                   VALUES(?,?,?,?,?,0,0,0,?,'{}',?,?)""",
                ("g1-agent", "g2-agent", 2, "offline_blocked", "candidate_correct", now + 1,
                 '{"passed":false,"status":"failed"}', now + 1),
            )

    def tearDown(self):
        self.temp.cleanup()

    def _candidate_row(self, parent_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM agent_candidates WHERE parent_agent_id=?", (str(parent_id),)
            ).fetchone()
        return dict(row) if row else None

    def test_observation_resolves_blocked_g2_leaf_not_old_root_edge(self):
        row = _active_observation_row(self.manager, "root-live")
        self.assertEqual(row["parent_agent_id"], "g1-agent")
        self.assertEqual(row["candidate_id"], "g2-agent")
        self.assertEqual(row["state"], "offline_blocked")

    def test_observation_override_opens_legacy_pairing_gate_then_restores_block(self):
        row = _active_observation_row(self.manager, "root-live")
        with _temporary_offline_gate_pass(self.manager, row, purpose="observation"):
            active = self._candidate_row("g1-agent")
            self.assertEqual(active["state"], "comparing")
            self.assertTrue(json.loads(active["offline_gate_json"])["passed"])
            with self.store.conn() as c:
                lifecycle = c.execute(
                    "SELECT lifecycle_state FROM agent_candidate_generations WHERE generation_id='candidate:g2'"
                ).fetchone()[0]
            self.assertEqual(lifecycle, "comparing")

        restored = self._candidate_row("g1-agent")
        self.assertEqual(restored["state"], "offline_blocked")
        self.assertFalse(json.loads(restored["offline_gate_json"])["passed"])
        with self.store.conn() as c:
            lifecycle = c.execute(
                "SELECT lifecycle_state FROM agent_candidate_generations WHERE generation_id='candidate:g2'"
            ).fetchone()[0]
        self.assertEqual(lifecycle, "offline_blocked")


if __name__ == "__main__":
    unittest.main()
