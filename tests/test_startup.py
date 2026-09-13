"""Exercise the container entry point without starting HA or background workers."""
import runpy
import unittest
from unittest.mock import patch
from support import ROOT


class StartupTests(unittest.TestCase):
    def test_script_starts_http_server_and_closes_workers(self):
        with patch('engine.Engine.start') as engine_start, \
             patch('engine.HAEventStream.start') as websocket_start, \
             patch('history.HistoryManager.start') as history_start, \
             patch('http.server.ThreadingHTTPServer') as server_class, \
             patch('os.nice', create=True), patch('builtins.print'):
            server = server_class.return_value
            server.serve_forever.side_effect = KeyboardInterrupt
            runtime = runpy.run_path(str(ROOT/'adaptive_ai/src/main.py'), run_name='__main__')
            engine_start.assert_called_once()
            websocket_start.assert_called_once()
            history_start.assert_called_once()
            server_class.assert_called_once_with(('0.0.0.0', 8099), runtime['Handler'])
            server.serve_forever.assert_called_once()
            server.server_close.assert_called_once()
            self.assertTrue(runtime['ENGINE'].stop_event.is_set())
            self.assertIs(runtime['ENGINE'].history_manager, runtime['HISTORY'])
            self.assertIsNotNone(runtime['ENGINE'].home_bootstrap)


if __name__ == '__main__':
    unittest.main()
