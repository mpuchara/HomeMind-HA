"""Exact persisted writer bytes against the frozen pre-optimization oracle."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import support
import universal_affected_sensors as affected
import universal_history_collector as writer
from storage import Store
from observation_contract import FeatureJournal
import test_universal_affected_sensors as fixtures


def frozen(module, filename, name):
    scope = dict(vars(module))
    exec(compile((Path(__file__).parent/'reference'/filename).read_text(), filename, 'exec'), scope)
    return scope[name]


old_validate = frozen(affected, 'affected_validate_9739874.py', 'validate')
old_write = frozen(writer, 'collector_write_9739874.py', '_write')


class ReceiptWriterDifferentialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AffectedSensorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def arm(self, method, batch, sql=None, fail=False):
        f = self.fixture.f
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'copy.db'
            with f.store.conn() as source, sqlite3.connect(path) as target:
                source.backup(target)
                # SQLite backup assigns its destination a fresh schema cookie.
                # Restore the source cookie on this disposable exact-schema copy
                # so the positive witness remains valid (DDL cases change it later).
                schema = source.execute('PRAGMA schema_version').fetchone()[0]
                target.execute('PRAGMA schema_version='+str(int(schema)))
            # This is an already initialized native snapshot, not a restart
            # migration: rerunning Store DDL would correctly stale every schema
            # witness and make the positive differential case vacuous.
            with patch.object(Store, '_init'), patch.object(Store, 'migrate_models'):
                store = Store(path)
            with patch.object(writer.uuid, 'uuid4', return_value=SimpleNamespace(hex='differential-session')):
                collector = writer.HistoricalTrainingCollector(store, FeatureJournal(store), clock=lambda: 2000000000.)
            for name in ('session','epoch','topology','topology_at','segment','connected','subscription_at','subscription_known','registries'):
                setattr(collector, name, deepcopy(getattr(f.collector, name)))
            if sql:
                with store.conn() as conn: conn.executescript(sql)
            failure = None
            try:
                method(collector, deepcopy(batch), None, collector.epoch, False)
            except Exception as exc:
                if not fail: raise
                failure = (type(exc).__name__, str(exc))
            with store.conn() as conn:
                names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'universal_collection_%' OR name LIKE 'feature_%') ORDER BY name")]
                tables = {name: sorted((tuple(row) for row in conn.execute('SELECT * FROM '+name)), key=repr) for name in names}
            return tables, failure

    def test_exact_receipt_bytes_hashes_and_native_rows(self):
        f = self.fixture.f
        self.fixture.declare([], empty_reason='independent fixture sensor')
        f.prepare(); intent, evidence = f.bind()
        f.collector.decision(intent, f.agent, f.state, required=('binary_sensor.motion',), affected=(), predecision=evidence)
        original = deepcopy(list(f.collector.pending))
        for variant in ('complete','incomplete','binding','mask','generation','model','epoch','topology','declaration','duplicate','metadata','rollback','trigger','ddl','aba'):
            batch = deepcopy(original); payload = batch[0][2]; sql = None
            if variant == 'incomplete': payload['predecision'] = None; payload['receipt'] = None
            elif variant == 'binding': payload['predecision']['decision_id'] = 'other'
            elif variant == 'mask': payload['predecision']['legal_mask']['actions'] = []
            elif variant == 'generation': payload['predecision']['generation_id'] = None
            elif variant == 'model': payload['predecision']['model_revision'] = 'stale'
            elif variant == 'epoch': payload['predecision']['epoch'] += 1
            elif variant == 'topology': payload['predecision']['topology'] = 'other'
            elif variant == 'declaration': payload['affected_sensor_evidence'] = None
            elif variant == 'duplicate': batch *= 3
            elif variant == 'metadata': batch += [(f.collector.epoch,'notice','test notice',1,0.)] + deepcopy(batch)
            elif variant == 'rollback': sql = "CREATE TRIGGER fail_receipt BEFORE INSERT ON universal_collection_decisions BEGIN SELECT RAISE(ABORT,'receipt rollback'); END;"
            elif variant == 'trigger': sql = "CREATE TRIGGER change_native AFTER INSERT ON universal_collection_decisions BEGIN UPDATE agents SET name=name; END;"
            elif variant == 'ddl': sql = 'CREATE TABLE schema_change(value);'
            elif variant == 'aba': sql = 'UPDATE agents SET name=name; UPDATE agents SET name=name;'
            with self.subTest(variant=variant):
                with patch.object(affected, 'validate', old_validate):
                    before = self.arm(old_write, batch, sql, variant=='rollback')
                after = self.arm(writer.HistoricalTrainingCollector._write, batch, sql, variant=='rollback')
                self.assertEqual(before, after)
                if variant == 'complete':
                    rows = after[0]['universal_collection_decisions']
                    self.assertEqual(len(rows), 1)
                    document = json.loads(rows[0][4])
                    self.assertIsNotNone(document['receipt'])
                    self.assertIsNone(document['missing_proof'])
                    self.assertEqual(document['receipt']['version'], 2)
                if variant == 'rollback': self.assertIsNotNone(after[1])

    def test_derived_inputs_and_rejection_equivalence(self):
        f = self.fixture.f
        self.fixture.declare([], empty_reason='independent fixture sensor')
        f.prepare(); _, parent = f.bind()
        evidence = parent['affected_sensor_evidence']
        universe = ['binary_sensor.motion']
        candidate = getattr(affected, '_PreparedSensorUniverse', None)
        prepared = candidate(universe) if candidate is not None else list(universe)
        universe.append('sensor.changed')
        if candidate is not None:
            self.assertEqual(prepared.members, frozenset(['binary_sensor.motion']))
        else:
            # Rejected experiments keep their frozen source at 4dc2d2d6.
            # Continue testing live validation against the frozen oracle after
            # restoring production; no experimental type is needed at runtime.
            self.assertEqual(prepared, ['binary_sensor.motion'])
        for field,value in [(None,None),('decision_id','bad'),('generation_id','bad'),('complete',False),('information_available_ts',float('nan'))]:
            changed = deepcopy(evidence)
            if field: changed[field] = value
            results = []
            for function,source in [(old_validate,['binary_sensor.motion']), (affected.validate,prepared)]:
                try: results.append(('ok',function(changed,parent,sensor_universe=source)))
                except (ValueError,KeyError,TypeError) as exc: results.append((type(exc).__name__,str(exc)))
            self.assertEqual(*results)
