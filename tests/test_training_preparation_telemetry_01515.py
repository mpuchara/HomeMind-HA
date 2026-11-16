"""Stable non-blocking preparation telemetry under huge active entity streams."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

import support
from training_generation import Preparation


class PreparationTelemetryTests(unittest.TestCase):
    def test_history_counters_and_timestamp_cursor_are_distinct_from_overall_fraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            emitted=[]
            p=Preparation(Path(tmp)/'source.db',Path(tmp)/'dest.db',
                ['binary_sensor.radar','switch.light'],100.,100.+7*86400.,
                {},lambda:False,lambda **k:emitted.append(k),'abc')
            p.schema=[dict(name='entity_history')]*3
            p.state['queries']={'history:binary_sensor.radar':dict(done=False,last=[100.+3*86400.,999])}
            p.state['tables']={'entity_history':dict(query_key='history:binary_sensor.radar',
                current_cursor=[100.+3*86400.,999],source_rows_scanned=4107231,
                payload_rows_fetched=4107231,rows_copied=4107231,committed_batches=8133)}
            # No connection needed in this tiny test: the telemetry reads
            # only in-memory progress and must never trigger SQL.
            p.src=SimpleNamespace(in_transaction=False)
            with patch('training_generation.scratch_bytes',return_value=0):
                p.emit('entity_history','entity_time_keyset',True)
            row=emitted[-1]['preparation']
            self.assertEqual(row['history_entities_total'],2)
            self.assertEqual(row['history_entities_done'],0)
            self.assertEqual(row['history_current_entity'],'binary_sensor.radar')
            self.assertAlmostEqual(row['history_cursor_window_fraction'],3./7,places=4)
            self.assertEqual(row['payload_rows_fetched'],4107231)
            self.assertLessEqual(emitted[-1]['progress'],.09)

if __name__=='__main__':
    unittest.main()
