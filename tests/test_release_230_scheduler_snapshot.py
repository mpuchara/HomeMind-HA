"""Sparse scheduler routing retains snapshot isolation, retry and timer semantics."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import engine as engine_module
from tools.benchmark_target_retries import fixture
from telemetry import Telemetry, RuntimeDebugTrace


class SchedulerSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.runtime = fixture(SimpleNamespace())

    def test_unrelated_index_is_not_traversed(self):
        class Routing(dict):
            def items(self):
                raise AssertionError('Full routing index traversal')
        r = self.runtime
        r.dependency_agents = Routing(r.dependency_agents)
        r.dependency_agents.update({f'sensor.unrelated_{i}': {'0','1'} for i in range(2000)})
        r.process(r.state_map, {'binary_sensor.shared'})
        self.assertEqual(len(r.control_workers.tasks), 2)
        snapshots = [task[2][2] for task in r.control_workers.tasks]
        self.assertIs(snapshots[0], snapshots[1])
        self.assertIsNot(snapshots[0][0], r.state_map)
        self.assertIsNot(snapshots[0][2], r.entity_revisions)

    def test_concurrent_routing_and_revision_changes_cannot_rewrite_pass(self):
        r = self.runtime
        original = r.control_workers.submit
        def submit(*args):
            r.dependency_agents['binary_sensor.shared'].clear()
            r.state_map['binary_sensor.shared'] = {'state':'new'}
            r.entity_revisions['binary_sensor.shared'] = 999
            return original(*args)
        r.control_workers.submit = submit
        r.process(r.state_map, {'binary_sensor.shared'})
        self.assertEqual(len(r.control_workers.tasks), 2)
        for task in r.control_workers.tasks:
            args = task[2]
            self.assertEqual(args[1], {'binary_sensor.shared'})
            self.assertEqual(args[2][0]['binary_sensor.shared']['state'], 'off')
            self.assertEqual(args[2][2]['binary_sensor.shared'], 0)

    def test_retry_entity_survives_independent_fresh_event(self):
        r = self.runtime
        r.dependency_agents['sensor.other'] = {'1'}
        r.ready_target_changes['light.target_0'] = {'binary_sensor.shared'}
        r.process(r.state_map, {'sensor.other'})
        changes = {task[2][0][0]['id']:task[2][1] for task in r.control_workers.tasks}
        self.assertEqual(changes, {'0':{'binary_sensor.shared'}, '1':{'sensor.other'}})

    def test_timer_retains_routing_hint_and_no_event_clock(self):
        r = self.runtime
        r.process(r.state_map, {'light.target_1'}, event_driven=False)
        task = r.control_workers.tasks[0][2]
        self.assertEqual(task[1], {'light.target_1'})
        self.assertEqual(task[3], {})


class PerformanceTelemetryTests(unittest.TestCase):
    def test_percentiles_are_bounded_and_small_p99_is_marked(self):
        telemetry = Telemetry()
        for i in range(800):
            telemetry.observe('test', i)
        metric = telemetry.snapshot()['metrics']['test']
        self.assertEqual(metric['sample_count'], 512)
        self.assertEqual(metric['count'], 800)
        self.assertLessEqual(metric['p50_ms'], metric['p95_ms'])
        self.assertLessEqual(metric['p95_ms'], metric['p99_ms'])
        self.assertFalse(metric['p99_sufficient'])

    def test_anonymous_export_omits_household_fields_and_unknown_operations(self):
        trace = RuntimeDebugTrace()
        self.assertIsNone(trace.begin('inference_agent'))
        trace.set_enabled(True)
        token = trace.begin('inference_agent', agent_id='secret', entity_id='light.private')
        trace.end(token)
        token = trace.begin('secret_operation', stage='secret_stage')
        trace.end(token)
        result = trace.anonymous_spans()
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('light.private', repr(result))
        self.assertEqual([s['operation'] for s in result['spans']], ['inference_agent','other'])
        self.assertIn('thread_cpu_ms', result['spans'][0])
        self.assertIn('process_cpu_ms', result['spans'][0])
        self.assertEqual(len(result['spans']), 2)

    def test_anonymous_http_route_remains_trusted_and_uses_only_trace_ram(self):
        from runtime_debug_log import register_runtime_debug_routes
        from runtime_http import ExplicitRouteRegistry
        from unittest.mock import Mock
        registry = ExplicitRouteRegistry()
        manager = SimpleNamespace()
        register_runtime_debug_routes(registry, SimpleNamespace(), manager)
        route = registry._routes[('GET', 'debug.runtime_log.timings')]
        self.assertTrue(route['require_trusted'])
        http = Mock()
        with patch.object(manager.runtime_debug_log, 'export_payload', side_effect=AssertionError('Full export')):
            route['callback'](http, {})
        self.assertEqual(http.send_json.call_args.args[0], 200)
        self.assertEqual(set(http.send_json.call_args.args[1]), {'capacity','dropped','spans'})
