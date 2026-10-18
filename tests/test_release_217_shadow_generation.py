import unittest
from unittest.mock import patch
import support
import test_candidate_shadow_runtime as shadow_tests
import agent_candidate_shadow_runtime as shadow_runtime


class ShadowGenerationSafetyTests(unittest.TestCase):
    def test_generation_change_during_prediction_cannot_publish_or_persist_result(self):
        helper=shadow_tests.CandidateShadowRuntimeTests();helper.setUp()
        self.addCleanup(helper.tearDown)
        _,generation=helper._g1();helper._run_shadow()
        before=helper.manager.candidate_latest_runtime(generation['generation_id'])
        with helper.store.conn() as c:
            count=c.execute('SELECT COUNT(*) FROM candidate_generation_decisions').fetchone()[0]
        original=shadow_runtime._predict_candidate
        def predict(*args,**kwargs):
            result=original(*args,**kwargs)
            helper.store._provenance_generation_revision+=1
            return result
        with patch.object(shadow_runtime,'_predict_candidate',side_effect=predict):
            self.assertIsNone(helper._run_shadow())
        after=helper.manager.candidate_latest_runtime(generation['generation_id'])
        self.assertEqual(after['event_id'],before['event_id'])
        with helper.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_generation_decisions').fetchone()[0],count)
        self.assertEqual(helper.manager.candidate_shadow_persistence_snapshot()['admission']['reason'],
                         'generation_changed_during_observation')
        helper.executor.service.assert_not_called()


if __name__ == '__main__': unittest.main()
