"""Exercise the container entry point without starting HA or background workers."""
import runpy
import unittest
from unittest.mock import Mock, patch
from support import ROOT


class StartupTests(unittest.TestCase):
    def test_http_server_binds_before_background_runtime(self):
        order = []
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt

        def server_factory(*args, **kwargs):
            order.append('http_bound')
            return server

        thread = Mock()
        thread.start.side_effect = lambda: order.append('runtime_thread_started')

        with patch('http.server.ThreadingHTTPServer', side_effect=server_factory) as server_class, \
             patch('threading.Thread', return_value=thread) as thread_class, \
             patch('os.nice', create=True), patch('builtins.print'):
            runtime = runpy.run_path(str(ROOT/'adaptive_ai/src/main.py'), run_name='__main__')

        server_class.assert_called_once_with(('0.0.0.0', 8099), runtime['Handler'])
        thread_class.assert_called_once()
        thread.start.assert_called_once()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()
        self.assertEqual(order[:2], ['http_bound', 'runtime_thread_started'])
        self.assertFalse(runtime['STARTUP']['ready'])
        self.assertEqual(runtime['STARTUP']['state'], 'http_ready')

    def test_status_is_available_before_runtime(self):
        runtime = runpy.run_path(str(ROOT/'adaptive_ai/src/main.py'), run_name='startup_test_module')
        handler = runtime['Handler'].__new__(runtime['Handler'])
        payload = handler.status_payload()
        self.assertIn('startup', payload)
        self.assertFalse(payload['startup']['ready'])
        self.assertEqual(payload['state_count'], 0)


if __name__ == '__main__':
    unittest.main()
