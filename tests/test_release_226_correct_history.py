import subprocess
import sys
import unittest
from unittest.mock import patch

import support
import test_correct_multigeneration_history as fixtures
import agent_workflow_actions as workflow
from agent_correct_generation_history import build_correct_history,build_correct_point


class CorrectReads226Tests(unittest.TestCase):
    def fixture(self):
        result=fixtures.CorrectGenerationHistoryTests(methodName='runTest');result.setUp()
        self.addCleanup(result.tearDown);return result

    def test_real_http_read_works_with_store_mutex_and_sqlite_writer_held(self):
        result=subprocess.run([sys.executable,'tools/verify_correct_history_readonly.py'],cwd=support.ROOT,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('"sqlite_writer_held": true',result.stdout)

    def test_actual_dialog_renders_without_status_and_coalesces_retries(self):
        result=subprocess.run(['node','tools/verify_correct_history_ui.js'],cwd=support.ROOT,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('"rendered_svg":true',result.stdout)

    def test_missing_root_row_is_not_created_and_model_is_not_required_to_view_evidence(self):
        f=self.fixture();a=f.store.create_agent(support.agent(target_entity='light.history'))
        with f.store.conn() as c:before=c.execute('SELECT count(*) FROM agent_candidate_generations').fetchone()[0]
        f.manager._generation=lambda aid:42
        with patch.object(f.store,'get_model',side_effect=AssertionError('Observed chart must not load a model')):
            for ref in (a['id'],'root:'+a['id']):
                body=build_correct_history(f.manager,ref,f.ts-10,f.ts+2,None)
                self.assertEqual(body['generation_id'],'root:'+a['id'])
                self.assertEqual(body['generation_number'],42)
                self.assertTrue(body['series']['current']['points'])
                self.assertFalse(body['policy_replay_used'])
                self.assertTrue(all(p['value'] is None for p in body['series']['live_desired']['points']))
        with f.store.conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM agent_candidate_generations').fetchone()[0],before)

    def test_chart_and_point_never_synchronize_metadata_or_load_models(self):
        f=self.fixture()
        with patch.object(workflow,'_sync_live_generation_metadata',side_effect=AssertionError('Read must not update lineage')),patch.object(f.store,'get_model',side_effect=AssertionError('Read must not load model')):
            for ref in (f.root['id'],f.g1['generation_id'],f.g2['generation_id']):
                body=build_correct_history(f.manager,ref,f.ts-10,f.ts+2,None)
                point=build_correct_point(f.manager,ref,f.ts,None)
                self.assertFalse(body['policy_replay_used']);self.assertFalse(point['policy_replay_used_for_desired'])
                self.assertEqual(point['current'],0.)

    def test_mutation_resolver_still_requires_a_usable_model(self):
        f=self.fixture()
        with patch.object(workflow,'_sync_live_generation_metadata',return_value=f.g0),patch.object(f.store,'get_model',return_value=None):
            with self.assertRaisesRegex(ValueError,'no usable model'):
                workflow._resolve_generation(f.manager,f.root['id'])


if __name__=='__main__':unittest.main()
