"""Native preflight is rejection-only; stale RAM never authorizes rejection."""
import unittest
from unittest.mock import patch

import test_universal_predecision as base


class CardinalityPreflightTests(unittest.TestCase):
    setUp = base.PredecisionTests.setUp
    drain = base.PredecisionTests.drain

    def tearDown(self):
        self.evidence.close()

    def hint(self):
        self.engine.agent_configs.update({f'hint-{i}': self.agent for i in range(256)})

    def native_agents(self, count):
        with self.store.conn() as c:
            columns = [row[1] for row in c.execute('PRAGMA table_info(agents)')]
            row = list(c.execute('SELECT * FROM agents WHERE id=?', (self.agent['id'],)).fetchone())
            index = columns.index('id')
            for i in range(count-1):
                values = list(row)
                values[index] = f'native-{i}'
                c.execute('INSERT INTO agents VALUES('+','.join('?' for _ in values)+')', values)

    def test_unsupported_rejects_before_ram_copy(self):
        self.native_agents(257)
        self.hint()
        self.evidence.request(self.agent['id'])
        with patch('universal_predecision.bounded_copy', side_effect=AssertionError('expensive copy')):
            with self.assertRaisesRegex(ValueError, 'agent bound'):
                self.evidence.prepare_one()
        self.assertFalse(self.evidence.prepared)
        self.assertIsNone(self.evidence.arbiter_snapshot.db)

    def test_stale_positive_hint_cannot_reject_supported_native_source(self):
        self.hint()
        base.PredecisionTests.prepare(self)
        self.assertIn(self.agent['id'], self.evidence.prepared)

    def test_stale_negative_hint_keeps_original_native_bound(self):
        self.native_agents(257)
        self.evidence.request(self.agent['id'])
        with self.assertRaisesRegex(ValueError, 'agent bound'):
            self.evidence.prepare_one()
        self.assertFalse(self.evidence.prepared)

    def test_exact_supported_native_limit_is_not_rejected(self):
        self.native_agents(256)
        self.hint()
        base.PredecisionTests.prepare(self)

    def test_native_change_after_rejection_is_not_cached(self):
        self.native_agents(257)
        self.hint()
        self.evidence.request(self.agent['id'])
        with self.assertRaisesRegex(ValueError, 'agent bound'):
            self.evidence.prepare_one()
        with self.store.conn() as c:
            c.execute('DELETE FROM agents WHERE id<>?', (self.agent['id'],))
        base.PredecisionTests.prepare(self)

    def test_rolled_back_native_deletion_does_not_allow_preparation(self):
        self.native_agents(257)
        self.hint()
        with self.store.conn() as c:
            c.execute('DELETE FROM agents WHERE id<>?', (self.agent['id'],))
            c.rollback()
        self.evidence.request(self.agent['id'])
        with self.assertRaisesRegex(ValueError, 'agent bound'):
            self.evidence.prepare_one()


if __name__ == '__main__':
    unittest.main()
