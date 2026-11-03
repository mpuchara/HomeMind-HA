"""Native V3 scope, original seeds, atomic deletion and restart integrity."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import support
import test_universal_affected_sensors as affected_fixtures
from observation_contract import FeatureJournal
from storage import Store
from universal_history_collector import source_from_collection,digest,encoded,verify_collection_coverage
from universal_checkpoint_lifecycle import NativeDatasetSource
from universal_training_data import ReplayCoverage


class ScopedFixture:
    def __init__(self,test):
        fixture=affected_fixtures.AffectedSensorTests();fixture.setUp()
        test.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.f=fixture.f;self.store=self.f.store
        fixture.topology({'sensor.outdoor_temperature':{'device_class':'temperature'},
                          'binary_sensor.kitchen_motion':{'device_class':'motion'}})
        fixture.declare([],empty_reason='this independent monitored motion fixture has no dependent sensors')
        self.data,self.source,self.feedback,self.intent,self.evidence=fixture.dataset()
        test.assertEqual(len(self.data.samples),1,self.data.rejected)
        self.doc=json.loads(self.source.raw);self.coverage=ReplayCoverage(**dict(self.doc['coverage'],entity_ids=frozenset(self.doc['coverage']['entity_ids'])))
        self.required='binary_sensor.motion'

    def dataset(self,source=None):
        with self.store.conn() as c:
            c.execute('BEGIN');return (source or self.source).dataset(c)

    def delete(self,eid):
        with self.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=?',(eid,))


class RetentionTests(unittest.TestCase):
    def setUp(self):self.x=ScopedFixture(self)

    def test_external_replace_without_recursive_triggers_cannot_hide_old_loss(self):
        x=self.x
        with closing(sqlite3.connect(x.store.path)) as c:
            self.assertEqual(c.execute('PRAGMA recursive_triggers').fetchone()[0],0)
            key=c.execute('SELECT event_key FROM feature_observation_events WHERE entity_id=? ORDER BY received_time DESC LIMIT 1',(x.required,)).fetchone()[0]
            row=c.execute('SELECT * FROM feature_observation_events WHERE event_key=?',(key,)).fetchone()
            cols=[r[1] for r in c.execute('PRAGMA table_info(feature_observation_events)')]
            altered=list(row);altered[cols.index('received_time')]=x.coverage.complete_through_ts+10
            c.execute('INSERT OR REPLACE INTO feature_observation_events VALUES('+','.join('?' for _ in altered)+')',altered)
            c.commit()
            self.assertEqual(c.execute('SELECT live_count FROM universal_retention_meta').fetchone()[0],
                             c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0])
            c.execute('PRAGMA recursive_triggers=ON')
            altered[cols.index('received_time')]+=1
            c.execute('INSERT OR REPLACE INTO feature_observation_events VALUES('+','.join('?' for _ in altered)+')',altered)
            c.commit()
            self.assertEqual(c.execute('SELECT live_count FROM universal_retention_meta').fetchone()[0],
                             c.execute('SELECT COUNT(*) FROM feature_observation_events').fetchone()[0])
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_metadata_budget_exhaustion_burns_old_scopes_without_growing(self):
        with patch('universal_retention.MAX_ENTITIES',2):
            x=ScopedFixture(self)
            x.delete('sensor.outdoor_temperature');x.delete('binary_sensor.kitchen_motion')
            self.assertEqual(len(x.dataset().samples),1)
            with x.store.conn() as c:
                c.execute('''INSERT INTO feature_observation_events
                    (event_key,contract_version,entity_id,event_time,received_time,state,attributes_json,source,quality,protected_until)
                    VALUES('overflow',1,'sensor.extra',1,?,'1','{}','test',1,0)''',
                    (x.coverage.complete_through_ts+5,))
                c.execute("DELETE FROM feature_observation_events WHERE event_key='overflow'")
                self.assertEqual(c.execute('SELECT COUNT(*) FROM universal_retention_entities').fetchone()[0],2)
            with self.assertRaisesRegex(ValueError,'revision'):x.dataset()

    def test_required_scope_is_separate_from_full_historical_universe(self):
        x=self.x
        self.assertEqual(x.coverage.version,3)
        self.assertEqual(x.coverage.entity_ids,frozenset((x.required,)))
        self.assertEqual(x.coverage.historical_sensor_universe_count,3)
        original=json.loads(x.evidence['affected_sensor_evidence']['declaration_json'])
        self.assertEqual(original['sensor_universe_count'],3)
        self.assertEqual(original['sensor_universe_revision'],x.coverage.historical_sensor_universe_revision)

    def test_unrelated_rows_and_seeds_preserve_dataset_and_reexport(self):
        x=self.x;x.delete('sensor.outdoor_temperature')
        self.assertEqual(x.dataset().dataset_id,x.data.dataset_id)
        with x.store.conn() as c:
            c.execute('BEGIN')
            source=source_from_collection(c,[x.f.agent['id']],start=x.coverage.start_ts,through=x.coverage.complete_through_ts)
            self.assertEqual(len(source.dataset(c).samples),1)
        self.assertEqual(json.loads(source.raw)['coverage']['historical_sensor_universe_count'],3)

    def test_required_seed_loss_rejects_and_reinsertion_cannot_repair(self):
        x=self.x;key=x.coverage.seed_event_keys[0][1]
        with x.store.conn() as c:
            c.execute('CREATE TEMP TABLE saved AS SELECT * FROM feature_observation_events WHERE event_key=?',(key,))
            c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(key,))
            c.execute('INSERT INTO feature_observation_events SELECT * FROM saved')
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_required_event_and_meaningful_mutation_reject(self):
        x=self.x
        with x.store.conn() as c:c.execute("UPDATE feature_observation_events SET state='off' WHERE event_key=?",(x.coverage.seed_event_keys[0][1],))
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_backdated_insert_rejects_original_and_new_export(self):
        x=self.x
        with x.store.conn() as c:
            c.execute('''INSERT INTO feature_observation_events SELECT 'injected-past',contract_version,entity_id,
                event_time,received_time,state,attributes_json,last_changed,last_updated,source,quality,protected_until
                FROM feature_observation_events WHERE event_key=?''',(x.coverage.seed_event_keys[0][1],))
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()
        with x.store.conn() as c:
            c.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'retention'):
                source_from_collection(c,[x.f.agent['id']],start=x.coverage.start_ts,through=x.coverage.complete_through_ts)

    def test_later_receipt_outside_window_does_not_expire_old_scope(self):
        x=self.x;now=time.time()
        key=x.f.journal.record(x.required,support.state(x.required,'off'),event_time=now,received_time=now,source='ha_state_changed')
        self.assertEqual(len(x.dataset().samples),1)
        with x.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(key,))
        self.assertEqual(len(x.dataset().samples),1)

    def test_earlier_unrequired_seed_of_same_sensor_does_not_expire_scope(self):
        x=self.x
        with x.store.conn() as c:
            keys=c.execute('SELECT event_key FROM feature_observation_events WHERE entity_id=? AND received_time<?',
                           (x.required,x.coverage.start_ts)).fetchall()
            self.assertTrue(keys)
            c.executemany('DELETE FROM feature_observation_events WHERE event_key=?',keys)
        self.assertEqual(len(x.dataset().samples),1)

    def test_bounded_envelope_can_conservatively_merge_disjoint_damage(self):
        x=self.x
        with x.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=? AND received_time<?',(x.required,x.coverage.start_ts))
        now=time.time();key=x.f.journal.record(x.required,support.state(x.required,'off'),event_time=now,received_time=now,source='ha_state_changed')
        with x.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(key,))
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_protection_changes_and_exact_duplicate_keys_do_not_change_history(self):
        x=self.x
        with x.store.conn() as c:
            before=c.execute('SELECT revision,live_count FROM universal_retention_meta').fetchone()
            c.execute('UPDATE feature_observation_events SET protected_until=?',(time.time()+1000,))
            c.execute('''INSERT INTO feature_observation_events SELECT * FROM feature_observation_events WHERE event_key=?
                ON CONFLICT(event_key) DO UPDATE SET protected_until=excluded.protected_until''',(x.coverage.seed_event_keys[0][1],))
            self.assertEqual(tuple(before),tuple(c.execute('SELECT revision,live_count FROM universal_retention_meta').fetchone()))
        self.assertEqual(len(x.dataset().samples),1)

    def test_age_cutoff_and_per_entity_prune_preserve_unrelated_scope(self):
        x=self.x;now=time.time()+1
        with x.store.conn() as c:c.execute('UPDATE feature_observation_events SET protected_until=? WHERE entity_id=?',(now+1000,x.required))
        journal=FeatureJournal(x.store,clock=lambda:now,retention_hours=0,max_events_per_entity=0)
        journal.prune('sensor.outdoor_temperature')
        self.assertEqual(len(x.dataset().samples),1)
        journal.prune('sensor.outdoor_temperature')
        self.assertEqual(len(x.dataset().samples),1)

    def test_per_entity_limit_is_atomic_and_does_not_need_callback(self):
        x=self.x
        journal=FeatureJournal(x.store,retention_hours=1e9,max_events_per_entity=0)
        x.store._universal_training_retention_changed=None
        journal.prune('sensor.outdoor_temperature')
        self.assertEqual(len(x.dataset().samples),1)
        journal.prune(x.required)
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_global_hard_cap_deletes_unprotected_then_protected(self):
        x=self.x
        with x.store.conn() as c:c.execute('UPDATE feature_observation_events SET protected_until=? WHERE entity_id=?',(time.time()+10000,x.required))
        journal=FeatureJournal(x.store,retention_hours=1e9,global_event_limit=2)
        journal.prune()
        self.assertEqual(len(x.dataset().samples),1)
        journal.global_event_limit=0;journal.prune()
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_delete_and_invalidation_rollback_together(self):
        x=self.x
        with x.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(x.coverage.seed_event_keys[0][1],))
            with self.assertRaisesRegex(ValueError,'retention'):verify_collection_coverage(c,x.coverage)
            c.rollback()
        self.assertEqual(len(x.dataset().samples),1)

    def test_trigger_fault_rolls_back_observation_delete(self):
        x=self.x
        with x.store.conn() as c:c.execute("CREATE TRIGGER injected_retention_fault BEFORE UPDATE ON universal_retention_meta BEGIN SELECT RAISE(ABORT,'injected ledger failure'); END")
        with self.assertRaisesRegex(sqlite3.DatabaseError,'injected ledger'):
            x.delete(x.required)
        with x.store.conn() as c:c.execute('DROP TRIGGER injected_retention_fault')
        self.assertEqual(len(x.dataset().samples),1)

    def test_crash_before_and_after_commit(self):
        x=self.x
        script='''import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute('BEGIN IMMEDIATE')
c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(sys.argv[2],))
if sys.argv[3]=='commit':c.commit()
os._exit(77)
'''
        for mode in ('rollback','commit'):
            p=subprocess.run([sys.executable,'-c',script,x.store.path,x.coverage.seed_event_keys[0][1],mode],capture_output=True,text=True,timeout=30)
            self.assertEqual(p.returncode,77,p.stderr)
            if mode=='rollback':self.assertEqual(len(x.dataset().samples),1)
            else:
                with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_restart_disabled_collection_and_external_connection(self):
        x=self.x;store=Store(x.store.path);FeatureJournal(store)
        with closing(sqlite3.connect(store.path)) as c,c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=?',('sensor.outdoor_temperature',))
        self.assertEqual(len(x.dataset().samples),1)
        with closing(sqlite3.connect(store.path)) as c,c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=?',(x.required,))
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_reader_snapshot_and_writer_see_consistent_atomic_states(self):
        x=self.x
        with x.store.conn() as reader:
            reader.execute('BEGIN');self.assertEqual(len(x.source.dataset(reader).samples),1)
            with closing(sqlite3.connect(x.store.path)) as writer,writer:
                writer.execute('DELETE FROM feature_observation_events WHERE entity_id=?',(x.required,))
            self.assertEqual(len(x.source.dataset(reader).samples),1)
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset()

    def test_missing_trigger_is_not_repaired_into_old_authority(self):
        x=self.x
        with x.store.conn() as c:c.execute('DROP TRIGGER ur_delete')
        with self.assertRaisesRegex(ValueError,'trigger contract'):x.dataset()
        FeatureJournal(Store(x.store.path))
        with self.assertRaisesRegex(ValueError,'tracker|revision'):x.dataset()

    def test_missing_damage_metadata_burns_old_scopes(self):
        x=self.x;x.delete('sensor.outdoor_temperature')
        self.assertEqual(len(x.dataset().samples),1)
        with x.store.conn() as c:c.execute('DELETE FROM universal_retention_entities')
        with self.assertRaisesRegex(ValueError,'revision'):x.dataset()

    def test_corrupt_witness_topology_revision_and_downgrade_reject(self):
        x=self.x
        for key in ('checksum','topology','origin','universe','missing','downgrade'):
            doc=json.loads(x.source.raw);cov=doc['coverage'];proof=json.loads(cov['granular_witness_json'])
            if key=='checksum':proof['checksum']='wrong'
            elif key=='topology':proof['topology_revision']='wrong'
            elif key=='origin':proof['origin_revision']+=1
            elif key=='universe':cov['historical_sensor_universe_count']=1
            elif key=='missing':cov['granular_witness_json']=None
            else:cov['version']=2
            if key not in ('missing','universe','downgrade'):
                if key!='checksum':
                    body=dict(proof);body.pop('checksum');proof['checksum']=digest(encoded(body))
                cov['granular_witness_json']=encoded(proof)
            bad=NativeDatasetSource(json.dumps(doc).encode())
            with self.subTest(key=key),self.assertRaises(ValueError):x.dataset(bad)

    def test_legacy_v1_and_v2_remain_distinct_conservative_contracts(self):
        import test_universal_training_data as legacy
        self.assertEqual(len(legacy.build().samples),1)
        x=self.x;doc=json.loads(x.source.raw)
        with x.store.conn() as c:
            row=c.execute('SELECT proof,topology,retention_epoch FROM universal_collection_segments WHERE id=?',(x.coverage.source_revision,)).fetchone()
            original=json.loads(row[0]);proof={k:original[k] for k in ('source_revision','start_ts','complete_through_ts','entity_ids','complete','seed_receipts','collector_retention_epoch')}
            proof.update(version=2,source_revision='legacy-v2-fixture')
            raw=encoded(proof);checksum=digest(raw);end=x.coverage.complete_through_ts
            c.execute('INSERT INTO universal_collection_segments VALUES(?,?,?,?,?,?,?,1,NULL,?,?)',
                ('legacy-v2-fixture','legacy-fixture',proof['start_ts'],end,row[1],raw,row[2],checksum,
                 digest(encoded(['legacy-v2-fixture',proof['start_ts'],end,row[1],row[2],checksum]))))
        proof['entity_ids']=sorted(proof['entity_ids']);proof['complete_through_ts']=end
        doc['coverage']=proof
        source=NativeDatasetSource(json.dumps(doc).encode())
        self.assertEqual(len(x.dataset(source).samples),1)
        x.delete('sensor.outdoor_temperature')
        with self.assertRaisesRegex(ValueError,'retention'):x.dataset(source)
        self.assertEqual(len(x.dataset().samples),1)


if __name__=='__main__':unittest.main()
