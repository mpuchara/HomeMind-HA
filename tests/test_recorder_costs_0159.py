"""Recorder timing observations must not alter payload, cache, or row semantics."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import history
from ha import HAClient
from storage import Store
from recorder_metrics import scope


class RecorderCostsTests(unittest.TestCase):
    def test_real_http_sqlite_import_and_second_agent_reuses_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'archive.db')
            stamp=1700000000.
            payload=[[dict(entity_id='binary_sensor.motion',state='on' if i%2 else 'off',
                last_changed=datetime.fromtimestamp(stamp+i*30,timezone.utc).isoformat(),attributes={})
                for i in range(200)]]
            raw=json.dumps(payload).encode()
            requests=[]
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append(True)
                    self.send_response(200)
                    self.send_header('Content-Length',str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                def log_message(self,*args):
                    pass
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                client=HAClient('http://127.0.0.1:'+str(server.server_port),'')
                engine=SimpleNamespace(lock=threading.RLock(),state_map={},entity_registry={})
                with patch.object(history,'STORE',store),patch.object(history,'HA',client):
                    manager=history.HistoryManager(engine,session_mode=True)
                    manager.training_job_agent_id='first'
                    args=dict(batch_size=1,minimal=False,no_attributes=False,source='ha_history_full',label='target',max_hours=6)
                    self.assertEqual(manager._training_recorder_import(['binary_sensor.motion'],stamp-1,stamp+6000,**args),200)
                    metrics=manager.training_job_timings['recorder_costs']
                    for key in ('http_response_headers_wait_seconds','http_body_read_seconds','json_parse_seconds',
                                'sqlite_insert_and_dedup_seconds','sqlite_commit_and_release_seconds',
                                'coverage_persistence_seconds','section_wall_seconds'):
                        self.assertGreaterEqual(metrics[key],0.,key)
                    self.assertEqual(metrics['response_bytes'],len(raw))
                    manager.training_job_agent_id='second'
                    self.assertEqual(manager._training_recorder_import(['binary_sensor.motion'],stamp-1,stamp+6000,**args),0)
                    self.assertEqual(len(requests),1)
                    fresh=history.HistoryManager(engine)
                    self.assertTrue(fresh.training_recorder_coverage)
                    self.assertEqual(fresh._training_recorder_import(['binary_sensor.motion'],stamp-1,stamp+6000,**args),0)
                    self.assertEqual(len(requests),1)
                    self.assertEqual(store.archive_count(),200)
                    self.__class__.last_metrics=dict(metrics)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__=='__main__':
    unittest.main()
