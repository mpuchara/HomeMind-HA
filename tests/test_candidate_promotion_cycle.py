import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import storage
from agent_candidate_lineage import ensure_lineage_tables
from agent_candidate_promotion_cycle import (
    _decorate,
    candidate_base_name,
    candidate_cycle_number,
    install as install_promotion_cycle,
)
from test_atomic_promote_lifecycle import AtomicPromoteTests


ROOT = Path(__file__).resolve().parents[1]


class CandidatePromotionCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "candidate-cycle.db")
        ensure_lineage_tables(self.store)
        self.root = self.store.create_agent({
            "name": "shellyplus1pm-441793a613bc",
            "target_entity": "switch.test",
            "target_property": "power",
            "min_value": 0,
            "max_value": 1,
            "deadband": .5,
            "action_interval": .25,
            "exploration_step": 1,
        })

    def tearDown(self):
        self.temp.cleanup()

    def _lineage(self, live_generation=12):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_candidate_generations
                   (generation_id,root_agent_id,parent_generation_id,agent_id,generation_number,
                    generation_type,parent_type,config_fingerprint,created_reason,lifecycle_state,
                    created_ts,updated_ts)
                   VALUES('live-g',?,NULL,?,?,'live','live','test-fingerprint','test','live',1,1)""",
                (self.root["id"], self.root["id"], int(live_generation)),
            )

    def test_repeated_candidate_suffixes_collapse_to_one_logical_base(self):
        self.assertEqual(
            candidate_base_name("shellyplus1pm-441793a613bc · Candidate · Candidate · Candidate"),
            "shellyplus1pm-441793a613bc",
        )
        self.assertEqual(
            candidate_base_name("shellyplus1pm-441793a613bc - Candidate · Candidate"),
            "shellyplus1pm-441793a613bc",
        )

    def test_visible_generation_is_relative_to_current_promoted_live(self):
        self._lineage(live_generation=12)
        self.assertEqual(candidate_cycle_number(self.store, {
            "root_agent_id": self.root["id"], "generation_number": 13,
        }), 1)
        self.assertEqual(candidate_cycle_number(self.store, {
            "root_agent_id": self.root["id"], "generation_number": 14,
        }), 2)

    def test_card_status_uses_root_name_one_candidate_suffix_and_cycle_number(self):
        self._lineage(live_generation=12)
        manager = SimpleNamespace(store=self.store)
        out = _decorate(manager, {
            "root_agent_id": self.root["id"],
            "parent_name": "shellyplus1pm-441793a613bc · Candidate · Candidate",
            "generation_number": 13,
        })
        self.assertEqual(out["parent_name"], "shellyplus1pm-441793a613bc")
        self.assertEqual(out["candidate_name"], "shellyplus1pm-441793a613bc · Candidate")
        self.assertEqual(out["candidate_generation_number"], 1)
        self.assertEqual(out["absolute_generation_number"], 13)

    def test_child_creation_never_inherits_generated_candidate_suffix(self):
        seen = {}
        manager = SimpleNamespace(
            store=self.store,
            _create_candidate=lambda parent: seen.update(parent=dict(parent)) or {"candidate_id": "child"},
            status=lambda _ref: None,
            list_status=lambda: [],
        )
        install_promotion_cycle(manager)
        manager._create_candidate({
            "id": "hidden-parent",
            "name": "Room light · Candidate · Candidate · Candidate",
        })
        self.assertEqual(seen["parent"]["name"], "Room light")


class CandidatePromotionConsumptionTests(unittest.TestCase):
    """Reuse the production atomic-promote fixture but assert the UI postcondition."""

    def setUp(self):
        AtomicPromoteTests.setUp(self)

    def tearDown(self):
        AtomicPromoteTests.tearDown(self)

    set_root_mode = AtomicPromoteTests.set_root_mode

    def test_successful_promote_consumes_candidate_and_leaves_one_active_live_agent(self):
        self.set_root_mode("shadow")
        candidate_id = self.candidate["id"]
        with self.store.conn() as c:
            generation_id = c.execute(
                "SELECT generation_id FROM agent_candidate_generations WHERE agent_id=?",
                (candidate_id,),
            ).fetchone()[0]

        self.manager.promote(self.root["id"])

        self.assertIsNone(self.manager._candidate_row(self.root["id"]))
        self.assertEqual(self.manager.list_status(), [])
        self.assertIsNone(self.store.get_agent_config(candidate_id))
        self.assertIsNotNone(self.store.get_agent_config(self.root["id"]))
        with self.store.conn() as c:
            promoted = c.execute(
                """SELECT generation_type,lifecycle_state,agent_id
                   FROM agent_candidate_generations WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
        self.assertEqual(promoted["generation_type"], "live")
        self.assertEqual(promoted["lifecycle_state"], "live")
        self.assertEqual(promoted["agent_id"], self.root["id"])


class CandidatePromotionUiContractTests(unittest.TestCase):
    def test_title_has_one_candidate_and_uses_cycle_generation(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("candidateTitle(c)", text)
        self.assertIn("candidate_generation_number", text)
        self.assertIn("candidateBaseName", text)
        self.assertIn("(?:\\s*[·-]\\s*Candidate)+", text)

    def test_default_promote_is_explicit_confirmation_not_hidden_evidence_gate(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("minFuture:'0'", text)
        self.assertIn("minPerAction:'0'", text)
        self.assertIn("maxRegression:''", text)
        self.assertIn("allowOffline:true", text)
        self.assertIn("Hard model/config/atomic/Control qualification checks still apply", text)

    def test_confirmed_promote_removes_card_and_documents_new_cycle(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("The Candidate card will disappear after the commit", text)
        self.assertIn("the next Candidate cycle will start at Gen 1", text)
        self.assertIn("el.remove();uiState.delete(ref);await refresh()", text)
        self.assertIn("candidate/promote-custom", text)


if __name__ == "__main__":
    unittest.main()
