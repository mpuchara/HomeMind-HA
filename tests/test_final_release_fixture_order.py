"""A DB creation-order difference must not change offered benchmark work."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import support
sys.path.insert(0,str(support.ROOT/'tools'))
from storage import Store
from universal_e2e_fixture import OriginalEvidenceScenario


class FixtureOrderTests(unittest.TestCase):
    def test_declared_target_order_survives_reversed_store_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'fixture.db')
            original=store.list_agent_configs
            def reversed_configs():
                return sorted(original(),key=lambda a:a['target_entity'],reverse=True)
            with patch.object(store,'list_agent_configs',reversed_configs):
                with OriginalEvidenceScenario(store,sensors=5,devices=5) as scenario:
                    self.assertEqual([a['target_entity'] for a in scenario.agents],scenario.targets)
                    self.assertEqual(list(scenario.engine.agent_configs),[a['id'] for a in scenario.agents])
