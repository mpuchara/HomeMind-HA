"""U05a trust, causality, undo, resource bounds and future-holdout gates."""
from dataclasses import replace, asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from universal_ha_adapter import CausalObservationReplay, JournalObservation, RegistryEpoch, RegistryTimeline, SemanticRegistry
from universal_training_data import (HistoricalDecision, ReplayCoverage, Supervision, build_dataset,
                                     chronological_split, prepare_dataset, read_supervision,
                                     read_archive_observations)
from test_universal_action_runtime import FakeDeviceAgent


def timeline():
    registry = SemanticRegistry.create(entities={'binary_sensor.motion': {'area_id': 'kitchen'}}, revision='r1')
    return RegistryTimeline([RegistryEpoch(1., 1., registry)])


def replay(rows=None, registry=None):
    rows = rows if rows is not None else [JournalObservation('motion', 'binary_sensor.motion', 10., 11., 'on', {})]
    return CausalObservationReplay(rows, registry_timeline=registry or timeline())


def receipt(at=20., agent_id='a', *, required=('binary_sensor.motion',), affected=(), target='light.kitchen'):
    agent = support.agent(id=agent_id, target_entity=target)
    state = support.state(target, 'off')
    service = FakeDeviceAgent()
    return HistoricalDecision.capture(
        decision_ts=at, event_ts=at, received_ts=at, registry_information_ts=1.,
        registry_revision='r1', agent=agent, descriptor=service.descriptor(agent),
        legal_mask=service.legal_action_mask(agent, state, [0., 1.]), state=state,
        required_sensor_ids=required, affected_sensor_ids=affected,
    )


def label(event='e1', at=20., **kwargs):
    return replace(Supervision(event, 'f-'+event, 'a', 'g1', None, at, at+1,
                               1., 'correct', True), **kwargs)


def coverage():
    return ReplayCoverage('fixture-snapshot-1', 1., 100.,
                          frozenset(('binary_sensor.motion', 'sensor.lux')), True)


def build(labels=None, decisions=None, history=None):
    labels = labels if labels is not None else [label()]
    decisions = decisions if decisions is not None else {('a', 20.): receipt()}
    return build_dataset(labels, decisions, history or replay(), coverage=coverage())


class UniversalTrainingDataTests(unittest.TestCase):
    def test_versioned_sample_uses_existing_contracts_and_is_reproducible(self):
        first, second = build(), build()
        self.assertEqual(first, second)
        self.assertEqual(len(first.samples), 1)
        sample = first.samples[0]
        self.assertEqual(sample.version, 1)
        self.assertEqual(sample.available_actions.count, 2)
        self.assertEqual(sample.home_observation[0].received_ts, 11.)
        self.assertEqual(sample.label_available_ts, 21.)
        self.assertEqual(json.loads(json.dumps(asdict(sample)))['desired_action'], 1.)

    def test_future_event_or_receipt_cannot_supply_a_required_sensor(self):
        for event, received in ((21., 11.), (10., 21.)):
            data = build(history=replay([JournalObservation('x', 'binary_sensor.motion', event, received, 'on', {})]))
            self.assertFalse(data.samples)
            self.assertIn('incomplete_history', data.rejected[0][1])

    def test_later_sensor_state_and_registry_are_not_input_to_earlier_correct(self):
        rows = [JournalObservation('before', 'binary_sensor.motion', 10., 11., 'off', {}),
                JournalObservation('after', 'binary_sensor.motion', 21., 21., 'on', {})]
        future = SemanticRegistry.create(entities={'binary_sensor.motion': {'area_id': 'bath'}}, revision='r2')
        registry = RegistryTimeline([RegistryEpoch(1., 1., timeline().as_of(20.)), RegistryEpoch(21., 22., future)])
        sample = build(history=replay(rows, registry)).samples[0]
        self.assertEqual(sample.home_observation[0].value, 0.)
        self.assertEqual(sample.home_observation[0].area_id, 'kitchen')
        self.assertEqual(sample.registry_revision, 'r1')

    def test_missing_registry_receipt_or_complete_required_set_rejects(self):
        for decisions, history in (({}, replay()),
                                   ({('a',20.): receipt()}, replay(registry=RegistryTimeline())),
                                   ({('a',20.): receipt(required=('binary_sensor.missing',))}, replay())):
            self.assertFalse(build(decisions=decisions, history=history).samples)

    def test_future_authority_metadata_rejects(self):
        for field in ('event_ts', 'received_ts', 'registry_information_ts'):
            self.assertFalse(build(decisions={('a',20.):replace(receipt(), **{field:21.})}).samples)

    def test_untrusted_home_mind_shadow_candidate_automation_and_outcome_never_train(self):
        for source in ('homemind', 'shadow', 'candidate', 'ha_automation', 'physical_outcome', 'unknown'):
            self.assertFalse(build([label(source=source)]).samples)

    def test_logical_event_is_counted_once_across_generation_copies(self):
        labels = [label(), label(feedback_id='copy', generation_id='g2')]
        data = build(labels)
        self.assertEqual(len(data.samples), 1)
        self.assertEqual(data.samples[0].sample_id, build().samples[0].sample_id)
        self.assertEqual(build(list(reversed(labels))), data)

    def test_undo_tombstone_removes_older_active_copy_before_dedup(self):
        for labels in ([label(), label(feedback_id='copy', active=False)],
                       [label(active=False), label(feedback_id='copy', generation_id='g2')]):
            data = build(labels)
            self.assertFalse(data.samples)
            self.assertNotEqual(data.supervision_revision, build().supervision_revision)

    def test_conflicting_duplicate_facts_reject(self):
        self.assertFalse(build([label(), label(desired_action=0.)]).samples)

    def test_desired_must_be_in_actual_historical_legal_action_set(self):
        self.assertFalse(build([label(desired_action=.5)]).samples)
        raw = json.loads(receipt().payload_json)
        raw['legal_mask']['actions'][1]['legal'] = False
        denied = replace(receipt(), payload_json=json.dumps(raw))
        self.assertFalse(build(decisions={('a',20.):denied}).samples)

    def test_affected_sensors_and_uncorrected_photometry_are_excluded(self):
        rows = [JournalObservation('m','binary_sensor.motion',10.,11.,'on',{}),
                JournalObservation('lux','sensor.lux',10.,11.,'100',{'unit_of_measurement':'lx'})]
        r = receipt(required=('binary_sensor.motion','sensor.lux'))
        data = build(decisions={('a',20.):r}, history=replay(rows))
        self.assertEqual([t.entity_id for t in data.samples[0].home_observation], ['binary_sensor.motion'])
        self.assertFalse(build(decisions={('a',20.):receipt(affected=('binary_sensor.motion',))}).samples)

    def test_entirely_unavailable_stale_or_zero_quality_context_rejects(self):
        for row in (JournalObservation('m','binary_sensor.motion',10.,11.,'unavailable',{}),
                    JournalObservation('m','binary_sensor.motion',-1000.,-999.,'on',{}),
                    JournalObservation('m','binary_sensor.motion',10.,11.,'on',{},quality=0.)):
            self.assertFalse(build(history=replay([row])).samples)

    def test_receipt_detaches_live_mutable_payload(self):
        saved = receipt()
        payload = json.loads(saved.payload_json)
        payload['agent']['target_entity'] = 'light.other'
        self.assertEqual(json.loads(saved.payload_json)['agent']['target_entity'], 'light.kitchen')

    def test_resource_caps_raise_instead_of_truncating(self):
        with self.assertRaises(ValueError):
            build_dataset([label(), label('e2')], {}, replay(), max_samples=1)
        with patch('universal_training_data.MAX_TOKEN_REFERENCES',1):
            with self.assertRaisesRegex(ValueError,'aggregate'):
                build([label(),label('e2')])
        with patch('universal_training_data.MAX_RECEIPT_BYTES',1):
            with self.assertRaisesRegex(OverflowError,'byte cap'):
                build()

    def test_dynamic_sensor_and_device_cardinalities(self):
        for sensors,devices in ((5,1),(50,10),(200,30),(1000,200)):
            ids=tuple(f'binary_sensor.s{i}' for i in range(sensors))
            rows=[JournalObservation(eid,eid,10.,11.,'on',{}) for eid in ids]
            labels=[label(f'e{i}',agent_id=f'a{i}') for i in range(devices)]
            decisions={(f'a{i}',20.):receipt(agent_id=f'a{i}',required=ids,target=f'light.lamp{i}')
                       for i in range(devices)}
            proof=ReplayCoverage('scale-fixture',1.,100.,frozenset(ids),True)
            data=build_dataset(labels,decisions,replay(rows),coverage=proof)
            self.assertEqual(len(data.samples),devices)
            self.assertTrue(all(len(s.home_observation)==sensors for s in data.samples))

    def test_continuous_and_categorical_desired_values_use_device_scale(self):
        for target,prop,lo,hi,desired,state,values in (
            ('climate.room','temperature',5.,35.,20.,
             support.state('climate.room','heat',temperature=18.),[15.,20.,25.]),
            ('select.mode','option_index',0.,1.,1.,
             support.state('select.mode','eco',options=['eco','comfort']),[0.,1.]),
        ):
            raw=json.loads(receipt().payload_json)
            raw['agent'].update(target_entity=target,target_property=prop,min_value=lo,max_value=hi)
            service=FakeDeviceAgent()
            raw.update(descriptor=service.descriptor(raw['agent']),state=state,
                       legal_mask=service.legal_action_mask(raw['agent'],state,values))
            data=build([label(desired_action=desired)],
                       {('a',20.):replace(receipt(),payload_json=json.dumps(raw))})
            self.assertEqual(len(data.samples),1)
            self.assertEqual(data.samples[0].desired_action,desired)

    def test_missing_or_gapped_coverage_is_not_inferred_from_a_sensor_seed(self):
        for proof in (None, replace(coverage(),complete=False),
                      replace(coverage(),complete_through_ts=19.),
                      replace(coverage(),start_ts=15.)):
            data=build_dataset([label()],{('a',20.):receipt()},replay(),coverage=proof)
            self.assertFalse(data.samples)

    def test_future_holdout_excludes_later_corrections_and_crossing_episodes(self):
        labels = [label('early',20.,available_ts=21.),
                  label('late-correct',22.,available_ts=40.),
                  label('train-episode',23.,episode_id='cross'),
                  label('hold-episode',40.,episode_id='cross'),
                  label('hold',41.,episode_id='independent')]
        decisions = {('a',l.decision_ts):receipt(l.decision_ts) for l in labels}
        data = build(labels, decisions)
        train, hold = chronological_split(data, train_until=30., holdout_from=35., holdout_until=50.)
        self.assertEqual([s.supervision_event_id for s in train], ['early'])
        self.assertEqual([s.supervision_event_id for s in hold], ['hold'])
        with self.assertRaises(ValueError):
            chronological_split(data, train_until=30., holdout_from=30., holdout_until=50.)


class ProductionJournalTrainingTests(unittest.TestCase):
    def setUp(self):
        from storage import Store
        from manual_feedback_contract import ManualFeedbackJournal
        from teaching_rl import RLTeaching
        from correct_data_foundation import _ensure_schema
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name)/'data.db')
        self.journal = ManualFeedbackJournal(self.store, clock=lambda:30.)
        RLTeaching(self.store, SimpleNamespace())
        _ensure_schema(self.store)
        self.feedback = self.journal.record(agent_id='a',selected_ts=20.,source='correct',correct_action=1.)
        with self.store.conn() as c:
            cursor=c.execute('''INSERT INTO teaching_rl_labels
                (agent_id,created_ts,sample_ts,desired,fingerprint,supervision_event_id)
                VALUES('a',30,20,1,'fingerprint','existing-event')''')
            label_id=cursor.lastrowid
        self.journal.link(self.feedback['feedback_id'],'learning','teach_rl_label',label_id)

    def test_reader_preserves_existing_supervision_identity_and_actual_undo(self):
        with self.store.conn() as c:
            before = read_supervision(c,['a'],as_of_ts=40.)
        self.assertTrue(before[0].active)
        self.assertEqual(before[0].supervision_event_id,'existing-event')
        self.journal.undo(self.feedback['feedback_id'])
        with self.store.conn() as c:
            after = read_supervision(c,['a'],as_of_ts=40.)
        self.assertFalse(after[0].active)
        self.assertFalse(build_dataset(after,{('a',20.):receipt()},replay()).samples)

    def test_bounded_reader_and_label_knowledge_cut(self):
        with self.store.conn() as c:
            self.assertFalse(read_supervision(c,['a'],as_of_ts=29.))
            self.journal.record(agent_id='a',selected_ts=21.,source='correct',correct_action=0.)
            with self.assertRaises(ValueError):
                read_supervision(c,['a'],as_of_ts=40.,max_rows=1)

    def test_candidate_lineage_read_includes_revoked_root_copy(self):
        child=self.journal.record(agent_id='b',root_agent_id='a',generation_id='g2',
                                  selected_ts=20.,source='correct',correct_action=1.)
        with self.store.conn() as c:
            cursor=c.execute('''INSERT INTO teaching_rl_labels
                (agent_id,created_ts,sample_ts,desired,fingerprint,supervision_event_id)
                VALUES('b',30,20,1,'fingerprint','existing-event')''')
            copy_id=cursor.lastrowid
        self.journal.link(child['feedback_id'],'learning','teach_rl_label',copy_id)
        self.journal.undo(self.feedback['feedback_id'])
        with self.store.conn() as c:
            labels=read_supervision(c,['b'],as_of_ts=40.)
        self.assertEqual(len(labels),2)
        data=build_dataset(labels,{('a',20.):receipt(),('b',20.):receipt(agent_id='b')},
                           replay(),coverage=coverage())
        self.assertFalse(data.samples)

    def test_dangling_feedback_link_cannot_hide_a_missing_revocation_identity(self):
        with self.store.conn() as c:
            c.execute('DELETE FROM teaching_rl_labels')
        with self.store.conn() as c:
            with self.assertRaisesRegex(ValueError,'dangling'):
                read_supervision(c,['a'],as_of_ts=40.)

    def test_archive_missing_or_late_receipts_do_not_become_early_observations(self):
        self.store.archive_batch([
            ('binary_sensor.motion',10.,'on',{},None,'live',11.),
            ('binary_sensor.motion',12.,'off',{},None,'recorder',30.),
            ('binary_sensor.legacy',10.,'on',{},None,'recorder'),
        ])
        with self.store.conn() as c:
            rows=read_archive_observations(c,['binary_sensor.motion','binary_sensor.legacy'],at_ts=20.)
            later=read_archive_observations(c,['binary_sensor.motion'],at_ts=40.,since_ts=12.)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].received_ts,11.)
        self.assertEqual([row.event_ts for row in later],[12.])

    def test_preparation_requires_explicit_consistent_read_snapshot(self):
        with self.store.conn() as c:
            with self.assertRaisesRegex(ValueError,'transaction'):
                prepare_dataset(c,['a'],{},timeline(),as_of_ts=40.,coverage=coverage())

    def test_real_featurejournal_preparation_is_readonly_and_reproducible(self):
        from observation_contract import FeatureJournal
        FeatureJournal(self.store)
        with self.store.conn() as c:
            c.execute('''INSERT INTO feature_observation_events
                (event_key,contract_version,entity_id,event_time,received_time,state,
                 attributes_json,source,quality,protected_until)
                VALUES('m',1,'binary_sensor.motion',10,11,'on','{}','live',1,100)''')
        with self.store.conn() as c:
            # An unrelated old prefix must not exhaust a bounded covered window.
            c.execute('''INSERT INTO feature_observation_events
                (event_key,contract_version,entity_id,event_time,received_time,state,
                 attributes_json,source,quality,protected_until)
                VALUES('old',1,'binary_sensor.motion',-100,-99,'off','{}','live',1,100)''')
        with self.store.conn() as c:
            c.execute('BEGIN')
            before=c.total_changes
            data=prepare_dataset(c,['a'],{('a',20.):receipt()},timeline(),as_of_ts=40.,coverage=coverage(),max_observations=1)
            again=prepare_dataset(c,['a'],{('a',20.):receipt()},timeline(),as_of_ts=40.,coverage=coverage())
            self.assertEqual(c.total_changes,before)
        self.assertEqual(data,again)
        self.assertEqual(len(data.samples),1)
        with self.store.conn() as c:
            c.execute('BEGIN')
            with patch('universal_training_data.MAX_SOURCE_BYTES',1):
                with self.assertRaisesRegex(ValueError,'byte cap'):
                    prepare_dataset(c,['a'],{('a',20.):receipt()},timeline(),as_of_ts=40.,coverage=coverage())
