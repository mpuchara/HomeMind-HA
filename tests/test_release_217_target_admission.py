import unittest
import time
from pathlib import Path
import tempfile
from unittest.mock import patch

import support
import engine as engine_module
from storage import Store
from tools.benchmark_target_retries import fixture, drain

SHARED='binary_sensor.shared'


class TargetAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'admission.db')
        self.runtime=fixture(self.store, targets=20)
        self.runtime.control_worker_count=2
        self.runtime.inference_scheduler={}
        current=patch.object(engine_module,'STORE',self.store)
        current.start();self.addCleanup(current.stop)

    def outstanding(self):
        return sum(not f.done() for f,_,_ in self.runtime.control_workers.tasks)

    def test_twenty_target_burst_never_queues_old_worker_frames_and_is_fair(self):
        r=self.runtime
        r.process(r.state_map,{SHARED})
        self.assertEqual(len(r.control_workers.tasks),2)
        self.assertEqual(len(r.ready_target_changes),18)
        initial={task[2][0][0]['id'] for task in r.control_workers.tasks}
        r.state_revision=1;r.entity_revisions[SHARED]=1
        r.state_map[SHARED]={'state':'newest'}
        received=time.perf_counter();r.entity_event_received_perf[SHARED]=received
        r.process(r.state_map,{SHARED})
        self.assertEqual(len(r.control_workers.tasks),2)
        self.assertTrue(r.control_workers.complete_next());drain(r)
        task=r.control_workers.tasks[2]
        self.assertNotIn(task[2][0][0]['id'],initial)
        self.assertEqual(task[2][2][0][SHARED]['state'],'newest')
        self.assertEqual(task[2][3],{SHARED:received})
        for _ in range(100):
            self.assertLessEqual(self.outstanding(),2)
            if not r.control_workers.complete_next():break
            drain(r)
        else:self.fail('Waiting targets did not drain')
        final=dict(r.outputs)
        self.assertEqual(final,{str(i):'newest' for i in range(20)})
        self.assertEqual(len(r.outputs),22)
        self.assertFalse(r.ready_target_changes)
        self.assertFalse(r.pending_target_changes)

    def test_full_capacity_waits_for_completion_instead_of_wake_spin(self):
        r=self.runtime
        r.process(r.state_map,{SHARED});r.wake_event.clear()
        r.process(r.state_map,{SHARED})
        self.assertFalse(r.wake_event.is_set())
        r.control_workers.complete_next()
        self.assertTrue(r.wake_event.is_set())

    def test_timer_capacity_also_wakes_without_old_event_receipts(self):
        r=self.runtime
        hints={a['target_entity'] for a in r.agent_configs.values()}
        r.process(r.state_map,hints,event_driven=False)
        self.assertEqual(len(r.control_workers.tasks),2)
        self.assertTrue(r._timer_capacity_pending)
        for task in r.control_workers.tasks:self.assertEqual(task[2][3],{})
        r.wake_event.clear();r.control_workers.complete_next()
        self.assertTrue(r.wake_event.is_set())
        r.process(r.state_map,hints,event_driven=False)
        self.assertLessEqual(self.outstanding(),2)


if __name__=='__main__':unittest.main()
