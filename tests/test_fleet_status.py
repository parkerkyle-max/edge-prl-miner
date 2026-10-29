"""Synthetic general-miner accounting; no hardware, network or miner control."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from edge_prl.fleet_status import InputError, load_input, overview


def rate(identifier='gpu-a-rate', device='gpu-a', **changes):
    row = {'measurement_id': identifier, 'device_ids': [device], 'source': 'local',
           'source_id': 'synthetic-local-miner', 'coin': 'PRL', 'algorithm': 'pearl-matrix-v1',
           'rate_definition': 'synthetic-forge-reported-v1', 'basis': 'local_reported_hashes_per_wall_second',
           'unit': 'TH/s', 'value': 42, 'window_start_utc': '2026-01-01T00:00:00Z',
           'window_end_utc': '2026-01-01T00:00:30Z', 'observed_at_utc': '2026-01-01T00:00:30Z',
           'accounting': 'user', 'selected': True}
    row.update(changes)
    return row


def fixture():
    return {'schema_version': 1, 'as_of_utc': '2026-01-01T00:00:35Z', 'freshness_seconds': 60,
            'devices': [{'device_id': 'gpu-a', 'kind': 'GPU'}, {'device_id': 'gpu-b', 'kind': 'GPU'},
                        {'device_id': 'npu-a', 'kind': 'NPU'}, {'device_id': 'cpu-a', 'kind': 'CPU'}],
            'measurements': [rate(), rate('gpu-b-rate', 'gpu-b', value=28),
                rate('npu-rate', 'npu-a', unit='GMAC/s', value=.9,
                     rate_definition='synthetic-prl-complete-mac-v1', basis='completed_nonstale_macs_per_wall_second'),
                rate('cpu-rate', 'cpu-a', coin='XMR', algorithm='randomx', rate_definition='randomx-hash-v1',
                     unit='H/s', value=280)]}


def income(identifier='estimate-a', device='gpu-a', **changes):
    row = {'estimate_id': identifier, 'device_ids': [device], 'coin': 'PRL', 'currency': 'CAD',
           'period': 'day', 'amount': .1, 'source_id': 'synthetic-estimator',
           'market_snapshot_id': 'synthetic-prices-1', 'market_observed_at_utc': '2026-01-01T00:00:30Z',
           'observed_at_utc': '2026-01-01T00:00:30Z', 'net_of_all_miner_and_pool_fees': True, 'selected': True}
    row.update(changes)
    return row


class FleetStatusTests(unittest.TestCase):
    def test_homogeneous_gpu_sum_keeps_npu_and_xmr_separate(self):
        data = fixture()
        before = copy.deepcopy(data)
        result = overview(data)
        self.assertEqual(data, before)
        self.assertEqual(len(result['rate_groups']), 3)
        groups = {(g['coin'], g['unit']): g for g in result['rate_groups']}
        self.assertEqual(groups['PRL', 'H/s']['known_subtotal'], 70_000_000_000_000)
        self.assertEqual(groups['PRL', 'MAC/s']['known_subtotal'], 900_000_000)
        self.assertEqual(groups['XMR', 'H/s']['known_subtotal'], 280)
        self.assertIsNone(result['combined_cpu_gpu_npu_hashrate'])
        self.assertFalse(result['fees_collected'])
        self.assertFalse(result['miner_started_or_modified'])

    def test_decimal_prefix_scaling_and_large_integer_precision(self):
        data = fixture()
        data['measurements'] = [rate(unit='kH/s', value=.1), rate('second', 'gpu-b', unit='H/s', value=.2)]
        self.assertEqual(overview(data)['rate_groups'][0]['known_subtotal'], 100.2)
        data['measurements'][0].update(unit='H/s', value=9_007_199_254_740_993)
        data['measurements'][1]['value'] = 1
        self.assertEqual(overview(data)['rate_groups'][0]['known_subtotal'], 9_007_199_254_740_994)
        data['measurements'][0].update(unit='TH/s', value=12345678901234567890)
        self.assertEqual(overview(data)['rate_groups'][0]['known_subtotal'], 12345678901234567890000000000001)

    def test_rate_definition_window_source_and_accounting_must_match(self):
        variants = [dict(rate_definition='different-definition'),
                    dict(window_start_utc='2026-01-01T00:00:01Z'),
                    dict(source='pool', basis='pool_effective_hashes_per_wall_second'),
                    dict(accounting='gross')]
        for changes in variants:
            data = fixture()
            data['measurements'] = [rate(), rate('other', 'gpu-b', **changes)]
            with self.subTest(changes=changes):
                self.assertEqual(len(overview(data)['rate_groups']), 2)

    def test_duplicate_local_and_pool_coverage_requires_explicit_selection(self):
        data = fixture()
        duplicate = rate('pool-rate', source='pool', basis='pool_effective_hashes_per_wall_second')
        data['measurements'].append(duplicate)
        with self.assertRaisesRegex(InputError, 'Overlapping selected'):
            overview(data)
        duplicate['selected'] = False
        result = overview(data)
        self.assertEqual(len(result['rate_groups']), 3)
        self.assertEqual(result['measurements'][-1]['status'], 'not_selected')

    def test_multi_device_pool_aggregate_cannot_double_count_a_component(self):
        data = fixture()
        data['measurements'] = [rate(), rate('pool-all', 'gpu-b', device_ids=['gpu-a', 'gpu-b'],
                                            source='pool', basis='pool_effective_hashes_per_wall_second')]
        with self.assertRaisesRegex(InputError, 'Overlapping selected'):
            overview(data)

    def test_unknown_stale_and_fresh_zero_are_distinct(self):
        for value, end, expected in [(None, '2026-01-01T00:00:30Z', 'unknown'),
                                     (5, '2026-01-01T00:00:30Z', 'stale'),
                                     (0, '2026-01-01T00:01:59Z', 'included')]:
            data = fixture()
            data['as_of_utc'] = '2026-01-01T00:02:00Z'
            data['measurements'] = [rate(value=value, window_end_utc=end,
                                         observed_at_utc='2026-01-01T00:02:00Z')]
            result = overview(data)
            self.assertEqual(result['measurements'][0]['status'], expected)
            group = result['rate_groups'][0]
            self.assertEqual(group['known_subtotal'], 0 if expected == 'included' else None)
            self.assertEqual(group['selected_coverage_complete'], expected == 'included')

    def test_partial_group_never_calls_unknown_zero(self):
        data = fixture()
        data['measurements'] = [rate(), rate('unknown', 'gpu-b', value=None)]
        group = overview(data)['rate_groups'][0]
        self.assertEqual(group['known_subtotal'], 42_000_000_000_000)
        self.assertFalse(group['selected_coverage_complete'])
        self.assertEqual(group['excluded_measurement_ids'], ['unknown'])

    def test_utc_normalization_groups_equivalent_windows(self):
        data = fixture()
        data['measurements'] = [rate(), rate('other', 'gpu-b',
            window_start_utc='2026-01-01T00:00:00+00:00', window_end_utc='2026-01-01T00:00:30.000000Z')]
        self.assertEqual(len(overview(data)['rate_groups']), 1)

    def test_reject_bad_types_units_unknown_fields_and_duplicate_ids(self):
        for value in (-1, True, '42', float('nan'), float('inf'), -float('inf')):
            data = fixture(); data['measurements'][0]['value'] = value
            with self.subTest(value=value), self.assertRaises(InputError):
                overview(data)
        for changes in (dict(unit='TOPS'), dict(unit='MAC/s'), dict(selected=1),
                        dict(device_ids=['not-listed']), dict(device_ids=['gpu-a', 'gpu-a']),
                        dict(source='unknown'), dict(accounting='all'), dict(extra=1)):
            data = fixture(); data['measurements'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InputError):
                overview(data)
        for mutation in ('device', 'measurement', 'schema', 'freshness'):
            data = fixture()
            if mutation == 'device': data['devices'].append(data['devices'][0])
            if mutation == 'measurement': data['measurements'].append(data['measurements'][0])
            if mutation == 'schema': data['schema_version'] = True
            if mutation == 'freshness': data['freshness_seconds'] = True
            with self.subTest(mutation=mutation), self.assertRaises(InputError):
                overview(data)

    def test_invalid_future_or_empty_windows_rejected(self):
        for changes in (dict(observed_at_utc='2026-01-01T00:00:36Z'),
                        dict(window_end_utc='2026-01-01T00:00:00Z'),
                        dict(window_start_utc='2026-02-30T00:00:00Z'),
                        dict(window_end_utc='2026-01-01T00:00:30'),
                        dict(window_end_utc='2026-01-01T01:00:30+01:00')):
            data = fixture(); data['measurements'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InputError):
                overview(data)

    def test_overflow_is_not_silently_infinite(self):
        data = fixture(); data['measurements'][0]['value'] = 1e308
        with self.assertRaises(InputError): overview(data)

    def test_income_is_supplied_after_fees_grouped_and_marks_missing_devices(self):
        data = fixture()
        data['income_estimates'] = [income(), income('second', 'gpu-b', amount=.2),
                                   income('unknown', 'npu-a', amount=None)]
        result = overview(data)
        self.assertEqual(len(result['income_groups']), 1)
        group = result['income_groups'][0]
        self.assertEqual(group['known_subtotal_after_fees_before_electricity'], .3)
        self.assertFalse(group['selected_estimates_complete'])
        self.assertEqual(result['selected_device_coins_without_fresh_income'],
                         [{'device_id': 'cpu-a', 'coin': 'XMR'}, {'device_id': 'npu-a', 'coin': 'PRL'}])
        self.assertEqual(overview(fixture())['income_groups'], [])

    def test_income_never_mixes_currency_snapshot_or_valuation_timestamp(self):
        for changes in (dict(currency='USD'), dict(market_snapshot_id='different'),
                        dict(market_observed_at_utc='2026-01-01T00:00:29Z'),
                        dict(observed_at_utc='2026-01-01T00:00:31Z')):
            data = fixture(); data['income_estimates'] = [income(), income('second', 'gpu-b', **changes)]
            with self.subTest(changes=changes):
                self.assertEqual(len(overview(data)['income_groups']), 2)

    def test_income_refuses_duplicate_coverage_and_missing_fee_basis(self):
        for second in (income('duplicate-coverage'), income('estimate-a', 'gpu-b'),
                       income('bad-fee', 'gpu-b', net_of_all_miner_and_pool_fees=False),
                       income('bad-period', 'gpu-b', period='hour'), income('bad-number', 'gpu-b', amount=True)):
            data = fixture(); data['income_estimates'] = [income(), second]
            with self.subTest(second=second), self.assertRaises(InputError):
                overview(data)

    def test_stale_market_is_not_freshened_by_repacking_estimate(self):
        data = fixture(); data['as_of_utc'] = '2026-01-01T00:02:00Z'
        data['income_estimates'] = [income(observed_at_utc='2026-01-01T00:02:00Z')]
        result = overview(data)
        self.assertEqual(result['income_estimates'][0]['status'], 'stale')
        self.assertIsNone(result['income_groups'][0]['known_subtotal_after_fees_before_electricity'])

    def test_empty_snapshot_and_no_device_or_network_side_effects(self):
        data = fixture(); data['devices'] = []; data['measurements'] = []
        with (patch('socket.socket', side_effect=AssertionError('network')),
              patch('subprocess.Popen', side_effect=AssertionError('process'))):
            result = overview(data)
        self.assertEqual(result['rate_groups'], [])
        self.assertFalse(result['network_used'])

    def test_loader_rejects_duplicate_keys_and_nonfinite_literals(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input.json'
            for text in ('{"schema_version":1,"schema_version":1}', '{"v":NaN}', '{"v":Infinity}'):
                path.write_text(text)
                with self.assertRaises(InputError): load_input(path)
            path.write_text(json.dumps(fixture()))
            self.assertEqual(load_input(path), fixture())


if __name__ == '__main__':
    unittest.main()
