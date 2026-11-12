import unittest
import support
from training_stages import stage_for


class TrainingStagesTests(unittest.TestCase):
    def test_reports_operational_stage_without_eta_guessing(self):
        cases=[(('worker_bootstrap','','',None,None),'Preparing generation'),
            (('worker_bootstrap','','',{'stage':'Recovering corruption'},None),'Recovering corruption'),
            (('training','','',None,{'state':'waiting_wal'}),'Waiting for WAL'),
            (('training','requesting Recorder chunk','',None,None),'Recorder import'),
            (('training','replay 4/100','',None,None),'Replay'),
            (('training','Training supervised Tiny MLP','',None,None),'Learning'),
            (('training','saving held-out benchmark','',None,None),'Benchmark'),
            (('training','serializing policy model','',None,None),'Publishing model')]
        for args,expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(stage_for(*args),expected)
