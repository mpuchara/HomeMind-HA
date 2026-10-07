import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import state, agent
from causal_home_statistics import CausalHomeStatistics
from context_engine import ContextEngine
from engine import Engine
import engine as engine_module
from home_mapping import automation_radar_mapping
from observation_contract import HOME_FEATURE_NAMES, policy_features
from policy import DiagonalLinUCB, MultiHorizonPolicy
from replay import SQLiteTemporalTracker
from settings import DEFAULT_OPTIONS
from storage import Store


ENERGY = 'sensor.espen4_stationary_energy'
PRESENCE = 'binary_sensor.espen4_has_target'
TARGET = 'switch.bathroom'
HALL = 'binary_sensor.hall_motion'
KITCHEN = 'binary_sensor.kitchen_presence'


def controllers():
    return [dict(entity_id='automation.' + arm, enabled=True,
                 context_entities=[ENERGY], action_services=['switch.turn_' + arm],
                 baseline_rules=[dict(source='trigger', kind='numeric_state', entity_id=ENERGY,
                                      **{key: threshold})])
            for arm, key, threshold in [('on', 'above', 22), ('off', 'below', 12)]]


def fixture():
    states = {ENERGY: state(ENERGY, 8, unit_of_measurement='%'),
              PRESENCE: state(PRESENCE, 'off', device_class='occupancy'),
              TARGET: state(TARGET, 'off'), HALL: state(HALL, 'off', device_class='motion'),
              KITCHEN: state(KITCHEN, 'off', device_class='occupancy'),
              'binary_sensor.office_presence': state('binary_sensor.office_presence', 'off', device_class='occupancy')}
    registry = {TARGET: {'area_id': 'bathroom'}, HALL: {'area_id': 'hall'},
                KITCHEN: {'area_id': 'kitchen'}, 'binary_sensor.office_presence': {'area_id': 'office'}}
    context = ContextEngine(dict(DEFAULT_OPTIONS))
    context.configure(states, entities=registry, automation_hints={TARGET: controllers()})
    return states, registry, context


def learn_routes(context, states, start=1701000000., count=40):
    for index in range(count):
        ts = start + index * 100
        for eid in (HALL, PRESENCE, KITCHEN):
            context.observe(eid, dict(states[eid], state='off'), ts)
        context.observe(HALL, dict(states[HALL], state='on'), ts + 1)
        context.observe(PRESENCE, dict(states[PRESENCE], state='on'), ts + 3)
        context.observe(KITCHEN, dict(states[KITCHEN], state='on'), ts + 5)
        for eid in (HALL, PRESENCE, KITCHEN):
            context.observe(eid, dict(states[eid], state='off'), ts + 15)
        context.home.expire(ts + 50)


class Trajectory146Tests(unittest.TestCase):
    def test_automation_mapping_recovers_exact_radar_and_binary_sibling(self):
        states, registry, context = fixture()
        self.assertEqual(context.area_for(ENERGY), 'bathroom')
        self.assertEqual(context.area_for(PRESENCE), 'bathroom')
        self.assertEqual(context.evidence_metadata(PRESENCE)['role'], 'radar_occupancy')
        self.assertFalse(context.evidence_metadata(ENERGY)['occupancy_authority'])
        self.assertEqual(context.resolved_registry()[ENERGY]['area_mapping_origin'], 'automation_radar_anchor')
        self.assertNotIn(ENERGY, registry)  # HA registry is never changed.

    def test_explicit_area_conflicts_and_ambiguous_controllers_are_not_overridden(self):
        states, registry, context = fixture()
        mapped = {TARGET: 'bathroom', ENERGY: 'hall'}
        inferred, audit = automation_radar_mapping(states, registry, mapped, {TARGET: controllers()})
        self.assertEqual(inferred, {})
        self.assertEqual(audit[0]['reason'], 'area_conflict')
        inferred, audit = automation_radar_mapping(states, registry,
            {TARGET: 'bathroom', 'switch.other': 'hall'},
            {TARGET: controllers(), 'switch.other': controllers()})
        self.assertEqual(inferred, {})
        self.assertTrue(all(row['reason'] == 'ambiguous_target_areas' for row in audit))

    def test_unpaired_numeric_trigger_and_remote_boundary_do_not_assign_area(self):
        states, registry, _context = fixture()
        inferred, _ = automation_radar_mapping(states, registry, {TARGET: 'bathroom'},
                                               {TARGET: controllers()[:1]})
        self.assertEqual(inferred, {})
        registry[ENERGY] = {'boundary_for': 'bathroom'}
        inferred, _ = automation_radar_mapping(states, registry, {TARGET: 'bathroom'}, {TARGET: controllers()})
        self.assertEqual(inferred, {})

    def test_disabled_controllers_after_takeover_keep_structural_mapping(self):
        states, registry, _context = fixture()
        infos = [dict(info, enabled=False) for info in controllers()]
        inferred, _ = automation_radar_mapping(states, registry, {TARGET: 'bathroom'}, {TARGET: infos})
        self.assertEqual(inferred[ENERGY]['area_id'], 'bathroom')

    def test_known_device_siblings_do_not_match_other_devices_with_similar_names(self):
        states, registry, _context = fixture()
        registry[ENERGY] = {'device_id': 'radar'}
        registry[PRESENCE] = {'device_id': 'other'}
        inferred, _ = automation_radar_mapping(states, registry, {TARGET: 'bathroom'}, {TARGET: controllers()})
        self.assertNotIn(PRESENCE, inferred)

    def test_raw_energy_alone_never_creates_a_trajectory_or_confirmed_presence(self):
        states, _registry, context = fixture()
        for i, value in enumerate((8, 80, 0, 80)):
            context.observe(ENERGY, dict(states[ENERGY], state=str(value)), 1701000000. + i)
        self.assertEqual(context.home.graph, {})
        self.assertEqual(context.home.hypotheses, [])
        self.assertLess(context.forecast(TARGET, 1701000003.)['occupancy_now'], .5)

    def test_learned_paths_wake_agent_without_raw_upstream_schema_or_unrelated_rooms(self):
        states, _registry, context = fixture()
        learn_routes(context, states)
        dummy = SimpleNamespace(context=context, experiments=SimpleNamespace(watches=lambda _aid: ()))
        policy = SimpleNamespace(schema=SimpleNamespace(entities=[ENERGY]))
        deps = Engine.event_dependencies(dummy, agent(target_entity=TARGET), policy)
        self.assertIn(HALL, deps)
        self.assertIn(KITCHEN, deps)
        self.assertIn(PRESENCE, deps)
        self.assertNotIn('binary_sensor.office_presence', deps)

    def test_alternative_branch_can_cancel_arrival_and_is_an_event_dependency(self):
        states, _registry, context = fixture()
        context.home._record(('hall',), 'bathroom', 2, 1701000000., 50)
        context.home._record(('hall',), 'kitchen', 2, 1701000000., 50)
        for eid in (HALL, PRESENCE, KITCHEN):
            context.observe(eid, states[eid], 1701000001.)
        context.observe(HALL, dict(states[HALL], state='on'), 1701000002.)
        before = context.forecast(TARGET, 1701000002.)['arrival_probability']
        context.observe(KITCHEN, dict(states[KITCHEN], state='on'), 1701000004.)
        after = context.forecast(TARGET, 1701000004.)['arrival_probability']
        self.assertGreater(before, .1)
        self.assertLess(after, before)
        self.assertIn(KITCHEN, context.trajectory_sources_for(TARGET))

    def test_trajectory_changes_the_trained_classifier_with_identical_local_energy(self):
        states, _registry, context = fixture()
        learn_routes(context, states)
        configured = agent(target_entity=TARGET, input_entities=[ENERGY])
        policy = MultiHorizonPolicy(configured, states, context.resolved_registry(), [], context_engine=context)
        timestamp = 1701005000.
        context.home.reset_live_state()
        for eid in (HALL, PRESENCE, KITCHEN):
            context.observe(eid, states[eid], timestamp)
        empty, _, _ = policy_features(policy, states, None, timestamp)
        context.observe(HALL, dict(states[HALL], state='on'), timestamp + 1)
        arriving, _, meta = policy_features(policy, states, None, timestamp + 1)
        self.assertGreater(meta['home_forecast']['arrival_probability'], .1)
        head = DiagonalLinUCB(policy.dims, [0, 1], desired_state_learning=True)
        for _ in range(80):
            head.update(0, empty, .9)
            head.update(1, arriving, .9)
        head.state_classifier.fit()
        restored = DiagonalLinUCB(policy.dims, [0, 1], model=head.export())
        self.assertEqual(restored.choose(empty)[0]['index'], 0)
        self.assertEqual(restored.choose(arriving)[0]['index'], 1)

    def test_active_trajectory_gets_a_one_second_timer_and_manual_hold_wins(self):
        dummy = SimpleNamespace()
        configured = agent(target_entity=TARGET)
        runtime = {'context_meta': {'home_forecast': {'arrival_probability': .5}}}
        self.assertEqual(Engine._schedule_next_inference(dummy, configured, runtime, 100), 101)
        runtime['manual_override_until'] = 400
        self.assertEqual(Engine._schedule_next_inference(dummy, configured, runtime, 100), 400)

    def test_route_topology_revision_changes_only_when_a_route_changes(self):
        _states, _registry, context = fixture()
        model = context.home
        model._record(('hall',), 'bathroom', 2, 100, 1)
        revision = model.routing_revision
        model._record(('hall',), 'bathroom', 3, 102, 1)
        self.assertEqual(model.routing_revision, revision)
        model._record(('hall',), 'kitchen', 2, 103, 1)
        self.assertGreater(model.routing_revision, revision)

    def test_new_route_invalidates_cached_event_routing_immediately(self):
        states, _registry, context = fixture()
        runtime = Engine()
        self.addCleanup(lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True))
        self.addCleanup(lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True))
        runtime.context, runtime.state_map = context, states
        configured = agent(target_entity=TARGET)
        fake = SimpleNamespace(_agent_index_revision=1, list_agent_configs=lambda: [configured])
        knowledge = SimpleNamespace(lock=threading.RLock(), last_scan=1, by_target={TARGET: controllers()})
        with patch.object(engine_module, 'STORE', fake), patch.object(engine_module, 'AUTOMATION_KNOWLEDGE', knowledge):
            self.assertEqual(runtime._active_agents_for_changes({HALL}), [])
            context.home._record(('hall',), 'bathroom', 2, 1701000000., 20)
            self.assertEqual(runtime._active_agents_for_changes({HALL}), [configured])
            self.assertEqual(runtime._active_agents_for_changes({'binary_sensor.office_presence'}), [])

    def test_runtime_resolves_radar_before_inference_without_fabricating_a_movement(self):
        states, registry, _context = fixture()
        runtime = Engine()
        self.addCleanup(lambda: runtime.control_workers.shutdown(wait=False, cancel_futures=True))
        self.addCleanup(lambda: runtime.poll_worker.shutdown(wait=False, cancel_futures=True))
        context = ContextEngine(dict(DEFAULT_OPTIONS))
        states[PRESENCE]['state'] = 'on'
        context.configure(states, entities=registry)
        runtime.context, runtime.state_map = context, states
        knowledge = SimpleNamespace(lock=threading.RLock(), last_scan=1, by_target={TARGET: controllers()})
        with patch.object(engine_module, 'AUTOMATION_KNOWLEDGE', knowledge):
            self.assertTrue(runtime.refresh_automation_context())
            revision = context.registry_revision
            self.assertFalse(runtime.refresh_automation_context())
            self.assertEqual(context.registry_revision, revision)
        self.assertEqual(context.home.hypotheses, [])
        self.assertEqual(context.home.graph, {})
        self.assertTrue(context.forecast(TARGET, engine_module.now_ts())['known'])

    def test_training_learns_causal_paths_without_a_home_backfill_and_rewinds_safely(self):
        states, _registry, context = fixture()
        base = 1701000000.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            store = Store(Path(tmp) / 'paths.db')
            rows = [(eid, base, 'off', states[eid]['attributes'], None, 'test') for eid in (HALL, PRESENCE)]
            for i in range(15):
                t = base + 100 * i + 10
                rows.extend([(HALL, t, 'on', states[HALL]['attributes'], None, 'test'),
                             (PRESENCE, t + 2, 'on', states[PRESENCE]['attributes'], None, 'test'),
                             (HALL, t + 15, 'off', states[HALL]['attributes'], None, 'test'),
                             (PRESENCE, t + 15, 'off', states[PRESENCE]['attributes'], None, 'test')])
            rows.append((HALL, base + 1510, 'on', states[HALL]['attributes'], None, 'test'))
            store.archive_batch(rows)
            tracker = SQLiteTemporalTracker(store, [ENERGY], context, base, base + 1600)
            cleanup.callback(tracker.close)
            index = CausalHomeStatistics(tracker.conn, context, base, base + 1600)
            tracker.home_statistics = index
            self.assertEqual(index.statistics_at(base)[0], {})
            tracker.advance(base + 1510)
            late = tracker.history.home_context.forecast(TARGET, base + 1510)
            self.assertGreater(late['arrival_probability'], .5)
            tracker.advance(base + 10)
            early = tracker.history.home_context.forecast(TARGET, base + 10)
            self.assertEqual(early['arrival_probability'], 0)
            tracker.advance(base + 1510)
            self.assertEqual(tracker.history.home_context.forecast(TARGET, base + 1510)['arrival_probability'], late['arrival_probability'])
            self.assertLess(index.bytes, 8 * 1024 * 1024)

    def test_late_arrival_is_not_learned_before_receive_time(self):
        states, _registry, context = fixture()
        base = 1701000000.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            store = Store(Path(tmp) / 'late.db')
            store.archive_batch([(HALL, base, 'off', states[HALL]['attributes'], None, 'live', base),
                (PRESENCE, base, 'off', states[PRESENCE]['attributes'], None, 'live', base),
                (HALL, base + 10, 'on', states[HALL]['attributes'], None, 'live', base + 10),
                (PRESENCE, base + 12, 'on', states[PRESENCE]['attributes'], None, 'live', base + 14)])
            tracker = SQLiteTemporalTracker(store, [ENERGY], context, base, base + 20)
            cleanup.callback(tracker.close)
            index = CausalHomeStatistics(tracker.conn, context, base, base + 20)
            self.assertEqual(index.statistics_at(base + 13)[0], {})
            self.assertIn(('hall',), index.statistics_at(base + 14)[0])


if __name__ == '__main__':
    unittest.main()
