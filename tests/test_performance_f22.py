import json
import math
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

import agent_candidate_preference_metrics as pref
import agent_candidate_shadow_runtime as shadow
import performance_f22 as f22
import confidence_contract as confidence
import teaching_rl
from settings import OPTIONS


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.statements = []
        self.db.set_trace_callback(self.statements.append)

    @contextmanager
    def conn(self):
        with self.db:
            yield self.db

    def reset_trace(self):
        self.statements.clear()

    def selects(self):
        return [x for x in self.statements if x.lstrip().upper().startswith(("SELECT", "WITH"))]

    def event(self, *args, **kwargs):
        return None


class FastMetricFixture:
    @staticmethod
    def schema(store):
        with store.conn() as c:
            c.executescript(
                """
                CREATE TABLE agent_candidate_generations (
                    generation_id TEXT PRIMARY KEY, root_agent_id TEXT NOT NULL,
                    agent_id TEXT, parent_generation_id TEXT, created_ts REAL NOT NULL,
                    comparison_json TEXT DEFAULT '{}', lifecycle_state TEXT DEFAULT 'comparing',
                    updated_ts REAL DEFAULT 0
                );
                CREATE TABLE candidate_generation_pairs (
                    root_agent_id TEXT NOT NULL, parent_generation_id TEXT NOT NULL,
                    child_generation_id TEXT NOT NULL, prediction_event_id TEXT NOT NULL,
                    prediction_ts REAL NOT NULL, outcome_ts REAL NOT NULL, outcome REAL NOT NULL,
                    parent_prediction REAL NOT NULL, child_prediction REAL NOT NULL,
                    parent_confidence REAL, child_confidence REAL,
                    parent_correct INTEGER NOT NULL, child_correct INTEGER NOT NULL,
                    paired_result TEXT NOT NULL, parent_lead_seconds REAL,
                    child_lead_seconds REAL, lead_gain_seconds REAL,
                    PRIMARY KEY(parent_generation_id,child_generation_id,outcome_ts)
                );
                CREATE TABLE candidate_generation_decisions (
                    root_agent_id TEXT NOT NULL, generation_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
                    confidence REAL, model_revision TEXT, schema_revision TEXT,
                    PRIMARY KEY(generation_id,ts)
                );
                CREATE TABLE candidate_generation_comparisons (
                    parent_generation_id TEXT NOT NULL, child_generation_id TEXT NOT NULL,
                    root_agent_id TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}',
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(parent_generation_id,child_generation_id)
                );
                CREATE TABLE agent_candidates (
                    parent_agent_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    comparison_json TEXT DEFAULT '{}', state TEXT DEFAULT 'comparing',
                    updated_ts REAL DEFAULT 0
                );
                CREATE TABLE teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL, sample_ts REAL NOT NULL, desired REAL NOT NULL,
                    previous_desired REAL, fingerprint TEXT, undone_ts REAL
                );
                CREATE TABLE entity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL, ts REAL NOT NULL,
                    state TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', context_user_id TEXT,
                    source TEXT NOT NULL DEFAULT 'test', UNIQUE(entity_id,ts)
                );
                CREATE TABLE adaptation_regression_anchors (
                    agent_id TEXT NOT NULL, episode_id TEXT NOT NULL, reason TEXT NOT NULL,
                    retained_ts REAL NOT NULL, training_weight REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(agent_id,episode_id)
                );
                """
            )
            c.execute(
                "INSERT INTO agent_candidate_generations(generation_id,root_agent_id,agent_id,parent_generation_id,created_ts) VALUES(?,?,?,?,?)",
                ("g1", "root", "child", "g0", 100.0),
            )
            c.execute(
                "INSERT INTO agent_candidate_generations(generation_id,root_agent_id,agent_id,parent_generation_id,created_ts) VALUES(?,?,?,?,?)",
                ("g0", "root", "parent", None, 50.0),
            )
            c.execute("INSERT INTO agent_candidates(parent_agent_id,candidate_id) VALUES('parent','child')")

    @staticmethod
    def data(store, n=120):
        with store.conn() as c:
            for i in range(n):
                outcome = float(i % 2)
                ts = 1000.0 + i * 200.0
                p_ok = (i % 7) != 0
                c_ok = (i % 9) != 0
                c.execute(
                    """INSERT INTO candidate_generation_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("root", "g0", "g1", f"e{i}", ts-1.0, ts, outcome,
                     outcome if p_ok else 1.0-outcome, outcome if c_ok else 1.0-outcome,
                     .8, .8, int(p_ok), int(c_ok),
                     "both_correct" if p_ok and c_ok else "child_win" if c_ok else "parent_win" if p_ok else "both_wrong",
                     10.0 if p_ok else None, 20.0 if c_ok else None,
                     10.0 if p_ok and c_ok else None),
                )
                # Isolated decision runs: an opposite decision stops the contiguous lead.
                for gid, lead, ok in (("g0", 10.0, p_ok), ("g1", 20.0, c_ok)):
                    desired = outcome if ok else 1.0-outcome
                    c.execute(
                        "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                        ("root", gid, f"{gid}:stop:{i}", ts-lead-5.0, outcome, 1.0-desired, .7, "m", "s"),
                    )
                    c.execute(
                        "INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                        ("root", gid, f"{gid}:go:{i}", ts-lead, outcome, desired, .8, "m", "s"),
                    )


class F22ContractParityTests(unittest.TestCase):
    def test_build_info_and_runtime_source_publish_current_bounded_cost_contract(self):
        root = Path(__file__).resolve().parents[1]
        build = json.loads((root / "adaptive_ai" / "BUILD_INFO.json").read_text(encoding="utf-8"))
        self.assertEqual(build["history_cost_contract_version"], f22.CONTRACT_VERSION)
        self.assertIn("revision cache", build["history_cost_confidence_selection"])
        self.assertIn("bounded fixed-window recomputation", build["history_cost_confidence_final"])
        self.assertIn("one row", build["history_cost_confidence_selection"])
        self.assertIn("reliability-bin count", build["history_cost_probability_calibration"])
        self.assertEqual(
            build["history_cost_pi_budgets_not_measurements"]["training_queue_pending_max"],
            16,
        )
        self.assertIn("--pairs 3000", build["history_cost_pi_benchmark_command"])
        runtime = (root / "adaptive_ai" / "src" / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn('"performance"', runtime)
        self.assertIn("manager.performance_f22", runtime)


class StartupIndexTests(unittest.TestCase):
    def test_f22_does_not_build_redundant_entity_history_index(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "adaptive_ai", "src", "performance_f22.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("idx_entity_history_entity_ts_id_f22", source)
        self.assertIn("idx_entity_history_entity_ts(entity_id,ts)", source)


class FastMetricsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        FastMetricFixture.schema(self.store)
        FastMetricFixture.data(self.store)
        self.manager = SimpleNamespace(store=self.store)
        self.row = {"candidate_id": "child", "parent_agent_id": "parent", "queued_ts": 100.0}
        self.parent = {"id": "parent", "target_entity": "light.x", "target_property": "power"}
        self.child = {"id": "child", "training_state": "qualified"}
        self.status = {"teach_fit_total": 0, "teach_fit_after_count": 0}
        self.legacy = pref._fast_metrics
        self.old_flag = getattr(pref, "_f22_fast_metrics_installed", False)

    def tearDown(self):
        pref._fast_metrics = self.legacy
        pref._f22_fast_metrics_installed = self.old_flag

    def test_fast_metrics_are_equivalent_and_warm_path_is_cursor_only(self):
        self.store.reset_trace()
        before = self.legacy(self.manager, self.row, self.parent, self.child, {}, self.status)
        legacy_selects = len(self.store.selects())
        self.assertGreater(legacy_selects, 200)  # 2 observed-lead SELECTs per pair + metadata.

        f22.ensure_tables(self.store)
        diag = f22.PerformanceDiagnostics()
        pref._f22_fast_metrics_installed = False
        f22._install_fast_metrics(self.manager, diag)
        self.store.reset_trace()
        after = pref._fast_metrics(self.manager, self.row, self.parent, self.child, {}, self.status)
        for key in (
            "meaningful_opportunities", "fast_per_action_samples", "parent_transition_accuracy",
            "candidate_transition_accuracy", "timing_parent_utility", "timing_candidate_utility",
            "timing_objective_gain", "preference_success_weight", "preference_failure_weight",
            "fast_on_parent_lead_seconds", "fast_on_candidate_lead_seconds",
            "fast_off_parent_lead_seconds", "fast_off_candidate_lead_seconds",
        ):
            if isinstance(before[key], float):
                self.assertAlmostEqual(before[key], after[key], places=10, msg=key)
            else:
                self.assertEqual(before[key], after[key], key)
        optimized_first_selects = len(self.store.selects())
        self.assertLess(optimized_first_selects, legacy_selects // 5)

        self.store.reset_trace()
        warm = pref._fast_metrics(self.manager, self.row, self.parent, self.child, {}, self.status)
        self.assertEqual(warm["meaningful_opportunities"], before["meaningful_opportunities"])
        sql = "\n".join(self.store.selects()).lower()
        self.assertNotIn("order by outcome_ts", sql)
        self.assertIn("rowid>", sql)


class IncrementalSummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        FastMetricFixture.schema(self.store)
        FastMetricFixture.data(self.store, n=300)
        f22.ensure_tables(self.store)
        self.manager = SimpleNamespace(store=self.store)
        self.manager._candidate_row = lambda parent_id: {
            "parent_agent_id": "parent", "candidate_id": "child", "comparison_json": "{}"
        }
        self.manager._comparison_summary = lambda row, *args: {
            **json.loads(row.get("comparison_json") or "{}"), "promotable": False
        }
        self.edge = {"parent_generation_id": "g0", "child_generation_id": "g1",
                     "parent_agent_id": "parent", "candidate_id": "child"}
        self.legacy = shadow._rebuild_summary
        self.old_flag = getattr(shadow, "_f22_incremental_summary_installed", False)

    def tearDown(self):
        shadow._rebuild_summary = self.legacy
        shadow._f22_incremental_summary_installed = self.old_flag

    def _summary(self):
        with self.store.conn() as c:
            row = c.execute("SELECT summary_json FROM candidate_generation_comparisons WHERE parent_generation_id='g0' AND child_generation_id='g1'").fetchone()
        return json.loads(row[0])

    def test_summary_matches_legacy_then_resumes_from_durable_cursor(self):
        self.legacy(self.manager, self.edge)
        legacy_summary = self._summary()
        with self.store.conn() as c:
            c.execute("DELETE FROM candidate_generation_comparisons")
        diag = f22.PerformanceDiagnostics()
        shadow._f22_incremental_summary_installed = False
        f22._install_incremental_summary(self.manager, diag)
        shadow._rebuild_summary(self.manager, self.edge)
        incremental = self._summary()
        for key in ("samples", "parent_correct", "child_correct", "child_wins", "parent_wins",
                    "both_correct", "both_wrong", "on_events", "off_events",
                    "live_on_lead_sum", "candidate_on_lead_sum", "live_off_lead_sum",
                    "candidate_off_lead_sum", "per_action"):
            self.assertEqual(legacy_summary[key], incremental[key], key)

        # New pair is the only raw evidence that may be consumed after the cursor.
        with self.store.conn() as c:
            c.execute(
                "INSERT INTO candidate_generation_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("root", "g0", "g1", "new", 99998.0, 99999.0, 1.0, 1.0, 1.0,
                 .9, .9, 1, 1, "both_correct", 2.0, 3.0, 1.0),
            )
        self.store.reset_trace()
        shadow._rebuild_summary(self.manager, self.edge)
        self.assertEqual(self._summary()["samples"], 301)
        sql = "\n".join(self.store.selects()).lower()
        self.assertIn("rowid>", sql)
        self.assertNotIn("order by outcome_ts", sql)

        # A restart/new manager sees the durable cursor and does not double-apply.
        manager2 = SimpleNamespace(store=self.store)
        manager2._candidate_row = self.manager._candidate_row
        manager2._comparison_summary = self.manager._comparison_summary
        shadow._rebuild_summary(manager2, self.edge)
        self.assertEqual(self._summary()["samples"], 301)


class TeachBatchTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        FastMetricFixture.schema(self.store)
        self.agent = {
            "id": "teach", "target_entity": "light.t", "target_property": "power",
            "min_value": 0.0, "max_value": 1.0, "input_entities": ["*"],
        }
        fp = teaching_rl.fingerprint(self.agent)
        base = 1_700_000_000.0
        self.labels = []
        with self.store.conn() as c:
            for i in range(12):
                ts = base + i * 6 * 3600.0
                desired = float(i % 2)
                row = {"sample_ts": ts, "desired": desired, "fingerprint": fp}
                self.labels.append(row)
                for j in range(48):
                    value = desired if j < 24 else float((i + j) % 3) / 2.0
                    c.execute(
                        "INSERT INTO entity_history(entity_id,ts,state,attributes_json,source) VALUES(?,?,?,?,?)",
                        (f"sensor.s{j}", ts - (j % 5), str(value), "{}", "synthetic"),
                    )
        self.fake = SimpleNamespace(store=self.store)
        self.fake.labels = lambda agent_id: list(self.labels)
        self.fake.eligible_entities = lambda agent: [f"sensor.s{j}" for j in range(48)]
        self.fake._f22_diagnostics = f22.PerformanceDiagnostics()

    def test_batched_asof_scores_match_legacy_with_far_fewer_queries(self):
        self.store.reset_trace()
        before, before_stats = teaching_rl.RLTeaching.supervised_scores(self.fake, self.agent)
        legacy_selects = len(self.store.selects())
        self.assertGreaterEqual(legacy_selects, 96)

        self.store.reset_trace()
        after, after_stats = f22._batched_supervised_scores(self.fake, self.agent)
        self.assertEqual(before, after)
        self.assertEqual(before_stats["labels"], after_stats["labels"])
        self.assertLessEqual(len(self.store.selects()), 3)
        self.assertLessEqual(
            self.fake._f22_diagnostics.snapshot()["max_rows_materialized_per_batch"],
            f22.TEACH_CANDIDATE_CHUNK * len(self.labels),
        )


class CurrentConfidenceCostTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        FastMetricFixture.schema(self.store)
        with self.store.conn() as db:
            for ddl in (
                "ALTER TABLE candidate_generation_pairs ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'legacy_unclassified'",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_eligible INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN dependency_cluster TEXT",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_outcome REAL",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_parent_correct INTEGER",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_child_correct INTEGER",
                "ALTER TABLE candidate_generation_pairs ADD COLUMN calibration_source_id TEXT",
            ):
                db.execute(ddl)
        confidence.ensure_tables(self.store)
        self.epochs = confidence.EvaluationEpochJournal(self.store)
        self.diag = f22.PerformanceDiagnostics()
        self.epochs._performance_diagnostics = self.diag

    def _insert_pair(self, i, *, eligible=False, outcome=None, parent_ok=True, child_ok=True):
        outcome = float(i % 2 if outcome is None else outcome)
        ts = 1000.0 + float(i) * 10.0
        kind = 'manual_user_target_change' if eligible else 'external_target_transition'
        with self.store.conn() as db:
            db.execute(
                """INSERT INTO candidate_generation_pairs
                   (root_agent_id,parent_generation_id,child_generation_id,prediction_event_id,
                    prediction_ts,outcome_ts,outcome,parent_prediction,child_prediction,
                    parent_confidence,child_confidence,parent_correct,child_correct,paired_result,
                    parent_lead_seconds,child_lead_seconds,lead_gain_seconds,
                    evidence_kind,calibration_eligible,dependency_cluster,calibration_outcome,
                    calibration_parent_correct,calibration_child_correct,calibration_source_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    'root','g0','g1',f'ce-{i}',ts-1,ts,outcome,
                    outcome if parent_ok else 1.0-outcome,
                    outcome if child_ok else 1.0-outcome,
                    .8,.8,int(parent_ok),int(child_ok),
                    'both_correct' if parent_ok and child_ok else
                    'child_win' if child_ok else 'parent_win' if parent_ok else 'both_wrong',
                    1.0,1.0,0.0,
                    kind,1 if eligible else 0,f'cluster-{i}',
                    outcome if eligible else None,
                    int(parent_ok) if eligible else None,
                    int(child_ok) if eligible else None,
                    f'label-{i}' if eligible else None,
                ),
            )

    def test_streamed_selection_sufficiency_matches_legacy_dependency_weighting(self):
        # Mix ON/OFF, repeated dependency clusters and enough rows to exercise decay.
        for i in range(40):
            self._insert_pair(i, outcome=(i % 3 != 0))
        with self.store.conn() as db:
            # Force several separated observations into shared dependency clusters.
            for i in range(0, 40, 5):
                db.execute(
                    """UPDATE candidate_generation_pairs SET dependency_cluster=?
                       WHERE parent_generation_id='g0' AND child_generation_id='g1'
                         AND prediction_event_id=?""",
                    (f'shared-{i % 10}', f'ce-{i}'),
                )
        rows = confidence._selection_pair_rows(self.store, 'g0', 'g1')
        legacy = confidence.action_quality_report(
            confidence.independent_episode_rows(rows),
            scope_id=None, min_total=12, min_per_action=4,
        )
        streamed = confidence._selection_sufficiency_from_store(
            self.store, 'g0', 'g1', selection_target=12, min_per_action=4,
        )
        self.assertEqual(streamed['episodes'], legacy['episodes'])
        self.assertAlmostEqual(streamed['effective_n'], legacy['effective_n'], places=10)
        for action in ('OFF','ON'):
            self.assertEqual(
                streamed['per_action'][action]['episodes'],
                legacy['per_action'][action]['episodes'],
            )
            self.assertAlmostEqual(
                streamed['per_action'][action]['effective_n'],
                legacy['per_action'][action]['effective_n'],
                places=10,
            )
            self.assertEqual(
                streamed['per_action'][action]['sufficient_evidence'],
                legacy['per_action'][action]['sufficient_evidence'],
            )
        self.assertEqual(streamed['sufficient_evidence'], legacy['sufficient_evidence'])
        self.assertEqual(streamed['python_rows_materialized'], 1)

    def test_unchanged_insufficient_selection_uses_revision_cache_not_pair_scan(self):
        for i in range(6):
            self._insert_pair(i)
        first = self.epochs.ensure_from_store(
            'g0','g1','rev-a','diagonal_linucb:v11',
            selection_target=12,final_target=12,min_per_action=4,
        )
        self.assertIsNone(first)
        self.store.reset_trace()
        second = self.epochs.ensure_from_store(
            'g0','g1','rev-a','diagonal_linucb:v11',
            selection_target=12,final_target=12,min_per_action=4,
        )
        self.assertIsNone(second)
        sql = "\n".join(self.store.selects()).lower()
        self.assertNotIn("from candidate_generation_pairs", sql)
        self.assertGreaterEqual(self.diag.confidence_selection_cache_hits, 1)

    def test_fixed_future_report_is_exact_cached_and_ignores_unrelated_pair_growth(self):
        for i in range(12):
            self._insert_pair(i)
        epoch = self.epochs.ensure_from_store(
            'g0','g1','rev-a','diagonal_linucb:v11',
            selection_target=12,final_target=12,min_per_action=4,
        )
        self.assertIsNotNone(epoch)
        for i in range(20, 32):
            self._insert_pair(i, eligible=True)
        optimized = self.epochs.final_report_from_store(
            epoch,'g0','g1',scope_id='root'
        )
        self.assertTrue(optimized['sufficient_evidence'])
        self.assertTrue(optimized['promotion_quality_passed'])
        locked = self.epochs.get('g0','g1','rev-a','diagonal_linucb:v11')
        legacy = self.epochs.final_report(
            locked, confidence._pair_rows(self.store,'g0','g1'), scope_id='root'
        )
        for key in ('episodes','effective_n','promotion_quality_passed','status','final_end_ts'):
            self.assertEqual(optimized[key], legacy[key], key)
        self.assertEqual(optimized['paired_delta'], legacy['paired_delta'])
        self.assertEqual(optimized['per_action_delta'], legacy['per_action_delta'])

        self.store.reset_trace()
        warm = self.epochs.final_report_from_store(
            locked,'g0','g1',scope_id='root'
        )
        self.assertEqual(warm, optimized)
        self.assertNotIn("from candidate_generation_pairs", "\n".join(self.store.selects()).lower())

        # Thousands of ordinary automation transitions belong to screening history only.
        for i in range(100, 1100):
            self._insert_pair(i, eligible=False)
        self.store.reset_trace()
        after_unrelated = self.epochs.final_report_from_store(
            locked,'g0','g1',scope_id='root'
        )
        self.assertEqual(after_unrelated, optimized)
        self.assertNotIn("from candidate_generation_pairs", "\n".join(self.store.selects()).lower())

        # A later independent label invalidates the calibration revision, but the
        # recomputation is constrained to the already locked fixed-future window rather
        # than the full Candidate edge. The following warm read is cached again.
        self._insert_pair(1200, eligible=True)
        self.store.reset_trace()
        after_label = self.epochs.final_report_from_store(
            locked,'g0','g1',scope_id='root'
        )
        self.assertEqual(after_label, optimized)
        sql = "\n".join(self.store.selects()).lower()
        self.assertIn("from candidate_generation_pairs", sql)
        self.assertIn("outcome_ts<=", sql)
        self.assertLessEqual(self.diag.snapshot()['max_rows_materialized_per_batch'], 12)
        self.store.reset_trace()
        self.assertEqual(
            self.epochs.final_report_from_store(locked,'g0','g1',scope_id='root'),
            optimized,
        )
        self.assertNotIn("from candidate_generation_pairs", "\n".join(self.store.selects()).lower())

        # Durable cache survives a new journal/runtime instance.
        restarted = confidence.EvaluationEpochJournal(self.store)
        restarted._performance_diagnostics = self.diag
        self.store.reset_trace()
        restarted_report = restarted.final_report_from_store(
            restarted.get('g0','g1','rev-a','diagonal_linucb:v11'),
            'g0','g1',scope_id='root',
        )
        self.assertEqual(restarted_report, optimized)
        self.assertNotIn("from candidate_generation_pairs", "\n".join(self.store.selects()).lower())

    def test_retroactive_label_inside_locked_window_invalidates_cache_and_matches_legacy(self):
        for i in range(12):
            self._insert_pair(i)
        epoch = self.epochs.ensure_from_store(
            'g0','g1','rev-retro','diagonal_linucb:v11',
            selection_target=12,final_target=12,min_per_action=4,
        )
        for i in range(20, 32):
            self._insert_pair(i, eligible=True)
        initial = self.epochs.final_report_from_store(epoch,'g0','g1',scope_id='root')
        locked = self.epochs.get('g0','g1','rev-retro','diagonal_linucb:v11')
        self.assertIsNotNone(locked['final_end_ts'])

        # Simulate a late independent label attached to an existing pair whose outcome
        # timestamp belongs to the locked window. Old semantics would include it.
        with self.store.conn() as db:
            target = db.execute(
                """SELECT prediction_event_id FROM candidate_generation_pairs
                   WHERE parent_generation_id='g0' AND child_generation_id='g1'
                     AND outcome_ts>? AND outcome_ts<=?
                   ORDER BY outcome_ts LIMIT 1""",
                (locked['selection_cutoff_ts'], locked['final_end_ts']),
            ).fetchone()[0]
            db.execute(
                """UPDATE candidate_generation_pairs SET
                   evidence_kind='episode_evaluator_independent',
                   calibration_eligible=1,
                   calibration_outcome=1-calibration_outcome,
                   calibration_parent_correct=1,
                   calibration_child_correct=0,
                   calibration_source_id='retroactive-label'
                   WHERE parent_generation_id='g0' AND child_generation_id='g1'
                     AND prediction_event_id=?""",
                (target,),
            )

        self.store.reset_trace()
        refreshed = self.epochs.final_report_from_store(
            locked,'g0','g1',scope_id='root'
        )
        legacy = self.epochs.final_report(
            locked, confidence._pair_rows(self.store,'g0','g1'), scope_id='root'
        )
        self.assertEqual(refreshed['episodes'], legacy['episodes'])
        self.assertEqual(refreshed['paired_delta'], legacy['paired_delta'])
        self.assertEqual(refreshed['per_action_delta'], legacy['per_action_delta'])
        self.assertEqual(refreshed['promotion_quality_passed'], legacy['promotion_quality_passed'])
        self.assertIn("outcome_ts<=", "\n".join(self.store.selects()).lower())
        self.assertNotEqual(initial['paired_delta'], refreshed['paired_delta'])

    def test_streamed_probability_calibration_matches_legacy_report(self):
        journal = confidence.ProbabilityCalibrationJournal(self.store)
        journal._performance_diagnostics = self.diag
        for i in range(80):
            journal.record(
                metric_id='presence_3s', model_key='room-v2', scope_id='kitchen',
                episode_id=f'stream-p-{i}', ts=float(i * 7),
                prediction=(0.15 + 0.7 * ((i % 9) / 8.0)),
                observed=float((i % 4) != 0),
                source_kind='manual_ground_truth',
                dependency_cluster=f'pc-{i // 3}',
                independent=True,
            )
        legacy = confidence.probability_calibration(
            journal.rows('presence_3s','room-v2','kitchen'),
            scope_id='kitchen', model_key='room-v2',
        )
        optimized = journal.report('presence_3s','room-v2','kitchen')
        for key in (
            'episodes','sufficient_evidence','overconfident',
            'mean_prediction','observed_frequency','calibration_gap','brier_score',
            'effective_n',
        ):
            if isinstance(legacy[key], float):
                self.assertAlmostEqual(optimized[key], legacy[key], places=10, msg=key)
            else:
                self.assertEqual(optimized[key], legacy[key], key)
        self.assertEqual(len(optimized['reliability_bins']), len(legacy['reliability_bins']))
        for left, right in zip(optimized['reliability_bins'], legacy['reliability_bins']):
            self.assertEqual(left['episodes'], right['episodes'])
            self.assertAlmostEqual(left['weight'], right['weight'], places=10)
            for key in ('mean_prediction','observed_frequency'):
                if right[key] is None:
                    self.assertIsNone(left[key])
                else:
                    self.assertAlmostEqual(left[key], right[key], places=10)
        self.assertLessEqual(
            self.diag.snapshot()['max_rows_materialized_per_batch'],
            confidence.PROBABILITY_BINS,
        )

    def test_probability_report_reuses_durable_scope_revision_cache(self):
        journal = confidence.ProbabilityCalibrationJournal(self.store)
        journal._performance_diagnostics = self.diag
        for i in range(20):
            journal.record(
                metric_id='presence_3s', model_key='room-v2', scope_id='kitchen',
                episode_id=f'p-{i}', ts=float(i), prediction=.8 if i % 2 else .2,
                observed=float(i % 2), source_kind='manual_ground_truth',
                dependency_cluster=f'pc-{i}', independent=True,
            )
        first = journal.report('presence_3s','room-v2','kitchen')
        self.store.reset_trace()
        second = journal.report('presence_3s','room-v2','kitchen')
        self.assertEqual(first, second)
        sql = "\n".join(self.store.selects()).lower()
        self.assertNotIn("from confidence_probability_episodes", sql)
        self.assertGreaterEqual(self.diag.confidence_probability_cache_hits, 1)


class AnchorAndBackpressureTests(unittest.TestCase):
    def test_anchor_active_pool_is_bounded_without_deleting_audit_rows(self):
        store = MemoryStore()
        FastMetricFixture.schema(store)
        f22.ensure_tables(store)
        service = SimpleNamespace(store=store)

        def retain(agent_id, episode_ids, reason="pre_drift_baseline"):
            with store.lock, store.conn() as c:
                for eid in episode_ids:
                    c.execute(
                        "INSERT OR IGNORE INTO adaptation_regression_anchors(agent_id,episode_id,reason,retained_ts,training_weight) VALUES(?,?,?,?,0)",
                        (agent_id, eid, reason, time.time()),
                    )
        service.retain_regression_anchors = retain
        service.regression_anchors = lambda aid: []
        manager = SimpleNamespace(adaptation_service=service)
        f22._install_anchor_retention(manager)
        for block in range(10):
            service.retain_regression_anchors("a", [f"ep-{block}-{i}" for i in range(8)])
        with store.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM adaptation_regression_anchors WHERE agent_id='a'").fetchone()[0]
            active = c.execute("SELECT COUNT(*) FROM adaptation_regression_anchors WHERE agent_id='a' AND active=1").fetchone()[0]
        self.assertEqual(total, 80)
        self.assertEqual(active, f22.ANCHOR_ACTIVE_LIMIT)
        self.assertEqual(len(service.regression_anchors("a")), f22.ANCHOR_ACTIVE_LIMIT)
        self.assertEqual(len(service.regression_anchor_audit("a", 100)), 80)

    def test_training_queue_has_bounded_backpressure_and_dedup_path_stays_available(self):
        old = OPTIONS.get("training_queue_max_pending")
        OPTIONS["training_queue_max_pending"] = 2
        try:
            store = MemoryStore()
            queue = SimpleNamespace(
                store=store, cv=threading.Condition(threading.RLock()), jobs=[], pending={}, active=None,
            )
            def enqueue(agent_id, rebuild=False, reason="training"):
                if agent_id not in queue.pending:
                    job = {"agent_id": agent_id, "queued_at": time.time(), "rebuild": rebuild, "reason": reason}
                    queue.jobs.append(job); queue.pending[agent_id] = job
                return {"state": "queued", "agent_id": agent_id}
            queue.enqueue = enqueue
            queue.snapshot = lambda: {"active": None, "queued": list(queue.jobs), "queued_count": len(queue.jobs)}
            core = SimpleNamespace(TRAINING_QUEUE=queue)
            diag = f22.PerformanceDiagnostics()
            f22._install_queue_backpressure(core, diag)
            self.assertEqual(queue.enqueue("a")["state"], "queued")
            self.assertEqual(queue.enqueue("b")["state"], "queued")
            self.assertEqual(queue.enqueue("c")["state"], "backpressure")
            # Dedup/upgrade of an already queued agent still reaches the original queue contract.
            self.assertEqual(queue.enqueue("a", rebuild=True)["state"], "queued")
            self.assertEqual(queue.snapshot()["performance"]["training_queue_capacity"], 2)
        finally:
            if old is None:
                OPTIONS.pop("training_queue_max_pending", None)
            else:
                OPTIONS["training_queue_max_pending"] = old


if __name__ == "__main__":
    unittest.main()
