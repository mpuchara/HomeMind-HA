"""Exercise the container entry point without starting HA or background workers."""
import os
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

    def test_supervisor_ingress_accepts_only_trusted_proxy_by_default(self):
        runtime = runpy.run_path(str(ROOT/'adaptive_ai/src/main.py'), run_name='ingress_test_module')
        handler = runtime['Handler'].__new__(runtime['Handler'])
        with patch.dict(os.environ, {'SUPERVISOR_TOKEN':'test-token'}, clear=False):
            handler.client_address=('172.30.32.2', 41000)
            self.assertTrue(handler.trusted_client())
            handler.client_address=('127.0.0.1', 41000)
            self.assertTrue(handler.trusted_client())
            handler.client_address=('172.30.33.9', 41000)
            self.assertFalse(handler.trusted_client())

    def test_ingress_proxy_allowlist_can_be_overridden(self):
        runtime = runpy.run_path(str(ROOT/'adaptive_ai/src/main.py'), run_name='ingress_override_test_module')
        handler = runtime['Handler'].__new__(runtime['Handler'])
        env={'SUPERVISOR_TOKEN':'test-token','ADAPTIVE_AI_TRUSTED_PROXY_IPS':'10.1.2.3'}
        with patch.dict(os.environ, env, clear=False):
            handler.client_address=('10.1.2.3', 41000)
            self.assertTrue(handler.trusted_client())
            handler.client_address=('172.30.32.2', 41000)
            self.assertFalse(handler.trusted_client())


if __name__ == '__main__':
    unittest.main()
