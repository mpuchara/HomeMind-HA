import unittest

from adaptive_presence import AdaptivePresenceModel, HardwareThresholdAdapterContract
from context_engine import ContextEngine
from settings import DEFAULT_OPTIONS
from support import state


def raw(entity='sensor.raw', value=.4, quality=1.0):
    return {
        'entity_id': entity,
        'role': 'radar_activity',
        'value': value,
        'quality': quality,
        'available': True,
    }


class AdaptivePresenceModelTests(unittest.TestCase):
    def test_prior_without_local_evidence_is_anticipation_not_virtual_presence(self):
        model = AdaptivePresenceModel()
        result = model.evaluate('kitchen', 1, .8, 1.0, [])
        self.assertEqual(result['mode'], 'anticipation_only')
        self.assertFalse(result['virtual_presence_active'])
        self.assertIsNone(result['posterior'])
        self.assertGreater(result['effective_arrival_prior'], 0)
        self.assertFalse(result['capability']['local_threshold_distance_available'])

    def test_weak_local_signal_with_correct_trajectory_beats_fixed_boundary_early(self):
        model = AdaptivePresenceModel()
        early = model.evaluate('kitchen', 10, .55, 1.0, [raw(value=.40)])
        self.assertTrue(early['virtual_presence_active'])
        self.assertGreaterEqual(early['posterior'], model.ENTER_THRESHOLD)
        self.assertFalse(early['fixed_boundary_active'])
        confirmed = model.evaluate('kitchen', 11.5, .55, 1.0, [raw(value=.75)])
        self.assertTrue(confirmed['fixed_boundary_active'])
        self.assertAlmostEqual(confirmed['timing']['mean_lead_seconds'], 1.5, places=6)
        self.assertEqual(confirmed['timing']['false_on_count'], 0)
        self.assertEqual(confirmed['timing']['false_on_cost'], 0)

    def test_false_trajectory_does_not_override_conflicting_local_signal(self):
        model = AdaptivePresenceModel()
        result = model.evaluate('kitchen', 1, .80, 1.0, [raw(value=.02)])
        self.assertFalse(result['virtual_presence_active'])
        self.assertLess(result['posterior'], model.ENTER_THRESHOLD)
        self.assertFalse(result['fixed_boundary_active'])

    def test_noisy_low_quality_signal_is_downweighted(self):
        high_quality = AdaptivePresenceModel().evaluate('kitchen', 1, .50, 1.0, [raw(value=.59, quality=1.0)])
        low_quality = AdaptivePresenceModel().evaluate('kitchen', 1, .50, 1.0, [raw(value=.59, quality=.10)])
        self.assertGreater(high_quality['posterior'], low_quality['posterior'])
        self.assertFalse(low_quality['virtual_presence_active'])

    def test_conflicting_signal_reduces_high_arrival_prior(self):
        result = AdaptivePresenceModel().evaluate('kitchen', 1, .90, 1.0, [raw(value=0.0)])
        self.assertLess(result['posterior'], result['effective_arrival_prior'])
        self.assertFalse(result['virtual_presence_active'])

    def test_only_one_correlated_raw_channel_is_fused(self):
        single = AdaptivePresenceModel().evaluate('kitchen', 1, .55, 1.0, [raw('sensor.a', .4, 1.0)])
        multiple = AdaptivePresenceModel().evaluate(
            'kitchen', 1, .55, 1.0,
            [raw('sensor.a', .4, 1.0), raw('sensor.b', .95, .8)],
        )
        self.assertAlmostEqual(single['posterior'], multiple['posterior'], places=9)
        self.assertEqual(multiple['selected_raw_source'], 'sensor.a')
        self.assertEqual(multiple['alternative_raw_sources_not_multiplied'], ['sensor.b'])

    def test_false_on_budget_limits_repeated_unconfirmed_virtual_triggers(self):
        model = AdaptivePresenceModel()
        now = 0.0
        for _ in range(model.FALSE_BUDGET_LIMIT):
            on = model.evaluate('kitchen', now, .55, 1.0, [raw(value=.40)])
            self.assertTrue(on['virtual_presence_active'])
            model.evaluate('kitchen', now + model.FALSE_CONFIRM_SECONDS + .1, .55, 1.0, [raw(value=.40)])
            model.evaluate('kitchen', now + model.FALSE_CONFIRM_SECONDS + .2, .05, 1.0, [raw(value=0.0)])
            now += 20.0
        blocked = model.evaluate('kitchen', now, .55, 1.0, [raw(value=.40)])
        self.assertFalse(blocked['virtual_presence_active'])
        self.assertTrue(blocked['suppressed_by_false_on_budget'])
        self.assertEqual(blocked['timing']['false_on_count'], model.FALSE_BUDGET_LIMIT)
        self.assertGreater(blocked['timing']['false_on_cost'], 0)

    def test_threshold_modified_output_cannot_calibrate_model(self):
        model = AdaptivePresenceModel()
        model.mark_threshold_changed('binary_sensor.output', 100)
        with self.assertRaises(ValueError):
            model.record_independent_label('sensor.raw', .4, True, 'binary_sensor.output', ts=50)
        summary = model.record_independent_label('sensor.raw', .4, True, 'binary_sensor.output', ts=101)
        self.assertEqual(summary['independent_labels'], 1)


class AdaptivePresenceIntegrationTests(unittest.TestCase):
    def _context(self, states, registry):
        options = dict(DEFAULT_OPTIONS)
        context = ContextEngine(options)
        context.configure(
            states,
            entities=registry,
            devices=[],
            areas=[{'area_id': 'kitchen', 'name': 'Kitchen'}],
        )
        return context

    def test_virtual_presence_raises_future_forecast_but_not_physical_occupancy_now(self):
        context = ContextEngine(dict(DEFAULT_OPTIONS))

        class FakeHome:
            revision = 1
            area_sources = {'kitchen': {'sensor.raw'}}
            sources = {
                'sensor.raw': {
                    'role': 'radar_activity', 'value': .40, 'available': True,
                    'communication_reliability': 1.0, 'ts': 10.0, 'state_since_ts': 10.0,
                }
            }
            def _freshness(self, source, ts):
                return 1.0
            def calibration_metrics(self):
                return {'independent_labels': 0, 'brier_score': None}

        base = {
            'occupancy_now': .10,
            'occupancy_in_1s': .20,
            'occupancy_in_3s': .40,
            'occupancy_in_5s': .45,
            'arrival_probability': .55,
            'arrival_probability_by_horizon': {'1s': .2, '3s': .55, '5s': .6},
            'trajectory_confidence': 1.0,
        }
        result = context.augment_home_forecast(FakeHome(), 'kitchen', base, 10)
        self.assertEqual(result['occupancy_now'], .10)
        self.assertTrue(result['virtual_presence_active'])
        self.assertGreater(result['occupancy_in_1s'], base['occupancy_in_1s'])
        self.assertEqual(result['presence_capability']['mode'], 'virtual_threshold')
        self.assertFalse(result['presence_capability']['hardware_threshold_adapter']['enabled'])

    def test_binary_only_capability_explicitly_offers_anticipation_only(self):
        states = {
            'binary_sensor.kitchen_presence': state(
                'binary_sensor.kitchen_presence', 'off', device_class='occupancy', friendly_name='Kitchen Presence'
            ),
            'light.kitchen': state('light.kitchen', 'off'),
        }
        registry = {
            'binary_sensor.kitchen_presence': {'area_id': 'kitchen', 'device_id': 'radar1'},
            'light.kitchen': {'area_id': 'kitchen', 'device_id': 'light1'},
        }
        context = self._context(states, registry)
        context.observe('binary_sensor.kitchen_presence', states['binary_sensor.kitchen_presence'], 1)
        forecast = context.forecast('light.kitchen', 1)
        cap = forecast['presence_capability']
        self.assertEqual(cap['mode'], 'anticipation_only')
        self.assertFalse(cap['local_threshold_distance_available'])
        self.assertIn('binary_sensor.kitchen_presence', cap['binary_sources'])
        self.assertIsNotNone(cap['binary_only_limitation'])

    def test_stationary_radar_occupancy_is_not_lost_when_raw_activity_falls(self):
        states = {
            'binary_sensor.kitchen_presence': state(
                'binary_sensor.kitchen_presence', 'on', device_class='occupancy', friendly_name='Kitchen Presence'
            ),
            'sensor.kitchen_still_energy': state(
                'sensor.kitchen_still_energy', '5', unit_of_measurement='%', friendly_name='Kitchen Still Energy'
            ),
            'light.kitchen': state('light.kitchen', 'off'),
        }
        registry = {
            'binary_sensor.kitchen_presence': {'area_id': 'kitchen', 'device_id': 'radar1'},
            'sensor.kitchen_still_energy': {'area_id': 'kitchen', 'device_id': 'radar1'},
            'light.kitchen': {'area_id': 'kitchen', 'device_id': 'light1'},
        }
        context = self._context(states, registry)
        context.observe('binary_sensor.kitchen_presence', states['binary_sensor.kitchen_presence'], 1)
        context.observe('sensor.kitchen_still_energy', states['sensor.kitchen_still_energy'], 1)
        forecast = context.forecast('light.kitchen', 100)
        self.assertGreater(forecast['occupancy_now'], .5)
        self.assertGreater(forecast['occupancy_in_1s'], .5)
        self.assertFalse(forecast['virtual_presence_active'])


class HardwareThresholdAdapterContractTests(unittest.TestCase):
    def test_adapter_is_disabled_by_default_and_has_no_physical_io(self):
        adapter = HardwareThresholdAdapterContract(
            whitelist=['number.radar_threshold'],
            constraints={'number.radar_threshold': {'min': 0, 'max': 1, 'step': .1}},
        )
        cap = adapter.capability('number.radar_threshold')
        self.assertFalse(cap['enabled'])
        self.assertFalse(cap['physical_io'])
        self.assertTrue(cap['requires_snapshot'])
        with self.assertRaises(RuntimeError):
            adapter.acquire_lease('number.radar_threshold', {'threshold': .5}, .5, 0)

    def test_enabled_contract_enforces_whitelist_range_step_rate_lease_ttl_and_restore_snapshot(self):
        adapter = HardwareThresholdAdapterContract(
            enabled=True,
            whitelist=['number.radar_threshold'],
            constraints={'number.radar_threshold': {'min': 0, 'max': 1, 'step': .1}},
            max_changes=2,
            change_window_seconds=60,
            lease_ttl_seconds=30,
        )
        lease = adapter.acquire_lease(
            'number.radar_threshold', {'threshold': .5, 'sensitivity': 7}, .5, 10
        )
        plan = adapter.plan_change('number.radar_threshold', lease['lease_id'], .4, 11)
        self.assertFalse(plan['physical_io'])
        self.assertTrue(plan['requires_external_adapter_commit'])
        with self.assertRaises(ValueError):
            adapter.plan_change('number.radar_threshold', lease['lease_id'], .45, 12)
        adapter.plan_change('number.radar_threshold', lease['lease_id'], .3, 13)
        with self.assertRaises(RuntimeError):
            adapter.plan_change('number.radar_threshold', lease['lease_id'], .2, 14)
        restore = adapter.restore_plan('number.radar_threshold', lease['lease_id'], 41)
        self.assertTrue(restore['expired'])
        self.assertEqual(restore['restore_value'], .5)
        self.assertEqual(restore['snapshot']['sensitivity'], 7)
        self.assertFalse(restore['physical_io'])


if __name__ == '__main__':
    unittest.main()
