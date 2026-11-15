"""Seven-day generation, real damaged provenance index and payload tests."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

import support
from storage import Store
from provenance import ProvenanceJournal
from training_generation import prepare_generation
from history_recovery import safe_segments


class SevenDayGenerationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source=self.root/'source.db'
        self.store=Store(self.source)
        self.addCleanup(self.store.stop_wal_keeper)
        self.addCleanup(self.store.stop_wal_checkpoint)
        self.addCleanup(self.store.stop_storage_retention)
        self.agent=self.store.create_agent(support.agent(target_entity='switch.target',
                                             input_entities=['binary_sensor.motion']))
        ProvenanceJournal(self.store)
        self.end=40*86400.
        self.start=self.end-7*86400.
        self.entities=['switch.target','binary_sensor.motion']
        self.dest=self.root/'trained.db'

    def add_events(self, entity, stamps, prefix, origin='own_command'):
        with self.store.conn() as c:
            c.executemany("""INSERT INTO provenance_events(event_id,contract_version,event_time,
                received_time,entity_id,source,origin,state_json) VALUES(?,1,?,?,?,'fixture',?,?)""",
                [(prefix+str(i),stamp,stamp,entity,origin,json.dumps({'payload':'x'*150}))
                 for i,stamp in enumerate(stamps)])
            c.executemany('INSERT INTO provenance_history_links VALUES(?,?,?)',
                [(entity,stamp,prefix+str(i)) for i,stamp in enumerate(stamps)])

    def prepare(self):
        return prepare_generation(self.source,self.dest,self.entities,self.end-14*86400.,
                                  self.end,agent_id=self.agent['id'],rebuild=True)

    def corrupt(self, object_name, leaf=False):
        with closing(sqlite3.connect(self.source)) as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            size=c.execute('PRAGMA page_size').fetchone()[0]
            page=c.execute('SELECT rootpage FROM sqlite_master WHERE name=?',(object_name,)).fetchone()[0]
        if leaf:
            raw=self.source.read_bytes()
            while raw[(page-1)*size]==5:
                offset=(page-1)*size
                count=int.from_bytes(raw[offset+3:offset+5],'big')
                cell=int.from_bytes(raw[offset+12+2*(count//2):offset+14+2*(count//2)],'big')
                page=int.from_bytes(raw[offset+cell:offset+cell+4],'big')
        with self.source.open('r+b') as f:
            f.seek((page-1)*size);f.write(b'\xff')

    def test_exact_window_seed_and_target_only_provenance(self):
        stamps=[self.start-86400*3,self.start-10,self.start+1,self.end-1,self.end+1]
        for entity in self.entities:
            self.store.archive_batch([(entity,t,'on',{},None,'live') for t in stamps])
            self.add_events(entity,stamps,entity)
        self.store.add_feedback(self.agent['id'],0,0.,-1.,'Correct',{0:1.},source='manual_feedback')
        report=self.prepare()
        self.assertEqual(report['training_window']['start_ts'],self.start)
        self.assertEqual(report['training_window']['end_ts'],self.end)
        with closing(sqlite3.connect(self.dest)) as c:
            self.assertEqual(c.execute('SELECT ts FROM entity_history WHERE entity_id=? ORDER BY ts',(self.entities[0],)).fetchall(),
                [(self.start-10,),(self.start+1,),(self.end-1,)])
            self.assertEqual(c.execute('SELECT DISTINCT entity_id FROM provenance_events').fetchall(),[('switch.target',)])
            self.assertEqual(c.execute('SELECT event_time,origin FROM provenance_events ORDER BY event_time').fetchall(),
                [(self.start-10,'own_command'),(self.start+1,'own_command'),(self.end-1,'own_command')])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM rl_feedback').fetchone()[0],1)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM entity_history').fetchone()[0],10)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_events').fetchone()[0],10)

    def test_corrupt_target_index_uses_physical_reads_without_losing_origin(self):
        stamps=[self.start+float(i+1) for i in range(500)]
        self.store.archive_batch([('switch.target',t,'on',{},None,'live') for t in stamps])
        self.add_events('switch.target',stamps,'target:')
        self.corrupt('idx_provenance_events_entity_time')
        with closing(sqlite3.connect(self.source)) as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute("SELECT * FROM provenance_events INDEXED BY idx_provenance_events_entity_time WHERE entity_id='switch.target'").fetchall()
        report=self.prepare()
        self.assertEqual(report['gaps'],[])
        self.assertTrue(any(x['table']=='provenance_events' and x['strategy']=='physical_rowid' for x in report['metadata_recoveries']))
        with closing(sqlite3.connect(self.dest)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM provenance_events WHERE origin='own_command'").fetchone()[0],len(stamps))
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')

    def test_corrupt_target_payload_maps_gaps_never_fabricates_origin(self):
        stamps=[self.start+float(i+1)*30 for i in range(2000)]
        self.store.archive_batch([('switch.target',t,'on',{},None,'live') for t in stamps])
        self.add_events('switch.target',stamps,'target:')
        self.corrupt('provenance_events',leaf=True)
        report=self.prepare()
        gaps=[g for g in report['gaps'] if g['source']=='provenance_events']
        self.assertTrue(gaps,report)
        self.assertTrue(safe_segments(report,self.start,self.end))
        self.assertTrue(all(g['failure_reason']=='HISTORY_GAP_SQLITE_CORRUPT' for g in gaps))
        with closing(sqlite3.connect(self.dest)) as c:
            recovered=c.execute('SELECT event_time,origin FROM provenance_events').fetchall()
            self.assertGreater(len(recovered),0)
            self.assertEqual({origin for _,origin in recovered},{'own_command'})
            missing=set(stamps)-{stamp for stamp,_ in recovered}
            self.assertTrue(missing)
            self.assertEqual(missing,{g['start_ts'] for g in gaps})

if __name__=='__main__':
    unittest.main()
