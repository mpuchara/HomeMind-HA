import unittest
from unittest.mock import Mock, patch

import support
import test_trial_knowledge as fixture


class TrialGate218Tests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TrialKnowledgeTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def call(self, agent, registry):
        return self.f.engine.experiments.propose(agent,object(),{},registry,{}, {},None,0.,[],1.,{})

    def test_disabled_standard_proposer_clears_prepared_without_registry_or_session_reads(self):
        f = self.f
        f.engine.experiments.configure(f.root,{'enabled':False})
        f.engine.experiments.prepared[f.root['id']] = {'old':'proposal'}
        f.engine.experiments.messages[f.root['id']] = 'No nearby action reachable: old context'
        registry = Mock(side_effect=AssertionError('unused registry'))
        with patch('trial_knowledge._session_for_live',side_effect=AssertionError('unused session')):
            self.assertIsNone(self.call(f.root,registry))
        self.assertNotIn(f.root['id'],f.engine.experiments.prepared)
        registry.assert_not_called()

    def test_shadow_never_revives_stale_information_probe_message_even_with_enabled_experiments(self):
        f = self.f
        f.engine.experiments.configure(f.root,{'enabled':True})
        f.engine.experiments.messages[f.root['id']] = 'No nearby action reachable'
        registry = Mock(side_effect=AssertionError('unused registry'))
        with patch('trial_knowledge._session_for_live',side_effect=AssertionError('Shadow session read')):
            self.assertIsNone(self.call({**f.root,'mode':'shadow'},registry))
        registry.assert_not_called()

    def test_enabled_control_keeps_the_full_registry_and_fresh_session_path(self):
        f = self.f
        f.engine.experiments.configure(f.root,{'enabled':True})
        registry = Mock(return_value={})
        chosen = {'value':0.,'index':0,'support':.9,'novelty':.1}
        with patch('trial_knowledge._session_for_live',return_value=None) as session:
            result = f.engine.experiments.propose(f.root,object(),{},registry,{}, {},chosen,0.,[],1.,{})
        self.assertIsNone(result)
        registry.assert_called_once_with()
        session.assert_called_once_with(f.store,str(f.root['id']))


if __name__ == '__main__':
    unittest.main()
