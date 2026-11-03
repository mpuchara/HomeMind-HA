"""Native journal/SQLite acceptance tests; no HA dispatch or production weights."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import support
sys.path.insert(0, str(support.ROOT / 'tools'))
from storage import Store
from manual_feedback_contract import ManualFeedbackJournal
from universal_training_fixture import make_native_source
from universal_checkpoint_lifecycle import (CheckpointLifecycle, NativeDatasetSource, _connection,
                                           _snapshot, _bytes)
from universal_scorer_training import TrainingConfig, context_from_sample
from universal_scorer_validation import ValidationPolicy, validate_core_artifact


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.template = Path(cls.folder.name) / 'template.db'
        store = Store(cls.template)
        cls.source,cls.split = make_native_source(store)
        service = CheckpointLifecycle(store)
        with _connection(store) as c:
            cls.data, _ = _snapshot(c, cls.source)
        result = service.rebuild(cls.source, parent_seed=1, **cls.split, config=TrainingConfig(epochs=24))
        if result['status'] != 'optimization_complete':
            raise AssertionError(result)
        cls.core = result['checkpoint']
        cls.model = result['model']
        report = validate_core_artifact(cls.core, cls.data)
        if report['status'] != 'passed':
            raise AssertionError(report)
        with store.conn() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'native.db'
        shutil.copyfile(self.template, self.path)
        self.store = Store(self.path)
        self.service = CheckpointLifecycle(self.store)

    def undo(self):
        ManualFeedbackJournal(self.store, clock=lambda:999999.).undo('synthetic-feedback:0')

    def published(self):
        result = self.service.publish(self.core, self.source)
        self.assertEqual(result['status'], 'published')
        return result['artifact_id']

    def count(self, table):
        with self.store.conn() as c:
            return c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_publish_restore_and_repeat_are_deterministic_without_new_evidence(self):
        aid = self.published()
        source = NativeDatasetSource(self.source.raw)
        restored = CheckpointLifecycle(Store(self.path)).load(aid, source)
        context = context_from_sample(self.data.samples[-1])
        self.assertEqual(restored.rank_context(context), self.model.rank_context(context))
        result = self.service.publish(self.core, source)
        self.assertEqual(result['status'], 'reproduced')
        self.assertFalse(result['independent_evidence'])
        self.assertFalse(restored.control_eligible)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT reproductions FROM universal_checkpoint_attempts').fetchone()[0], 1)
        self.assertEqual(self.count('universal_validated_checkpoints'),1)

    def test_undo_before_publication_rejects(self):
        self.undo()
        with self.assertRaisesRegex(ValueError, 'revision changed'):
            self.service.publish(self.core, self.source)
        self.assertEqual(self.count('universal_validated_checkpoints'),0)

    def test_undo_during_validation_cannot_commit_acceptance(self):
        def validate(*a, **kw):
            result = validate_core_artifact(*a, **kw); self.undo(); return result
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'changed before publication'):
                self.service.publish(self.core,self.source)
        self.assertEqual(self.count('universal_validated_checkpoints'),0)
        self.assertEqual(self.count('universal_checkpoint_attempts'),1)

    def test_undo_after_publication_revokes_previously_loaded_handle(self):
        aid = self.published(); loaded = self.service.load(aid,self.source)
        self.undo()
        with self.assertRaisesRegex(ValueError,'revoked'):
            loaded.rank_context(context_from_sample(self.data.samples[-1]))
        with self.assertRaisesRegex(ValueError,'revoked'):
            CheckpointLifecycle(Store(self.path)).load(aid,self.source)

    def test_native_revision_and_sensor_data_changes_revoke(self):
        aid=self.published()
        with self.store.conn() as c:
            c.execute("UPDATE manual_feedback_journal SET generation_id='changed'")
        with self.assertRaisesRegex(ValueError,'revoked'):self.service.load(aid,self.source)

    def test_history_changes_revoke(self):
        aid=self.published()
        with self.store.conn() as c:
            c.execute("UPDATE feature_observation_events SET state='on' WHERE event_key='motion:0'")
        with self.assertRaisesRegex(ValueError,'revoked'):self.service.load(aid,self.source)

    def test_policy_change_cannot_reuse_exposed_holdout(self):
        self.published()
        with self.assertRaisesRegex(ValueError,'holdout already exposed'):
            self.service.publish(self.core,self.source,policy=ValidationPolicy(family_alpha=.04))

    def test_model_change_cannot_reuse_exposed_holdout(self):
        from universal_training_data import _digest
        self.published(); core=copy.deepcopy(self.core)
        core['scorer']['layers']['dense2']['bias'][0] += .001
        core.pop('checksum');core['checksum']=_digest(core)
        with self.assertRaisesRegex(ValueError,'holdout already exposed'):
            self.service.publish(core,self.source)

    def test_restart_after_validation_crash_burns_pending_claims(self):
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):self.service.publish(self.core,self.source)
        service=CheckpointLifecycle(Store(self.path))
        self.assertEqual(service.recover(),1)
        self.assertEqual(service.recover(),0)
        with self.assertRaisesRegex(ValueError,'consumed or interrupted'):
            service.publish(self.core,self.source)
        self.assertEqual(self.count('universal_validated_checkpoints'),0)

    def test_write_failure_rolls_back_entire_acceptance_and_preserves_claims(self):
        with self.store.conn() as c:
            c.execute('''CREATE TRIGGER fail_acceptance BEFORE UPDATE ON universal_checkpoint_attempts
                WHEN NEW.status='published' BEGIN SELECT RAISE(ABORT,'injected write fault'); END''')
        with self.assertRaisesRegex(Exception,'injected write fault'):
            self.service.publish(self.core,self.source)
        self.assertEqual(self.count('universal_validated_checkpoints'),0)
        self.assertEqual(self.count('universal_checkpoint_attempts'),1)

    def test_corrupt_bytes_and_copied_passed_report_have_no_authority(self):
        aid=self.published()
        with self.store.conn() as c:
            raw=bytes(c.execute('SELECT artifact FROM universal_validated_checkpoints').fetchone()[0])
            artifact=json.loads(raw);artifact['core']['scorer']['layers']['dense2']['bias'][0]+=1
            c.execute('UPDATE universal_validated_checkpoints SET artifact=?',(_bytes(artifact),))
        with self.assertRaisesRegex(ValueError,'seal'):self.service.load(aid,self.source)
        with self.store.conn() as c:
            c.execute('UPDATE universal_validated_checkpoints SET artifact=?',(b'{',))
        with self.assertRaises(ValueError):self.service.load(aid,self.source)

    def test_trained_flag_and_bad_core_checksum_are_rejected(self):
        for change in ({'trained':True},{'checksum':'0'*64}):
            core=copy.deepcopy(self.core);core.update(change)
            with self.assertRaises(ValueError):self.service.publish(core,self.source)
        self.assertEqual(self.count('universal_checkpoint_attempts'),0)

    def test_rebuild_isolated_and_reproducible_from_clean_seed(self):
        before=copy.deepcopy(self.core)
        child=self.service.rebuild(self.source,parent_seed=1,**self.split,config=TrainingConfig(epochs=24))
        self.assertEqual(child['checkpoint'],self.core)
        self.assertEqual(before,self.core)

    def test_rebuild_after_undo_links_predecessor_without_reusing_its_weights(self):
        aid=self.published(); before=copy.deepcopy(self.core);self.undo()
        child=self.service.rebuild(self.source,parent_seed=1,predecessor_id=aid,
                                   **self.split,config=TrainingConfig(epochs=24))
        self.assertEqual(child['status'],'optimization_complete')
        self.assertEqual(child['checkpoint']['rebuild_of'],aid)
        self.assertNotEqual(child['checkpoint']['dataset_id'],before['dataset_id'])
        self.assertEqual(before,self.core)
        with self.assertRaisesRegex(ValueError,'holdout already exposed'):
            self.service.publish(child['checkpoint'],self.source)
        child['model'].scorer.layers['dense2'][0][:]=0
        self.assertEqual(before,self.core)

    def test_source_bounds_and_wrong_source_fail_closed(self):
        aid=self.published(); doc=json.loads(self.source.raw);doc['evidence']='offline'
        with self.assertRaisesRegex(ValueError,'wrong historical source'):
            self.service.load(aid,NativeDatasetSource(_bytes(doc)))
        with self.assertRaises(ValueError):NativeDatasetSource(b'{"version":1,"version":1}')

    def test_explicit_revocation_survives_restart_and_prevents_republication(self):
        aid=self.published()
        self.assertTrue(self.service.revoke(aid,reason='experiment retired'))
        self.assertFalse(self.service.revoke(aid,reason='already retired'))
        with self.assertRaisesRegex(ValueError,'revoked'):CheckpointLifecycle(Store(self.path)).load(aid,self.source)
        with self.assertRaisesRegex(ValueError,'no longer qualified'):self.service.publish(self.core,self.source)

    def test_conflict_invalidates_accepted_correct_and_revision(self):
        aid=self.published()
        ManualFeedbackJournal(self.store).set_status('synthetic-feedback:0','conflict')
        with self.assertRaisesRegex(ValueError,'revoked'):self.service.load(aid,self.source)

    def test_late_generation_tombstone_cannot_hide_behind_historical_as_of(self):
        aid=self.published()
        journal=ManualFeedbackJournal(self.store,clock=lambda:999999.)
        journal.record(agent_id='agent-new',root_agent_id='agent0',selected_ts=100.,source='correct',
                       correct_action=0.,feedback_id='late-copy',generation_id='new-generation',
                       episode_id='synthetic-episode:0')
        with self.store.conn() as c:
            lid=c.execute('''INSERT INTO teaching_rl_labels
                (agent_id,created_ts,sample_ts,desired,fingerprint,supervision_event_id)
                VALUES('agent-new',999999,100,0,'fixture','synthetic-correct:0')''').lastrowid
        journal.link('late-copy','learning','teach_rl_label',lid)
        journal.undo('late-copy')
        with self.assertRaisesRegex(ValueError,'revoked'):self.service.load(aid,self.source)
        with _connection(self.store) as c:
            data,_=_snapshot(c,self.source)
        self.assertNotIn('synthetic-correct:0',[s.supervision_event_id for s in data.samples])

    def test_late_new_correction_changes_revision_before_publication(self):
        journal=ManualFeedbackJournal(self.store,clock=lambda:999999.)
        journal.record(agent_id='agent0',selected_ts=101.,source='correct',correct_action=1.,
                       feedback_id='late-correct',episode_id='late-episode')
        with self.assertRaisesRegex(ValueError,'revision changed'):self.service.publish(self.core,self.source)

    def test_rebuild_withholds_holdout_and_disclosed_legacy_metrics_cannot_publish(self):
        from universal_training_data import _digest
        self.assertFalse(self.core['holdout_evaluated'])
        self.assertEqual(set(self.core['after']['holdout']),{'status','samples'})
        core=copy.deepcopy(self.core);core['holdout_evaluated']=True
        core.pop('checksum');core['checksum']=_digest(core)
        with self.assertRaisesRegex(ValueError,'already disclosed'):self.service.publish(core,self.source)

    def test_cancelled_validation_keeps_holdout_consumed(self):
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',return_value={'status':'cancelled'}):
            result=self.service.publish(self.core,self.source)
        self.assertEqual(result['status'],'cancelled')
        with self.assertRaisesRegex(ValueError,'consumed'):self.service.publish(self.core,self.source)

    def test_recovery_during_validation_cannot_be_overwritten(self):
        def validate(*a,**kw):
            result=validate_core_artifact(*a,**kw);self.assertEqual(self.service.recover(),1);return result
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'changed before publication'):self.service.publish(self.core,self.source)
        self.assertEqual(self.count('universal_validated_checkpoints'),0)

    def test_real_process_exit_in_validation_and_acceptance_transaction(self):
        # Abrupt exit bypasses all Python finally/rollback handlers.
        source_path=self.path.parent/'source.json';source_path.write_bytes(self.source.raw)
        core_path=self.path.parent/'core.json';core_path.write_bytes(_bytes(self.core))
        script='''import sys,os,json
sys.path.insert(0,sys.argv[1])
from storage import Store
from storage_budget import BudgetConnection
from universal_checkpoint_lifecycle import CheckpointLifecycle,NativeDatasetSource
from pathlib import Path
from unittest.mock import patch
service=CheckpointLifecycle(Store(Path(sys.argv[2])))
source=NativeDatasetSource(Path(sys.argv[3]).read_bytes())
core=json.loads(Path(sys.argv[4]).read_bytes())
if sys.argv[5]=='validation':
    with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=lambda *a,**k:os._exit(71)):
        service.publish(core,source)
else:
    original=BudgetConnection.execute
    def execute(self,sql,*args,**kw):
        if sql.startswith('UPDATE universal_checkpoint_attempts SET status=') and args[0][0]=='published':os._exit(72)
        return original(self,sql,*args,**kw)
    with patch.object(BudgetConnection,'execute',execute):service.publish(core,source)
'''
        for phase,code in [('validation',71),('acceptance',72)]:
            if phase=='acceptance':
                # Fresh independent disposable DB for the second crash case.
                self.path=self.path.parent/'second.db';shutil.copyfile(self.template,self.path)
                self.store=Store(self.path);self.service=CheckpointLifecycle(self.store)
            result=subprocess.run([sys.executable,'-c',script,str(support.ROOT/'adaptive_ai'/'src'),
                                   str(self.path),str(source_path),str(core_path),phase],
                                  capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,code,result.stderr)
            self.assertEqual(self.count('universal_validated_checkpoints'),0)
            service=CheckpointLifecycle(Store(self.path));self.assertEqual(service.recover(),1)
