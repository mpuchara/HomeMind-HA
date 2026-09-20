import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import storage
from agent_candidate_lineage import _ensure_root, _register_generation, ensure_lineage_tables
from agent_candidate_shadow_runtime import ensure_shadow_tables
from agent_correct_generation_history import build_correct_history, build_correct_point


class FakeRLTeaching:
    def labels(self, agent_id):
        return []


class CorrectGenerationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / "correct-history.db")
        ensure_lineage_tables(self.store)
        ensure_shadow_tables(self.store)
        self.root = self.store.create_agent({
            "name": "History light", "target_entity": "light.history", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.root["id"], {
            "version": 10, "model_revision": "g0", "schema": {"version": 1},
            "selection_meta": {"schema_revision": 1},
        })
        self.g0 = _ensure_root(self.store, self.root["id"], 0)
        self.g1_agent = self.store.create_agent({
            "name": "History light · G1", "target_entity": "light.history", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.g1_agent["id"], {
            "version": 10, "model_revision": "g1", "schema": {"version": 1},
            "selection_meta": {"schema_revision": 1},
        })
        self.g1 = _register_generation(self.store, self.root["id"], self.g0, self.g1_agent["id"], 1, "test", "comparing")
        self.g2_agent = self.store.create_agent({
            "name": "History light · G2", "target_entity": "light.history", "target_property": "power",
            "min_value": 0, "max_value": 1, "deadband": .5, "action_interval": .25,
            "exploration_step": 1, "input_entities": ["binary_sensor.presence"],
        })
        self.store.save_model(self.g2_agent["id"], {
            "version": 10, "model_revision": "g2", "schema": {"version": 1},
            "selection_meta": {"schema_revision": 1},
        })
        self.g2 = _register_generation(self.store, self.root["id"], self.g1, self.g2_agent["id"], 2, "test", "comparing")
        self.engine = SimpleNamespace(
            rl_teaching=FakeRLTeaching(),
            policy=Mock(side_effect=AssertionError("Correct history must never replay policy")),
        )
        self.manager = SimpleNamespace(store=self.store, engine=self.engine)
        self.manager._generation = lambda agent_id: 0
        self.manager.generation_history = self.generation_history
        self.manager.generation_decision_at = self.generation_decision_at
        self.ts = time.time() - 60
        self.store.archive_batch([
            ("light.history", self.ts - 5, "off", {}, None, "test"),
            ("light.history", self.ts + 0.5, "on", {}, None, "test"),
        ])
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_history (
                  agent_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
                  PRIMARY KEY(agent_id,ts));
                """
            )
            c.execute(
                "INSERT INTO decision_history(agent_id,ts,current,desired) VALUES(?,?,?,?)",
                (self.root["id"], self.ts, 0.0, 0.0),
            )
        self.insert_observed(self.g0["generation_id"], self.ts, 0.0, 0.0, "g0")
        self.insert_observed(self.g1["generation_id"], self.ts, 0.0, 1.0, "g1")
        self.insert_observed(self.g2["generation_id"], self.ts, 0.0, 0.0, "g2")

    def tearDown(self):
        self.temp.cleanup()

    def insert_observed(self, generation_id, ts, current, desired, revision):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO candidate_generation_decisions
                   (root_agent_id,generation_id,event_id,ts,current,desired,confidence,model_revision,schema_revision)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.root["id"], generation_id, f"event-{generation_id}", ts, current, desired, .9, revision, "1"),
            )

    def generation_history(self, ref, start, end):
        with self.store.conn() as c:
            rows = [dict(r) for r in c.execute(
                """SELECT * FROM candidate_generation_decisions
                   WHERE generation_id=? AND ts>=? AND ts<=? ORDER BY ts""",
                (str(ref), float(start), float(end)),
            ).fetchall()]
        return {
            "generation_id": str(ref), "start": float(start), "end": float(end),
            "points": rows, "gaps": [],
            "desired_source": "observed_candidate_generation_shadow_runtime",
            "policy_replay_used": False,
        }

    def generation_decision_at(self, ref, timestamp):
        with self.store.conn() as c:
            row = c.execute(
                """SELECT * FROM candidate_generation_decisions
                   WHERE generation_id=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                (str(ref), float(timestamp)),
            ).fetchone()
        return dict(row) if row else None

    def test_live_chart_has_only_current_live_desired_and_correct(self):
        legacy = Mock(side_effect=AssertionError("Live Correct must not call RL/policy history"))
        result = build_correct_history(self.manager, self.g0["generation_id"], self.ts - 1, self.ts + 1, legacy)
        self.assertEqual(result["chart_mode"], "live")
        self.assertEqual(result["series_order"], ["current", "live_desired", "correct"])
        self.assertEqual(result["series"]["current"]["label"], "Current")
        self.assertEqual(result["series"]["live_desired"]["label"], "Live Desired")
        self.assertNotIn("parent_desired", result["series"])
        self.assertNotIn("candidate_desired", result["series"])
        self.assertFalse(result["policy_replay_used"])
        legacy.assert_not_called()
        self.engine.policy.assert_not_called()

    def test_live_history_reads_observed_current_and_desired_without_policy_replay(self):
        legacy = Mock(side_effect=AssertionError("policy history forbidden"))
        result = build_correct_history(
            self.manager, self.g0["generation_id"],
            self.ts - 10, self.ts + 2, legacy,
        )
        self.assertEqual(result["desired_source"], "observed_runtime_decision_history")
        self.assertEqual(result["series"]["live_desired"]["points"][0]["value"], 0.0)
        self.assertEqual(result["series"]["current"]["points"][0]["value"], 0.0)
        self.assertEqual(result["series"]["current"]["points"][-1]["value"], 1.0)
        legacy.assert_not_called()

    def test_candidate_current_comes_from_physical_history_even_when_candidate_has_no_rows(self):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "DELETE FROM candidate_generation_decisions WHERE generation_id=?",
                (self.g2["generation_id"],),
            )
        result = build_correct_history(
            self.manager, self.g2["generation_id"], self.ts - 10, self.ts + 2,
            Mock(side_effect=AssertionError("Candidate history must not replay policy")),
        )
        current = result["series"]["current"]["points"]
        candidate = result["series"]["candidate_desired"]["points"]
        self.assertTrue(current)
        self.assertEqual(current[0]["value"], 0.0)
        self.assertEqual(current[-1]["value"], 1.0)
        self.assertEqual(candidate, [])

    def test_candidate_g1_compares_only_live_g0_to_candidate_g1(self):
        result = build_correct_history(
            self.manager, self.g1["generation_id"], self.ts - 1, self.ts + 1,
            Mock(side_effect=AssertionError("Candidate history must not use legacy policy history")),
        )
        self.assertEqual(result["chart_mode"], "candidate_vs_parent")
        self.assertEqual(result["parent_generation_id"], self.g0["generation_id"])
        self.assertEqual(result["series_order"], ["current", "parent_desired", "candidate_desired", "correct"])
        self.assertEqual(result["series"]["parent_desired"]["label"], "Live G0 Desired")
        self.assertEqual(result["series"]["candidate_desired"]["label"], "Candidate G1 Desired")
        self.assertEqual(result["series"]["parent_desired"]["points"][0]["value"], 0.0)
        self.assertEqual(result["series"]["candidate_desired"]["points"][0]["value"], 1.0)
        self.engine.policy.assert_not_called()

    def test_candidate_g2_compares_only_candidate_g1_to_candidate_g2(self):
        result = build_correct_history(
            self.manager, self.g2["generation_id"], self.ts - 1, self.ts + 1,
            Mock(side_effect=AssertionError("Candidate history must not use legacy policy history")),
        )
        self.assertEqual(result["parent_generation_id"], self.g1["generation_id"])
        self.assertEqual(result["series"]["parent_desired"]["label"], "Candidate G1 Desired")
        self.assertEqual(result["series"]["candidate_desired"]["label"], "Candidate G2 Desired")
        self.assertEqual(result["series"]["parent_desired"]["points"][0]["value"], 1.0)
        self.assertEqual(result["series"]["candidate_desired"]["points"][0]["value"], 0.0)
        # Root G0 exists in the same observed table but must not become G2's chart parent.
        self.assertNotEqual(result["parent_generation_id"], self.g0["generation_id"])
        self.engine.policy.assert_not_called()

    def test_candidate_point_reports_observed_direct_parent_and_child(self):
        legacy_point = Mock(side_effect=AssertionError("Correct point must not replay policy"))
        point = build_correct_point(self.manager, self.g2["generation_id"], self.ts, legacy_point)
        self.assertEqual(point["parent_desired_label"], "Candidate G1 Desired")
        self.assertEqual(point["candidate_desired_label"], "Candidate G2 Desired")
        self.assertEqual(point["parent_desired"], 1.0)
        self.assertEqual(point["candidate_desired"], 0.0)
        self.assertFalse(point["policy_replay_used_for_desired"])
        self.assertFalse(point["parent_policy_replay_used"])
        legacy_point.assert_not_called()
        self.engine.policy.assert_not_called()


class CorrectGenerationUiContractTests(unittest.TestCase):
    def source(self, name):
        return (Path(__file__).resolve().parents[1] / "adaptive_ai" / "src" / "static" / name).read_text(encoding="utf-8")

    def test_correct_chart_uses_fixed_colors_and_exact_series(self):
        source = self.source("agent_workflow_ui.js")
        self.assertIn("current:'#73dbec'", source)
        self.assertIn("parent:'#c2a6ff'", source)
        self.assertIn("candidate:'#ff9f43'", source)
        self.assertIn("correct:'#ffd166'", source)
        self.assertIn('data-series="current"', source)
        self.assertIn('data-series="parent_desired"', source)
        self.assertIn('data-series="candidate_desired"', source)
        self.assertIn('data-series="correct"', source)
        self.assertIn("series.live_desired.label||'Live Desired'", source)
        self.assertIn("series.parent_desired.label||'Parent Desired'", source)
        self.assertIn("series.candidate_desired.label||'Candidate Desired'", source)
        self.assertIn("Correct points", source)
        self.assertNotIn("policy.predict", source)

    def test_candidate_card_keeps_comparison_minimal_and_moves_full_stats_to_details(self):
        source = self.source("candidate_ui.js")
        minimal = source.split('candidate-compare candidate-compare-minimal', 1)[1].split('</div>\n      <p class="candidate-small">', 1)[0]
        self.assertIn("parentGain(m.accuracy_gain)", minimal)
        self.assertIn("Future samples", minimal)
        self.assertIn("Timing", minimal)
        self.assertNotIn("Parent accuracy", minimal)
        self.assertNotIn("Candidate accuracy", minimal)
        self.assertNotIn("Historical regression", minimal)
        self.assertNotIn("Offline gate", minimal)
        details = source.split('<details class="candidate-details"', 1)[1].split('</details>', 1)[0]
        self.assertIn("Parent accuracy", details)
        self.assertIn("Candidate accuracy", details)
        self.assertIn("Historical regression", details)
        self.assertIn("Offline gate", details)
        self.assertIn("ON lead · Parent / Candidate", details)
        self.assertIn("OFF lead · Parent / Candidate", details)


if __name__ == "__main__":
    unittest.main()
