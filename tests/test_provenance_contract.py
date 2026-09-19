import copy
from datetime import datetime, timezone
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import agent, state
from provenance import CONTRACT_VERSION, ProvenanceJournal, UNKNOWN
from provenance_runtime import install as install_provenance
from storage import Store
import test_executor as executor_fixture


class ProvenanceJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.store = Store(self.path)
        self.now = 1000.0
        self.journal = ProvenanceJournal(self.store, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_contract_is_additive_and_legacy_history_stays_unknown(self):
        self.store.archive_upsert('light.kitchen', 100.0, 'on', {}, user_id='legacy-user', source='live')
        fresh = ProvenanceJournal(self.store, clock=lambda: self.now)
        row = fresh.history_provenance('light.kitchen', 100.0)
        self.assertEqual(row['origin'], UNKNOWN)
        self.assertEqual(row['source'], UNKNOWN)
        self.assertIsNone(row['event_id'])
        with self.store.conn() as c:
            versioned = c.execute('PRAGMA table_info(provenance_events)').fetchall()
        self.assertTrue(any(r[1] == 'processed_time' for r in versioned))

    def test_command_context_evidence_survives_restart(self):
        a = agent()
        command_id = self.journal.reserve_command(a, 1.0, decision_id='decision-1')
        response = [state('light.kitchen', 'on') | {'context': {'id': 'ctx-command', 'parent_id': None}}]
        self.journal.dispatch_command(command_id, response=response)

        fresh = ProvenanceJournal(Store(self.path), clock=lambda: self.now + 1)
        observed = state('light.kitchen', 'on') | {'context': {'id': 'ctx-command', 'parent_id': None}}
        match = fresh.match_command_state(observed)

        self.assertIsNotNone(match)
        self.assertEqual(match['decision_id'], 'decision-1')
        self.assertEqual(match['command_origin'], 'own_command')

    def test_duplicate_event_and_experience_are_idempotent(self):
        st = state('binary_sensor.pir', 'on') | {
            'context': {'id': 'ctx-pir', 'parent_id': None, 'user_id': 'human'}
        }
        event_id, first = self.journal.record_event(
            'binary_sensor.pir', st, event_time=10.0, received_time=11.0,
            source='ha_state_changed', origin='user')
        same_id, second = self.journal.record_event(
            'binary_sensor.pir', st, event_time=10.0, received_time=12.0,
            source='ha_state_changed', origin='user')
        self.assertEqual(same_id, event_id)
        self.assertTrue(first)
        self.assertFalse(second)

        self.assertTrue(self.journal.record_experience(
            experience_key='feedback:one', agent_id='a', source='test', origin='user', reward=1.0))
        self.assertFalse(self.journal.record_experience(
            experience_key='feedback:one', agent_id='a', source='test', origin='user', reward=1.0))
        self.assertEqual(len(self.journal.list_experiences('a')), 1)

    def test_experience_batch_is_idempotent_and_uses_one_audit_contract(self):
        rows = [
            {
                "experience_key": f"batch:{i}",
                "agent_id": "a",
                "source": "historical_replay",
                "origin": "unknown",
                "action_index": i % 2,
                "action_value": float(i % 2),
                "reward": 1.0,
                "features": {0: float(i)},
                "metadata": {"i": i},
            }
            for i in range(16)
        ]
        self.assertEqual(self.journal.record_experiences_batch(rows), 16)
        self.assertEqual(self.journal.record_experiences_batch(rows), 0)
        self.assertEqual(len(self.journal.list_experiences("a")), 16)

    def test_action_probability_is_optional_and_never_invented(self):
        self.journal.record_decision(
            decision_id='none', created_time=1.0, agent_id='a',
            feature_manifest={'features': {}}, allowed_actions=[0, 1], action_probability=None)
        self.journal.record_decision(
            decision_id='known', created_time=2.0, agent_id='a',
            feature_manifest={'features': {}}, allowed_actions=[0, 1], action_probability=.25)
        self.assertIsNone(self.journal.decision('none')['action_probability'])
        self.assertEqual(self.journal.decision('known')['action_probability'], .25)
        self.assertEqual(self.journal.decision('known')['contract_version'], CONTRACT_VERSION)

    def test_shadow_decisions_can_be_persisted_as_one_final_status_batch(self):
        rows = [
            {
                "decision_id": f"shadow-{i}",
                "created_time": 10.0 + i,
                "agent_id": "a",
                "model_version": 12,
                "model_revision": "r1",
                "schema_version": 12,
                "reward_version": 1,
                "feature_manifest": {"features": {"0": float(i)}},
                "allowed_actions": [0.0, 1.0],
                "chosen_action": float(i % 2),
                "model_desired": float(i % 2),
                "dispatch_status": "SHADOW",
                "dispatch_reason": "observed only",
            }
            for i in range(3)
        ]
        self.assertEqual(self.journal.record_decisions_batch(rows), 3)
        for i in range(3):
            decision = self.journal.decision(f"shadow-{i}")
            self.assertEqual(decision["dispatch_status"], "SHADOW")
            self.assertEqual(decision["dispatch_reason"], "observed only")
        self.assertEqual(self.journal.record_decisions_batch(rows), 0)

    def test_manual_experience_survives_journal_restart(self):
        st = state('light.kitchen', 'off') | {
            'context': {'id': 'manual-ctx', 'parent_id': None, 'user_id': 'human'}
        }
        event_id, _ = self.journal.record_event(
            'light.kitchen', st, event_time=20.0, received_time=20.1,
            source='ha_state_changed', origin='user')
        self.assertTrue(self.journal.record_experience(
            experience_key='manual:20', agent_id='a', source='manual_demonstration',
            origin='user', source_event_id=event_id, action_index=0, action_value=0,
            reward=1.0, features={0: 1.0}))

        fresh = ProvenanceJournal(Store(self.path), clock=lambda: self.now + 10)
        rows = fresh.list_experiences('a')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['origin'], 'user')
        self.assertEqual(rows[0]['source_event_id'], event_id)


class ProvenanceRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_fixture.ExecutorTests()
        self.fixture.setUp()
        self.core = SimpleNamespace(STORE=self.fixture.store, ENGINE=self.fixture.e)
        install_provenance(self.core)

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def _stamp(st):
        stamp = datetime.now(timezone.utc).isoformat()
        st['last_changed'] = stamp
        st['last_updated'] = stamp
        return st

    def _dispatch_and_ack(self):
        f = self.fixture
        intent = f.intent()
        result = f.e.executor.submit(intent, {0: 1.0}, 1)
        self.assertEqual(result['status'], 'ACCEPTED')

        ack = self._stamp(state('light.kitchen', 'on'))
        ack['context'] = {'id': 'ctx-own-ack', 'parent_id': None, 'user_id': None}
        f.e.on_state_changed({'entity_id': 'light.kitchen', 'new_state': ack})
        f.e.flush_archive()
        f.e.process_agent(f.a, f.e.state_map, {'light.kitchen'})
        return intent

    def test_shadow_provenance_is_deferred_off_the_executor_hot_path(self):
        f = self.fixture
        f.store.update_agent(f.a["id"], {"mode": "shadow"})
        shadow = f.store.get_agent_config(f.a["id"])
        f.e.agent_configs = {f.a["id"]: dict(shadow)}

        original_single = f.e.provenance.record_decision
        original_batch = f.e.provenance.record_decisions_batch
        single = Mock(side_effect=AssertionError("Shadow must not synchronously insert provenance"))
        batch = Mock(return_value=1)
        f.e.provenance.record_decision = single
        f.e.provenance.record_decisions_batch = batch
        try:
            result = f.e.executor.submit(f.intent(), {0: 1.0}, 1)
            self.assertEqual(result["status"], "SHADOW")
            single.assert_not_called()
            snapshot = f.e.provenance_deferred_snapshot()
            self.assertGreaterEqual(snapshot["queued"], 1)
            f.store._flush_provenance_decisions(f.a["id"])
            batch.assert_called()
            payloads = batch.call_args.args[0]
            self.assertTrue(payloads)
            self.assertEqual(payloads[-1]["dispatch_status"], "SHADOW")
            self.assertEqual(payloads[-1]["agent_id"], f.a["id"])
        finally:
            f.e.provenance.record_decision = original_single
            f.e.provenance.record_decisions_batch = original_batch

    def test_full_decision_dispatch_ack_outcome_relationship(self):
        f = self.fixture
        intent = self._dispatch_and_ack()
        rt = f.e.runtime[f.a['id']]
        self.assertEqual(rt['pending']['decision_id'], intent.intent_id)
        self.assertIsNotNone(rt['pending']['acknowledged_ts'])

        f.e._reward_pending(f.a, rt, .2, 'settled acceptance')
        decision = f.e.provenance.decision(intent.intent_id)

        self.assertEqual(decision['decision_id'], intent.intent_id)
        self.assertEqual(decision['agent_id'], f.a['id'])
        self.assertEqual(decision['model_version'], f.model.VERSION)
        self.assertIsNotNone(decision['schema_version'])
        self.assertEqual(decision['reward_version'], f.e.executor.reward_engine.VERSION)
        self.assertIn('features', decision['feature_manifest'])
        self.assertEqual(decision['allowed_actions'], list(f.model.actions))
        self.assertEqual(decision['chosen_action'], 1.0)
        self.assertEqual(decision['dispatch_status'], 'ACCEPTED')
        self.assertIsNotNone(decision['dispatch_time'])
        self.assertEqual(decision['dispatch_value'], 1.0)
        self.assertIsNotNone(decision['ack_event_id'])
        self.assertIsNotNone(decision['ack_time'])
        self.assertEqual(decision['outcome_reward'], .2)
        self.assertEqual(decision['outcome_reason'], 'settled acceptance')
        self.assertEqual(decision['episode_id'], intent.intent_id)
        self.assertIsNone(decision['action_probability'])

    def test_own_command_ack_is_not_replayed_as_demonstration(self):
        f = self.fixture
        intent = self._dispatch_and_ack()
        rows = f.store.archive_rows(entity_id='light.kitchen')
        self.assertTrue(rows)
        target_row = rows[-1]
        provenance = f.e.provenance.history_provenance('light.kitchen', target_row['ts'])
        self.assertEqual(provenance['origin'], 'own_command')

        learned = f.store.add_historical_experience(
            f.a['id'], target_row['id'], 1, 1.0, 1.0, 30.0, {0: 1.0}, user_id=None)
        self.assertFalse(learned)
        self.assertEqual(f.store.list_historical_experiences(f.a['id']), [])
        excluded = [r for r in f.e.provenance.list_experiences(f.a['id'])
                    if r['source'] == 'historical_replay_excluded']
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]['decision_id'], None)
        self.assertEqual(excluded[0]['origin'], 'own_command')
        self.assertIsNotNone(intent.intent_id)

    def test_own_command_ack_is_excluded_by_batched_replay_too(self):
        f = self.fixture
        self._dispatch_and_ack()
        rows = f.store.archive_rows(entity_id="light.kitchen")
        self.assertTrue(rows)
        target_row = rows[-1]
        mapping = f.store.historical_replay_provenance(
            target_row["ts"] - 1.0, target_row["ts"] + 1.0, ["light.kitchen"]
        )
        self.assertEqual(mapping[target_row["id"]]["origin"], "own_command")

        inserted = f.store.add_historical_experiences_batch([{
            "agent_id": f.a["id"],
            "target_history_id": target_row["id"],
            "action_index": 1,
            "action_value": 1.0,
            "reward": 1.0,
            "dwell_seconds": 30.0,
            "features": {0: 1.0},
            "user_id": None,
            "_provenance": mapping[target_row["id"]],
        }])
        self.assertEqual(inserted, 0)
        self.assertEqual(f.store.list_historical_experiences(f.a["id"]), [])
        excluded = [
            r for r in f.e.provenance.list_experiences(f.a["id"])
            if r["source"] == "historical_replay_excluded"
        ]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["origin"], "own_command")

    def test_duplicate_feedback_key_does_not_apply_second_model_update(self):
        f = self.fixture
        intent = self._dispatch_and_ack()
        rt = f.e.runtime[f.a['id']]
        pending = copy.deepcopy(rt['pending'])
        before = len(f.store.list_feedback(f.a['id'], limit=100))

        self.assertTrue(f.e._reward_pending(f.a, rt, .2, 'same outcome', experience=pending))
        model_after_first = copy.deepcopy(f.e.policy(f.a).serialize())
        self.assertFalse(f.e._reward_pending(f.a, rt, .2, 'same outcome', experience=pending))
        model_after_second = f.e.policy(f.a).serialize()

        self.assertEqual(model_after_second, model_after_first)
        self.assertEqual(len(f.store.list_feedback(f.a['id'], limit=100)), before + 1)
        matching = [r for r in f.e.provenance.list_experiences(f.a['id'])
                    if r['experience_key'] == f"feedback:{intent.intent_id}:same outcome"]
        self.assertEqual(len(matching), 1)

    def test_user_manual_correction_and_provenance_survive_restart(self):
        f = self.fixture
        f.e.state_map['light.kitchen'] = state('light.kitchen', 'on')
        f.e.runtime[f.a['id']] = {'previous_target': 1.0, 'last_inference_ts': time.time()}
        manual = self._stamp(state('light.kitchen', 'off'))
        manual['context'] = {'id': 'ctx-human', 'parent_id': None, 'user_id': 'human'}

        f.e.on_state_changed({'entity_id': 'light.kitchen', 'new_state': manual})
        f.e.flush_archive()
        f.e.process_agent(f.a, f.e.state_map, {'light.kitchen'})
        feedback = [row for row in f.store.list_feedback(f.a['id'], limit=20)
                    if 'manual demonstration' in str(row.get('reason') or '')]
        self.assertTrue(feedback)

        restarted_store = Store(f.store.path)
        restarted = ProvenanceJournal(restarted_store)
        feedback_after = [row for row in restarted_store.list_feedback(f.a['id'], limit=20)
                          if 'manual demonstration' in str(row.get('reason') or '')]
        self.assertTrue(feedback_after)
        events = restarted.list_experiences(f.a['id'])
        self.assertTrue(any(row['origin'] == 'user' for row in events))

    def test_unknown_no_user_change_remains_unknown(self):
        f = self.fixture
        unknown = self._stamp(state('light.kitchen', 'on'))
        unknown['context'] = {'id': 'ctx-device', 'parent_id': None, 'user_id': None}
        f.e.on_state_changed({'entity_id': 'light.kitchen', 'new_state': unknown})
        f.e.flush_archive()
        event_id = f.e._provenance_latest_events['light.kitchen'][1]
        self.assertEqual(f.e.provenance.event(event_id)['origin'], UNKNOWN)


if __name__ == '__main__':
    unittest.main()
