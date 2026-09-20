from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_candidate_card_summary import decorate_candidate_status


class _Store:
    def __init__(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE agent_candidate_generations (
                    generation_id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    parent_generation_id TEXT
                );
                CREATE TABLE candidate_generation_decisions (
                    root_agent_id TEXT,
                    generation_id TEXT,
                    event_id TEXT,
                    ts REAL,
                    current REAL,
                    desired REAL,
                    confidence REAL,
                    model_revision TEXT,
                    schema_revision TEXT
                );
                """
            )
            c.execute(
                "INSERT INTO agent_candidate_generations VALUES('g0','live',NULL)"
            )
            c.execute(
                "INSERT INTO agent_candidate_generations VALUES('g1','candidate','g0')"
            )
        self.agents = {
            "live": {
                "id": "live", "target_property": "power", "min_value": 0.0, "max_value": 1.0
            },
            "candidate": {
                "id": "candidate", "target_property": "power", "min_value": 0.0, "max_value": 1.0
            },
        }

    @contextmanager
    def conn(self):
        # sqlite3.Connection.__exit__ commits/rolls back but does not close the handle.
        # Keep the fixture aligned with production Store.conn so Windows can unlink the
        # temporary database deterministically in tearDown.
        c = sqlite3.connect(self.tmp.name)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def get_agent_config(self, agent_id):
        return self.agents.get(str(agent_id))


class CandidateCardDecisionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()

    def tearDown(self):
        try:
            Path(self.store.tmp.name).unlink()
        except FileNotFoundError:
            pass

    def _status(self):
        return {
            "parent_agent_id": "live",
            "candidate_id": "candidate",
            "generation_id": "g1",
            "candidate_desired": 0.0,
            "candidate_confidence": 0.71,
            "shadow_current": 1.0,
        }

    def test_parent_tile_uses_latest_fresh_parent_observation(self):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g0", "shared", 90.0, 1.0, 1.0, 0.81, "p", "s"),
            )
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g1", "shared", 90.0, 1.0, 0.0, 0.71, "c", "s"),
            )
            # Card observability follows the newest still-fresh Parent decision.
            # Paired A/B evidence remains same-event-only elsewhere.
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g0", "other", 94.0, 1.0, 0.0, 0.55, "p", "s"),
            )
        out = decorate_candidate_status(self.store, self._status(), now=100.0)
        self.assertEqual(out["parent_desired"], 0.0)
        self.assertEqual(out["parent_confidence"], 0.55)
        self.assertEqual(out["parent_shadow_timestamp"], 94.0)
        self.assertFalse(out["parent_decision_paired"])
        self.assertEqual(out["parent_generation_id"], "g0")
        self.assertEqual(out["target_property"], "power")

    def test_candidate_only_passive_event_does_not_erase_fresh_parent_tile(self):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g0", "shared", 90.0, 1.0, 0.0, 0.81, "p", "s"),
            )
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g1", "shared", 90.0, 1.0, 1.0, 0.71, "c", "s"),
            )
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g1", "candidate-passive:1", 94.0, 1.0, 1.0, 0.72, "c", "s"),
            )
        out = decorate_candidate_status(self.store, self._status(), now=100.0)
        self.assertEqual(out["parent_desired"], 0.0)
        self.assertEqual(out["parent_confidence"], 0.81)
        self.assertEqual(out["parent_shadow_timestamp"], 90.0)
        self.assertFalse(out["parent_decision_paired"])

    def test_stale_shadow_decision_is_not_presented_as_current_desired(self):
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g0", "old", 1.0, 1.0, 1.0, 0.81, "p", "s"),
            )
            c.execute(
                "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                ("live", "g1", "old", 1.0, 1.0, 0.0, 0.71, "c", "s"),
            )
        out = decorate_candidate_status(self.store, self._status(), now=200.0)
        self.assertIsNone(out["parent_desired"])
        self.assertIsNone(out["parent_confidence"])


class CandidateCardUiContractTests(unittest.TestCase):
    def test_candidate_has_one_promote_action_and_four_decision_tiles(self):
        text = (SRC / "static" / "candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn("keep.textContent='Promote'", text)
        self.assertIn("button.matches('[data-promote]')||text.startsWith('promote')", text)
        self.assertIn("lifecycle.insertBefore(keep,discard||null)", text)
        self.assertIn("option.textContent='Shadow'", text)
        self.assertIn("option.textContent='Control'", text)
        self.assertIn("decisionTile('Current'", text)
        self.assertIn("decisionTile('Desired'", text)
        self.assertIn("decisionTile('Candidate Desired'", text)
        self.assertIn("decisionTile('Confidence'", text)
        self.assertIn("repeat(4,minmax(0,1fr))", text)
        self.assertIn("n>=.5?'ON':'OFF'", text)


if __name__ == "__main__":
    unittest.main()
