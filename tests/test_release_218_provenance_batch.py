import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support
from storage import Store
from provenance import ProvenanceJournal
from storage_budget import StoragePressure


class ProvenanceBatch218Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'audit.db')
        self.journal = ProvenanceJournal(self.store, clock=lambda:100.)

    def rows(self, count=240):
        return [dict(decision_id=str(i),created_time=float(i),agent_id='a',
                     feature_manifest={'features':{'1':i}},allowed_actions=[0.,1.],
                     model_desired=i%2,chosen_action=i%2,dispatch_status='SHADOW') for i in range(count)]

    def test_bounded_decision_insert_keeps_idempotency_results_and_fresh_admission_boundaries(self):
        with self.store.connection_session() as c:
            c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            with patch.object(self.store.storage_budget,'check',wraps=self.store.storage_budget.check) as checks:
                self.assertEqual(self.journal.record_decisions_batch(self.rows()),240)
            fresh = [call for call in checks.call_args_list if call.kwargs.get('refresh',True)]
            cached = [call for call in checks.call_args_list if call.kwargs.get('refresh') is False]
            self.assertGreaterEqual(len(fresh),3)
            self.assertLess(len(fresh),60)
            self.assertGreater(len(cached),0)
            self.assertEqual(c.storage_batch_depth,0)
            self.assertEqual(self.journal.record_decisions_batch(self.rows()),0)
            records = c.execute('SELECT decision_id,created_time,model_desired,dispatch_status FROM provenance_decisions').fetchall()
        self.assertEqual({tuple(r) for r in records},{(str(i),float(i),float(i%2),'SHADOW') for i in range(240)})

    def test_mid_batch_trigger_failure_rolls_back_the_complete_decision_batch(self):
        with self.store.conn() as c:
            c.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON provenance_decisions "
                      "WHEN NEW.decision_id='43' BEGIN SELECT RAISE(ABORT,'test audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.record_decisions_batch(self.rows())
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_decisions').fetchone()[0],0)
            c.execute('DROP TRIGGER reject_audit')
        self.assertEqual(self.journal.record_decisions_batch(self.rows()),240)

    def test_pressure_after_insert_rolls_back_every_group_and_releases_batch_scope(self):
        original = self.store.storage_budget.check
        inserted = []
        def pressure(parameters=(), *, refresh=True):
            if refresh and inserted:
                raise StoragePressure('test final admission')
            result = original(parameters,refresh=refresh)
            if not refresh:
                inserted.append(True)
            return result
        with patch.object(self.store.storage_budget,'check',side_effect=pressure):
            with self.assertRaises(StoragePressure):
                self.journal.record_decisions_batch(self.rows())
        with self.store.conn() as c:
            self.assertEqual(c.storage_batch_depth,0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_decisions').fetchone()[0],0)

    def test_event_link_failure_restores_the_entire_accepted_batch_for_exact_retry(self):
        for i in range(100):
            self.journal.record_event('sensor.a',support.state('sensor.a',str(i)),
                event_time=float(i),received_time=100.+i,event_id='e'+str(i))
        with self.store.conn() as c:
            c.execute("CREATE TRIGGER reject_link BEFORE INSERT ON provenance_history_links "
                      "WHEN NEW.event_id='e43' BEGIN SELECT RAISE(ABORT,'test link failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.flush_events_batch()
        self.assertEqual(self.journal.pending_event_count(),100)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_events').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_history_links').fetchone()[0],0)
            c.execute('DROP TRIGGER reject_link')
        self.assertEqual(self.journal.flush_events_batch(),100)
        self.assertEqual(self.journal.pending_event_count(),0)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_events').fetchone()[0],100)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_history_links').fetchone()[0],100)


if __name__ == '__main__':
    unittest.main()
