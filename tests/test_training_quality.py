"""Quality regressions for imperfect historical light demonstrations."""
import sqlite3
import importlib.util
import gc
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from agent_candidate_conservative_correct import _history_rows
from context import historical_reward
from policy import DiagonalLinUCB
from settings import OPTIONS
from training_evidence import evidence_weight_for, normalized_dwell_sample_mass


class HistoricalLightQualityTests(unittest.TestCase):
    light = {"target_entity": "light.hall", "target_property": "power"}

    def test_rapid_correction_remains_negative(self):
        self.assertLess(historical_reward(self.light, 5, None, "resident"), 0)

    def test_later_override_is_unknown_instead_of_positive_or_negative(self):
        with patch.dict(OPTIONS, {"historical_light_ambiguous_override_seconds": 90}):
            for dwell in (9, 20, 60, 90):
                with self.subTest(dwell=dwell):
                    self.assertEqual(historical_reward(self.light, dwell, None, "resident"), 0)

    def test_new_need_outside_window_is_not_assumed_to_be_a_correction(self):
        with patch.dict(OPTIONS, {"historical_light_ambiguous_override_seconds": 90}):
            self.assertGreater(historical_reward(self.light, 91, None, "resident"), 0)

    def test_short_manual_use_and_slow_actuator_are_preserved(self):
        self.assertGreater(historical_reward(self.light, 15, "resident", "resident"), 0)
        climate = {"target_entity": "climate.room", "target_property": "temperature"}
        self.assertGreater(historical_reward(climate, 60, None, "resident"), 0)

    def test_sensor_truncation_cannot_manufacture_rapid_user_correction(self):
        with patch.dict(OPTIONS, {"historical_light_ambiguous_override_seconds": 90}):
            self.assertGreater(historical_reward(
                self.light, 2, None, "resident", observed_dwell_seconds=300
            ), 0)
            self.assertEqual(historical_reward(
                self.light, 2, None, "resident", observed_dwell_seconds=20
            ), 0)

    def test_configurable_ambiguity_window_never_removes_rapid_rejection(self):
        with patch.dict(OPTIONS, {"historical_light_ambiguous_override_seconds": 8}):
            self.assertLess(historical_reward(self.light, 5, None, "resident"), 0)
            self.assertGreater(historical_reward(self.light, 20, None, "resident"), 0)


class EvidenceQualityTests(unittest.TestCase):
    def test_manual_exception_is_not_downweighted_for_being_rare(self):
        for source in ("onset", "persistence"):
            self.assertEqual(evidence_weight_for("manual_feedback", source), 1)
            self.assertGreater(evidence_weight_for("user", source),
                               evidence_weight_for("automation", source))
            self.assertGreater(evidence_weight_for("automation", source),
                               evidence_weight_for("unknown", source))

    def test_own_commands_never_become_demonstrations(self):
        for source in ("onset", "persistence", "upstream"):
            self.assertEqual(evidence_weight_for("own_command", source), 0)

    def test_upstream_is_weaker_than_local_evidence(self):
        for origin in ("user", "automation", "unknown"):
            self.assertLess(evidence_weight_for(origin, "upstream"),
                            evidence_weight_for(origin, "onset"))

    def test_more_persistence_samples_do_not_outvote_a_manual_exception(self):
        for count in (1, 3, 100):
            head = DiagonalLinUCB(1, [0, 1])
            ts = head.last_decay_ts
            for _ in range(count):
                head.update(1, {0: 1}, .8, ts,
                            sample_mass=normalized_dwell_sample_mass(count),
                            evidence_weight=evidence_weight_for("unknown", "persistence"))
            head.update(0, {0: 1}, 1, ts,
                        evidence_weight=evidence_weight_for("manual_feedback", "onset"))
            self.assertAlmostEqual(head.counts[1], .25)
            self.assertAlmostEqual(head.counts[0], 1)


class CorrectBenchmarkQualityTests(unittest.TestCase):
    def test_rejected_and_ambiguous_actions_cannot_certify_correct(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE entity_history(id INTEGER, ts REAL);
            CREATE TABLE historical_experiences(
                id INTEGER, target_history_id INTEGER, agent_id TEXT,
                action_index INTEGER, action_value REAL, features_json TEXT, reward REAL);
        ''')
        for index, reward in enumerate((-1, 0, .8, 1), 1):
            db.execute("INSERT INTO entity_history VALUES(?,?)", (index, 1700000000 + index))
            db.execute("INSERT INTO historical_experiences VALUES(?,?,?,?,?,?,?)",
                       (index, index, "agent", index % 2, index % 2, '{"0":1}', reward))

        @contextmanager
        def conn():
            yield db

        store = SimpleNamespace(conn=conn)
        rows = _history_rows(store, "agent")
        self.assertEqual([row["id"] for row in rows], [3, 4])
        rows = _history_rows(store, "agent", teach_times=[1700000003])
        self.assertEqual([row["id"] for row in rows], [4])


class IsolatedReplayQualityTests(unittest.TestCase):
    def test_later_manual_override_is_persisted_but_excluded_from_replay(self):
        # Exercise the real isolated HistoryManager/Ridge/TinyMLP path, rather than
        # asserting source strings or simulating the reward call in a fake trainer.
        path = Path(__file__).resolve().parents[1] / "tools" / "benchmark_feature_snapshot_reuse.py"
        spec = importlib.util.spec_from_file_location("quality_replay_fixture", path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path, aid, base, end, state_map, registry = fixture.seed_database(root)
            store = fixture.Store(db_path)
            store.archive_upsert("light.bench", base + 200, "off", {}, "resident", "test")
            with store.conn() as conn:
                target_id = conn.execute(
                    "SELECT id FROM entity_history WHERE entity_id=? AND ts=?",
                    ("light.bench", base + 180),
                ).fetchone()[0]
            result = fixture.run_worker(root, aid, base, end, state_map, registry, True)
            rows = result["store"].list_historical_experiences(aid, limit=10000)
            target = [row for row in rows if row["target_history_id"] == target_id]
            self.assertEqual(len(target), 1)
            self.assertEqual(target[0]["reward"], 0)
            model = result["store"].get_model(aid)
            # Both models were actually persisted by the production worker.
            self.assertTrue(model)
            self.assertIsNotNone(fixture.load_training_record(result["store"], aid))
            audit = model.get("_training_balance_audit") or {}
            # The persisted agent diagnostics contain the skipped neutral dwell.
            agent = result["store"].get_agent(aid)
            detail = agent.get("benchmark_detail") or {}
            audit = audit or detail.get("training_balance_audit") or {}
            self.assertGreaterEqual(audit["excluded_samples"]["ambiguous_user_override"], 1)
            gc.collect()


if __name__ == "__main__":
    unittest.main()
