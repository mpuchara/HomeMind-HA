import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

import agent_candidate_preference_metrics as pref


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row

    @contextmanager
    def conn(self):
        yield self.db


class CandidatePreferenceMetricTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        with self.store.conn() as c:
            c.executescript(
                '''
                CREATE TABLE agent_candidate_generations (
                    generation_id TEXT PRIMARY KEY,
                    root_agent_id TEXT NOT NULL,
                    agent_id TEXT,
                    parent_generation_id TEXT,
                    created_ts REAL NOT NULL
                );
                CREATE TABLE candidate_generation_pairs (
                    root_agent_id TEXT NOT NULL,
                    parent_generation_id TEXT NOT NULL,
                    child_generation_id TEXT NOT NULL,
                    prediction_event_id TEXT NOT NULL,
                    prediction_ts REAL NOT NULL,
                    outcome_ts REAL NOT NULL,
                    outcome REAL NOT NULL,
                    parent_prediction REAL NOT NULL,
                    child_prediction REAL NOT NULL,
                    parent_confidence REAL,
                    child_confidence REAL,
                    parent_correct INTEGER NOT NULL,
                    child_correct INTEGER NOT NULL,
                    paired_result TEXT NOT NULL,
                    parent_lead_seconds REAL,
                    child_lead_seconds REAL,
                    lead_gain_seconds REAL
                );
                CREATE TABLE candidate_generation_decisions (
                    root_agent_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    current REAL,
                    desired REAL,
                    confidence REAL,
                    model_revision TEXT,
                    schema_revision TEXT
                );
                CREATE TABLE teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT,
                    undone_ts REAL
                );
                '''
            )
            c.execute(
                'INSERT INTO agent_candidate_generations VALUES(?,?,?,?,?)',
                ('candidate:child', 'root', 'child', 'root:parent', 100.0),
            )
        self.manager = SimpleNamespace(store=self.store)
        self.row = {'candidate_id': 'child', 'parent_agent_id': 'parent', 'comparison_started_ts': 100.0}
        self.parent = {'id': 'parent', 'target_entity': 'light.bathroom', 'target_property': 'power'}
        self.child = {'id': 'child', 'training_state': 'qualified'}

    def _decision(self, generation, ts, desired, current=1.0):
        with self.store.conn() as c:
            c.execute(
                'INSERT INTO candidate_generation_decisions VALUES(?,?,?,?,?,?,?,?,?)',
                ('root', generation, f'{generation}:{ts}', float(ts), float(current), float(desired), .8, 'm', 's'),
            )

    def _pair(self, outcome_ts, outcome, p_ok, c_ok, p_lead=0.0, c_lead=0.0):
        with self.store.conn() as c:
            c.execute(
                '''INSERT INTO candidate_generation_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    'root', 'root:parent', 'candidate:child', f'e:{outcome_ts}', outcome_ts - 1,
                    outcome_ts, outcome, outcome, outcome, .8, .8, int(p_ok), int(c_ok),
                    'both_correct' if p_ok and c_ok else 'child_win' if c_ok else 'parent_win',
                    p_lead, c_lead, c_lead - p_lead,
                ),
            )

    def test_observed_history_recovers_long_off_lead_beyond_legacy_30_second_cap(self):
        self._decision('candidate:child', 120.0, 0.0)
        self._decision('candidate:child', 150.0, 0.0)
        self._decision('candidate:child', 180.0, 0.0)
        self._decision('root:parent', 190.0, 0.0)
        self._pair(200.0, 0.0, True, True, p_lead=10.0, c_lead=30.0)

        metrics = pref._fast_metrics(
            self.manager, self.row, self.parent, self.child, {},
            {'teach_fit_total': 1, 'teach_fit_after_count': 1},
        )
        self.assertAlmostEqual(metrics['fast_off_candidate_lead_seconds'], 80.0, places=4)
        self.assertAlmostEqual(metrics['fast_off_parent_lead_seconds'], 10.0, places=4)
        self.assertGreater(metrics['timing_objective_gain'], 0.0)

    def test_old_manual_correction_decays_by_later_opportunities_not_wall_clock(self):
        half_life = 20.0
        now_weight = 2.0 * (0.5 ** (0.0 / half_life))
        after_twenty = 2.0 * (0.5 ** (20.0 / half_life))
        self.assertAlmostEqual(now_weight, 2.0)
        self.assertAlmostEqual(after_twenty, 1.0)

    def test_preference_confidence_strengthens_with_repeated_confirmed_opportunities(self):
        def score(n):
            success = sum(pref._opportunity_weight(i, n, 20.0) for i in range(n))
            return pref._preference_confidence(success, 0.0)

        self.assertLess(score(4), score(8))
        self.assertLess(score(8), score(12))
        self.assertGreater(score(12), 0.70)

    def test_recent_correction_reduces_confidence_but_later_success_can_rebuild_it(self):
        success_12 = sum(pref._opportunity_weight(i, 12, 20.0) for i in range(12))
        recent = pref._preference_confidence(success_12, 2.0)
        success_32 = sum(pref._opportunity_weight(i, 32, 20.0) for i in range(32))
        aged = pref._preference_confidence(success_32, 1.0)
        self.assertGreater(aged, recent)

    def test_wrong_direction_is_failure_even_if_prediction_was_early(self):
        self.assertEqual(pref._timing_utility(False, 120.0, 120.0), -1.0)
        self.assertAlmostEqual(pref._timing_utility(True, 60.0, 120.0), 0.5)


if __name__ == '__main__':
    unittest.main()
