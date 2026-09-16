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
from agent_candidate_shadow_runtime import ensure_shadow_tables
import agent_candidate_user_promotion as user_promotion
import agent_candidate_blocked_shadow_evidence as stable_shadow


class StableBlockedShadowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "stable-blocked.db")
        ensure_tables(self.store)
        _ensure_gate_column(self.store)
        ensure_lineage_tables(self.store)
        ensure_shadow_tables(self.store)
        self.manager = SimpleNamespace(store=self.store)
        self.manager._candidate_row = self._candidate_row
        self.manager._comparison_summary = lambda row, *args, **kwargs: {
            "promotable": False,
            "samples": int(json.loads(row.get("comparison_json") or "{}").get("samples") or 0),
        }
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_candidate_generations
                   (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                    generation_type,parent_type,config_fingerprint,created_ts,updated_ts,lifecycle_state)
                   VALUES(?,?,?,?,?,'live',?,?,?,?,?)""",
                ("root:live", "root-live", "root-live", None, 0,
                 "live", "fp0", now, now, "live"),
            )
            c.execute(
                """INSERT INTO agent_candidate_generations
                   (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                    generation_type,parent_type,config_fingerprint,created_ts,updated_ts,lifecycle_state)
                   VALUES(?,?,?,?,?,'candidate',?,?,?,?,?)""",
                ("candidate:g1", "root-live", "g1-agent", "root:live", 1,
                 "live", "fp1", now + 1, now + 1, "offline_blocked"),
            )
            c.execute(
                """INSERT INTO agent_candidates
                   (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,
                    dirty,queued_ts,comparison_json,offline_gate_json,updated_ts)
                   VALUES(?,?,?,?,?,0,0,0,?,'{}',?,?)""",
                ("root-live", "g1-agent", 1, "offline_blocked", "candidate_correct", now,
                 json.dumps({"passed": False, "status": "failed", "reasons": ["binary one-class collapse detected"]}), now),
            )

    def tearDown(self):
        self.temp.cleanup()

    def _candidate_row(self, parent_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM agent_candidates WHERE parent_agent_id=?", (str(parent_id),)
            ).fetchone()
        return dict(row) if row else None

    def states(self):
        row = self._candidate_row("root-live")
        with self.store.conn() as c:
            lifecycle = c.execute(
                "SELECT lifecycle_state FROM agent_candidate_generations WHERE generation_id='candidate:g1'"
            ).fetchone()[0]
        return row["state"], lifecycle, json.loads(row["offline_gate_json"])

    def test_blocked_leaf_is_eligible_for_passive_pairing_without_gate_rewrite(self):
        edge = stable_shadow._active_comparison_edge(self.manager, "root-live")
        self.assertIsNotNone(edge)
        self.assertEqual(edge["state"], "offline_blocked")
        self.assertFalse(json.loads(edge["offline_gate_json"])["passed"])
        self.assertEqual(self.states()[0:2], ("offline_blocked", "offline_blocked"))

    def test_persisting_future_summary_keeps_blocked_state_stable(self):
        edge = stable_shadow._active_comparison_edge(self.manager, "root-live")
        before = self.states()
        stable_shadow._persist_summary(self.manager, edge, {
            "samples": 1,
            "parent_correct": 0,
            "child_correct": 1,
            "child_wins": 1,
            "parent_wins": 0,
            "on_events": 1,
            "off_events": 0,
        })
        after = self.states()
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[2], after[2])
        self.assertEqual(after[0], "offline_blocked")
        with self.store.conn() as c:
            saved = c.execute(
                """SELECT summary_json FROM candidate_generation_comparisons
                   WHERE parent_generation_id='root:live' AND child_generation_id='candidate:g1'"""
            ).fetchone()
        self.assertIsNotNone(saved)
        self.assertEqual(json.loads(saved[0])["samples"], 1)

    def test_installed_observation_context_does_not_toggle_persisted_state(self):
        stable_shadow.install(self.manager)
        row = self._candidate_row("root-live")
        before = self.states()
        with user_promotion._temporary_offline_gate_pass(self.manager, row, purpose="observation"):
            during = self.states()
        after = self.states()
        self.assertEqual(during, before)
        self.assertEqual(after, before)
        self.assertEqual(
            self.manager.candidate_offline_gate_observation_contract,
            "blocked_lifecycle_stays_persisted_while_passive_future_ab_evidence_collects",
        )


if __name__ == "__main__":
    unittest.main()
