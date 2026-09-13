import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from support import state
import ha
from storage import Store


class AutomationScanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'cache.db')
        self.store_patch=patch.object(ha,'STORE',self.store)
        self.store_patch.start()
        self.knowledge=ha.AutomationKnowledge()
        self.states={'automation.stairs':state('automation.stairs','on',id='stairs'),
                     'automation.other':state('automation.other','on',id='other')}
        self.config={'triggers':[{'entity_id':'binary_sensor.motion'}],
                     'actions':[{'action':'light.turn_on','target':{'entity_id':'light.stairs'}}]}

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def scan(self, side_effect):
        with patch.object(ha.HA,'automation_config',side_effect=side_effect):
            self.knowledge.scan(self.states,force=True)

    def test_partial_scan_keeps_readable_target_and_reports_exact_failure(self):
        def fetch(aid,**kwargs):
            if aid=='other':raise OSError('404')
            return self.config
        self.scan(fetch)
        self.assertEqual(len(self.knowledge.hints_for_target('light.stairs')[1]),1)
        self.assertEqual(self.knowledge.status()['config_failures'][0]['entity_id'],'automation.other')
        self.assertIsNotNone(self.knowledge.error)

    def test_failed_refresh_keeps_previous_targets_across_restart(self):
        self.scan(lambda aid,**kw:self.config if aid=='stairs' else {'actions':[]})
        self.knowledge=ha.AutomationKnowledge()
        self.assertTrue(self.knowledge.hints_for_target('light.stairs')[1])
        self.scan(OSError('timeout'))
        hints,infos=self.knowledge.hints_for_target('light.stairs')
        self.assertEqual(hints,{'binary_sensor.motion'})
        self.assertEqual(infos[0]['config_status'],'cached')
        self.assertEqual(len(self.knowledge.status()['config_failures']),2)

    def test_successful_configuration_change_removes_old_target(self):
        self.scan(lambda *a,**kw:self.config)
        self.scan(lambda *a,**kw:{'actions':[{'action':'light.turn_on','target':{'entity_id':'light.new'}}]})
        self.assertEqual(self.knowledge.hints_for_target('light.stairs')[1],[])
        self.assertTrue(self.knowledge.hints_for_target('light.new')[1])
        self.assertIsNone(self.knowledge.error)

    def test_deleted_automation_is_removed_from_persistent_cache(self):
        self.scan(lambda *a,**kw:self.config)
        self.states={}
        self.scan(lambda *a,**kw:None)
        self.assertEqual(ha.AutomationKnowledge().hints_for_target('light.stairs')[1],[])

    def test_reused_entity_with_different_id_does_not_get_old_mapping(self):
        self.scan(lambda aid,**kw:self.config if aid=='stairs' else {'actions':[]})
        self.states['automation.stairs']['attributes']['id']='replacement'
        self.scan(OSError('unreadable'))
        self.assertEqual(self.knowledge.hints_for_target('light.stairs')[1],[])

    def test_missing_configuration_id_is_a_visible_warning(self):
        self.states={'automation.yaml':state('automation.yaml','on')}
        self.scan(lambda *a,**kw:None)
        self.assertEqual(self.knowledge.status()['config_failures'][0]['entity_id'],'automation.yaml')
        self.assertEqual(self.knowledge.hints_for_target('light.stairs')[1],[])

    def test_invalid_response_is_reported(self):
        self.scan(lambda *a,**kw:[])
        self.assertEqual(len(self.knowledge.status()['config_failures']),2)


if __name__=='__main__':unittest.main()
