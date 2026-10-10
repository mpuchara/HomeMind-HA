import unittest

import test_candidate_preference_metrics as metric_fixture
import agent_candidate_preference_metrics as pref
from additional_signal import normalize


class GoalMetricTests(unittest.TestCase):
    setUp=metric_fixture.CandidatePreferenceMetricTests.setUp
    _pair=metric_fixture.CandidatePreferenceMetricTests._pair

    def tearDown(self):
        self.store.db.close()

    def test_goal_accuracy_is_separate_from_observed_action_accuracy_and_requires_dark_proof(self):
        for i in range(12):
            self._pair(200+i,0 if i%3==2 else 1,True,i%3!=0)
        with self.store.conn() as c:
            c.execute("ALTER TABLE candidate_generation_pairs ADD COLUMN objective_outcome REAL")
            c.execute("ALTER TABLE candidate_generation_pairs ADD COLUMN objective_source TEXT")
            for i in range(12):
                if i%3==0:
                    c.execute("""UPDATE candidate_generation_pairs SET objective_outcome=0,
                        objective_source='configured_daylight_preference',child_prediction=0 WHERE outcome_ts=?""",(200+i,))
                elif i%3==1:
                    c.execute("""UPDATE candidate_generation_pairs SET objective_outcome=1,
                        objective_source='dark_observed_action' WHERE outcome_ts=?""",(200+i,))
        metrics=pref._fast_metrics(self.manager,self.row,self.parent,self.child,{})
        evidence=metrics["additional_signal_evidence"]
        self.assertEqual(evidence["bright"],4)
        self.assertEqual(evidence["dark"],4)
        self.assertEqual(evidence["candidate_dark_accuracy"],1)
        self.assertEqual(metrics["candidate_transition_accuracy"],1)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT SUM(child_correct) FROM candidate_generation_pairs").fetchone()[0],8)
        self.store.get_model=lambda _: {"version":11}
        child={**self.child,"additional_signal":normalize({"entity_id":"sensor.kitchen_light",
                 "purpose":"avoid_bright_on","threshold":40})}
        gates,_=pref._promotion_gate_report(self.manager,self.row,self.parent,child,metrics)
        self.assertTrue(gates["additional_signal_objective"]["passed"])
        metrics["additional_signal_evidence"]={**evidence,"candidate_dark_accuracy":.5}
        gates,_=pref._promotion_gate_report(self.manager,self.row,self.parent,child,metrics)
        self.assertFalse(gates["additional_signal_objective"]["passed"])
        self.assertEqual(gates["additional_signal_objective"]["custom_override"],"never")
