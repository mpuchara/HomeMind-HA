from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import storage
from storage import Store
from candidate_http_routes import register_routes
from runtime_http import ExplicitRouteRegistry
from test_release_140_candidate_explicit_routes import Http


class CandidateRequestConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'request.db')
        self.store.meta_set('request-test', 'before')

    def dispatch(self, callback):
        registry = ExplicitRouteRegistry()
        register_routes(registry, SimpleNamespace(STORE=self.store),
                        SimpleNamespace(list_status=callback))
        http = Http('/api/candidates')
        original = http.send_json
        def send(status, payload):
            self.assertIsNone(getattr(self.store._connection_session, 'connection', None))
            return original(status, payload)
        http.send_json = send
        registry.dispatch('GET', http)
        return http.result

    def test_one_connection_retains_independent_fresh_reads_and_closes_before_response(self):
        connect = sqlite3.connect
        opened = []
        def count(*args, **kwargs):
            opened.append(True)
            return connect(*args, **kwargs)
        def cards():
            def read():
                with self.store.conn() as connection:
                    return connection.execute("SELECT value FROM app_meta WHERE key='request-test'").fetchone()[0]
            first = read()
            # Another writer must not wait on a request-long read transaction.
            with closing(connect(self.store.path, timeout=.1)) as other, other:
                other.execute("UPDATE app_meta SET value='after' WHERE key='request-test'")
            return [dict(first=first, second=read())]
        with patch.object(storage.sqlite3, 'connect', side_effect=count):
            result = self.dispatch(cards)
        self.assertEqual(len(opened), 1)
        self.assertEqual(result, ('json', 200, {'candidates': [{'first': 'before', 'second': 'after'}]}))
        with self.store.conn() as connection:
            self.assertEqual(connection.execute("SELECT value FROM app_meta WHERE key='request-test'").fetchone()[0], 'after')

    def test_error_closes_scope_without_undoing_independent_committed_helpers(self):
        def cards():
            self.store.meta_set('request-test', 'committed')
            raise ValueError('card failed')
        with self.assertRaisesRegex(ValueError, 'card failed'):
            self.dispatch(cards)
        self.assertIsNone(getattr(self.store._connection_session, 'connection', None))
        self.assertEqual(self.store.meta_get('request-test'), 'committed')


if __name__ == '__main__':
    unittest.main()
