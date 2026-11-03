"""Fresh collected V3 source through the real offline checkpoint lifecycle."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import support
import sys
sys.path.insert(0,str(support.ROOT/'tools'))
from storage import Store
from manual_feedback_contract import ManualFeedbackJournal
from universal_training_fixture import make_collected_source
from universal_checkpoint_lifecycle import CheckpointLifecycle,_connection,_snapshot
from universal_scorer_training import TrainingConfig,context_from_sample
from universal_scorer_validation import validate_core_artifact
from universal_scorer_training import train_scorer


class RetentionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory()
        cls.template=Path(cls.folder.name)/'template.db'
        store=Store(cls.template)
        cls.source,cls.split=make_collected_source(store)
        service=CheckpointLifecycle(store)
        with _connection(store) as c:cls.data,_=_snapshot(c,cls.source)
        result=service.rebuild(cls.source,parent_seed=1,**cls.split,config=TrainingConfig(epochs=24))
        if result['status']!='optimization_complete':raise AssertionError(result)
        cls.core=result['checkpoint'];cls.model=result['model']
        report=validate_core_artifact(cls.core,cls.data)
        if report['status']!='passed':raise AssertionError(report)
        with store.conn() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    @classmethod
    def tearDownClass(cls):cls.folder.cleanup()

    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        path=Path(folder.name)/'copy.db';shutil.copyfile(self.template,path)
        self.store=Store(path);self.service=CheckpointLifecycle(self.store)

    def delete(self,entity):
        with self.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=?',(entity,))

    def publish(self):
        result=self.service.publish(self.core,self.source)
        self.assertEqual(result['status'],'published');return result['artifact_id']

    def test_unrelated_loss_before_training_publish_load_and_rank(self):
        self.delete('sensor.unrelated')
        result=self.service.rebuild(self.source,parent_seed=1,**self.split,config=TrainingConfig(epochs=24))
        self.assertEqual(result['status'],'optimization_complete')
        aid=self.publish();loaded=CheckpointLifecycle(Store(self.store.path)).load(aid,self.source)
        context=context_from_sample(self.data.samples[-1])
        self.assertEqual(loaded.rank_context(context),self.model.rank_context(context))

    def test_required_loss_before_training_rejects(self):
        self.delete('binary_sensor.motion')
        with self.assertRaisesRegex(ValueError,'retention'):
            self.service.rebuild(self.source,parent_seed=1,**self.split,config=TrainingConfig(epochs=24))

    def test_required_loss_during_training_cannot_publish_result(self):
        def train(*args,**kwargs):
            self.delete('binary_sensor.motion');return train_scorer(*args,**kwargs)
        with patch('universal_checkpoint_lifecycle.train_scorer',side_effect=train):
            result=self.service.rebuild(self.source,parent_seed=1,**self.split,config=TrainingConfig(epochs=24))
        self.assertEqual(result['status'],'optimization_complete')
        with self.assertRaisesRegex(ValueError,'retention'):
            self.service.publish(result['checkpoint'],self.source)

    def test_unrelated_loss_during_validation_can_publish(self):
        def validate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs);self.delete('sensor.unrelated');return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=validate):self.publish()

    def test_required_loss_during_validation_cannot_publish(self):
        def validate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs);self.delete('binary_sensor.motion');return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'retention'):self.publish()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM universal_validated_checkpoints').fetchone()[0],0)

    def test_required_loss_revokes_load_and_existing_rank_handle(self):
        aid=self.publish();loaded=self.service.load(aid,self.source)
        self.delete('binary_sensor.motion')
        with self.assertRaises(ValueError):self.service.load(aid,self.source)
        with self.assertRaises(ValueError):loaded.rank_context(context_from_sample(self.data.samples[-1]))

    def test_undo_is_still_checked_after_unrelated_loss(self):
        aid=self.publish();loaded=self.service.load(aid,self.source)
        self.delete('sensor.unrelated')
        ManualFeedbackJournal(self.store,clock=lambda:999999.).undo('synthetic-feedback:0')
        with self.assertRaises(ValueError):loaded.rank_context(context_from_sample(self.data.samples[-1]))
        with self.assertRaises(ValueError):self.service.load(aid,self.source)

    def test_undo_during_validation_is_not_hidden_by_scoped_cas(self):
        def validate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs)
            ManualFeedbackJournal(self.store,clock=lambda:999999.).undo('synthetic-feedback:0')
            return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'supervision changed'):self.publish()


if __name__=='__main__':unittest.main()
