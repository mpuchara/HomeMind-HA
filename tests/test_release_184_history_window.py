"""Upgrade and retention boundaries for the fourteen-day training window."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
import history
import settings
from storage import Store
from storage_retention import StorageRetention


class HistoryWindow184Tests(unittest.TestCase):
    def test_upgrade_migrates_ten_day_defaults_and_preserves_shorter_windows(self):
        keys = ('agent_training_history_days', 'agent_training_long_memory_days',
                'archive_retention_days', 'history_bootstrap_days', 'auto_agent_recent_days')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'options.json'
            with patch.object(settings, 'OPTIONS_PATH', path):
                self.assertTrue(all(settings.load_options()[key] == 14 for key in keys))
                path.write_text(json.dumps(dict.fromkeys(keys, 10)), encoding='utf-8')
                self.assertTrue(all(settings.load_options()[key] == 14 for key in keys))
                path.write_text(json.dumps(dict.fromkeys(keys, 5)), encoding='utf-8')
                self.assertTrue(all(settings.load_options()[key] == 5 for key in keys))

    def test_training_bounds_include_fourteen_days_but_never_missing_archive(self):
        end = 50 * 86400.0
        store = SimpleNamespace(archive_stats=lambda: {'min_ts': end - 20 * 86400, 'max_ts': end})
        with patch.object(history, 'STORE', store), patch.object(history, 'now_ts', return_value=end):
            with patch.dict(history.OPTIONS, agent_training_history_days=14):
                self.assertEqual(history.HistoryManager._training_bounds(None), (end - 14 * 86400, end))
                store.archive_stats = lambda: {'min_ts': end - 3 * 86400, 'max_ts': end}
                self.assertEqual(history.HistoryManager._training_bounds(None), (end - 3 * 86400, end))

    def test_retention_keeps_days_eleven_to_fourteen_and_causal_seed(self):
        now = 50 * 86400.0
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'data.db')
            try:
                with store.conn() as connection:
                    connection.execute('CREATE TABLE provenance_events(event_time REAL, payload TEXT)')
                    connection.executemany('INSERT INTO provenance_events VALUES(?,?)', [
                        (now - age * 86400, str(age)) for age in (15, 14, 13, 11, 1)])
                    connection.executemany("INSERT INTO entity_history(entity_id,ts,state,source) VALUES('sensor.a',?,?,'live')", [
                        (now - age * 86400, str(age)) for age in (17, 16, 15, 14, 13, 11, 1)])
                worker = StorageRetention(store)
                worker.run_once(now)
                with store.conn() as connection:
                    self.assertEqual([row[0] for row in connection.execute('SELECT payload FROM provenance_events ORDER BY event_time')], ['14', '13', '11', '1'])
                    self.assertEqual([row[0] for row in connection.execute('SELECT state FROM entity_history ORDER BY ts')], ['15', '14', '13', '11', '1'])
                self.assertEqual(worker.snapshot()['history_days'], 14)
            finally:
                store.stop_storage_retention()
                store.stop_wal_checkpoint()
                store.stop_wal_keeper()


if __name__ == '__main__':
    unittest.main()
