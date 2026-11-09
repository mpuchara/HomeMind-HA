"""Compare all journal rows with the unmodified native writer oracle."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import observation_contract as contract
from storage import Store
from storage_budget import BudgetCursor

namespace = dict(vars(contract))
exec(compile((Path(__file__).parent/'reference/journal_record_batch_79bf0674.py').read_text(),
             'journal_record_batch_79bf0674.py', 'exec'), namespace)
oracle = namespace['record_batch']


class NativeJournalDifferentialTests(unittest.TestCase):
    def run_arm(self, method, windows, count, fail=False, trigger=False):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'data.db')
            journal = contract.FeatureJournal(store, clock=lambda: 100.)
            with store.conn() as c:
                for index, (start, end, expiry, entity) in enumerate(windows):
                    c.execute('INSERT INTO feature_windows VALUES(?,12,?,50,?,?,?,50,?)',
                              (str(index), 'agent-'+str(index%2), start, end, 'test', expiry))
                    c.execute('INSERT INTO feature_window_entities VALUES(?,?)', (str(index), entity))
                if fail:
                    c.execute("CREATE TRIGGER reject_observation BEFORE INSERT ON feature_observation_events BEGIN SELECT RAISE(ABORT,'test failure'); END")
                if trigger:
                    c.execute("""CREATE TRIGGER protect_after_first AFTER INSERT ON feature_observation_events
                        BEGIN INSERT OR IGNORE INTO feature_windows VALUES('trigger',12,'agent',50,0,500,'test',50,999);
                        INSERT OR IGNORE INTO feature_window_entities VALUES('trigger','sensor.s0'); END""")
            records = [dict(entity_id='sensor.s'+str(i%3), state=dict(state=str(i), attributes={'unit_of_measurement':'W'}),
                            event_time=float(i%100), received_time=100., source='test') for i in range(count)]
            with patch.object(contract.FeatureJournal, 'record_batch', method):
                if fail:
                    with self.assertRaisesRegex(Exception, 'test failure'):
                        journal.record_batch(records)
                    keys = []
                else:
                    keys = journal.record_batch(records)
                    keys += journal.record_batch(records[:7])  # exact duplicates
                    keys += journal.record_batch([dict(entity_id='sensor.s0', state={'state':'late'},
                                                       event_time=5., received_time=101., source='late')])
            with store.conn() as c:
                tables = {table: [tuple(row) for row in c.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                          for table in ('feature_observation_events', 'feature_windows', 'feature_window_entities')}
            return keys, tables

    def test_exact_native_rows_and_keys(self):
        scenarios = [[], [(0,100,500,'sensor.s0')],
                     [(0,70,500,'sensor.s0'), (30,100,700,'sensor.s0'), (5,90,800,'sensor.s1')],
                     [(0,10,500,'sensor.s0'), (90,100,700,'sensor.s0')]]
        for windows in scenarios:
            for count in (1, 128, 300):
                with self.subTest(windows=windows, count=count):
                    self.assertEqual(self.run_arm(oracle, windows, count),
                                     self.run_arm(contract.FeatureJournal.record_batch, windows, count))

    def test_insertion_exception_rolls_back_exactly(self):
        self.assertEqual(self.run_arm(oracle, [], 30, fail=True),
                         self.run_arm(contract.FeatureJournal.record_batch, [], 30, fail=True))

    def test_intervening_native_trigger_is_visible_next_batch(self):
        self.assertEqual(self.run_arm(oracle, [], 30, trigger=True),
                         self.run_arm(contract.FeatureJournal.record_batch, [], 30, trigger=True))

    def test_external_window_writer_is_excluded_until_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.db'
            store = Store(path)
            journal = contract.FeatureJournal(store, clock=lambda: 100.)
            with store.conn() as c:
                c.execute("INSERT INTO feature_windows VALUES('w',12,'agent',50,0,100,'test',50,500)")
            other = sqlite3.connect(path, timeout=0)
            self.addCleanup(other.close)
            original = BudgetCursor.execute
            checks = []
            def observed(cursor, sql, parameters=()):
                result = original(cursor, sql, parameters)
                if sql == 'SELECT 1 FROM feature_window_entities LIMIT 1':
                    self.assertTrue(cursor.connection.in_transaction)
                    with self.assertRaises(sqlite3.OperationalError) as blocked:
                        other.execute("INSERT OR IGNORE INTO feature_window_entities VALUES('w','sensor.s0')")
                    self.assertEqual(blocked.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                    other.rollback()
                    checks.append(True)
                return result
            record = dict(entity_id='sensor.s0', state={'state':'on'}, event_time=10., received_time=100., source='test')
            with patch.object(BudgetCursor, 'execute', observed):
                journal.record_batch([record])
            self.assertEqual(checks, [True])
            other.execute("INSERT INTO feature_window_entities VALUES('w','sensor.s0')")
            other.commit()
            journal.record_batch([record])
            with store.conn() as c:
                self.assertEqual(c.execute('SELECT protected_until FROM feature_observation_events').fetchone()[0], 500.)
            other.close()  # Release the file before TemporaryDirectory on Windows.


if __name__ == '__main__':
    unittest.main()
