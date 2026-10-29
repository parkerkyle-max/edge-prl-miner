"""An honest CPU/GPU/NPU snapshot, with no device control or mining side effects.

Only explicitly comparable rate definitions are summed. This module never
converts MAC/s into H/s, infers income from rates, or double-counts local and
pool observations covering the same physical device and algorithm.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import math
from pathlib import Path
import re

from .economics import load_input as _load_strict_json


class InputError(ValueError):
    """The supplied snapshot cannot support unambiguous accounting."""


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_COIN = re.compile(r'[A-Z][A-Z0-9]{0,15}\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z')
_UNITS = {prefix + unit: (unit, Decimal(10) ** power)
          for prefix, power in (('', 0), ('k', 3), ('M', 6), ('G', 9), ('T', 12))
          for unit in ('H/s', 'MAC/s')}
_BASES = {
    'local_reported_hashes_per_wall_second': ('local', 'H/s'),
    'completed_nonstale_macs_per_wall_second': ('local', 'MAC/s'),
    'pool_effective_hashes_per_wall_second': ('pool', 'H/s'),
}


def _object(value, where, required, optional=()):
    if type(value) is not dict:
        raise InputError(where + ' must be an object')
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise InputError(where + ' has missing or unknown fields')
    return value


def _identifier(value, where, pattern=_ID):
    if type(value) is not str or not pattern.fullmatch(value):
        raise InputError(where + ' must be a bounded identifier')
    return value


def _timestamp(value, where):
    if type(value) is not str or not _UTC.fullmatch(value):
        raise InputError(where + ' must be explicit UTC ISO 8601')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InputError(where + ' is not a valid calendar timestamp') from exc


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _amount(value, where):
    if value is None:
        return None
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise InputError(where + ' must be a finite nonnegative number or null')
    result = Decimal(str(value))
    if result < 0:
        raise InputError(where + ' must not be negative')
    return result


def _number(value):
    if value is None:
        return None
    try:
        converted = float(value)
    except (ValueError, OverflowError) as exc:
        raise InputError('Normalized amount exceeds the finite JSON number range') from exc
    if not math.isfinite(converted) or (value and not converted):
        raise InputError('Normalized amount exceeds the finite nonzero JSON number range')
    # Preserve integer precision rather than forcing large H/s totals through
    # an IEEE double. Fractional inputs are added with Decimal, not binary float.
    return int(value) if value == value.to_integral_value() else converted


def _rows(value, where, limit):
    if type(value) is not list or len(value) > limit:
        raise InputError(where + ' must be a bounded list')
    return value


def _coverage(row, devices, where):
    ids = _rows(row['device_ids'], where + '.device_ids', 256)
    if not ids or any(type(item) is not str or item not in devices for item in ids):
        raise InputError(where + ' must reference known physical device IDs')
    if len(set(ids)) != len(ids):
        raise InputError(where + ' contains duplicate physical device IDs')
    if type(row['selected']) is not bool:
        raise InputError(where + '.selected must be boolean')
    return sorted(ids)


def _claim_coverage(claims, keys, row_id):
    for key in keys:
        if key in claims:
            raise InputError('Overlapping selected device coverage: ' + claims[key] + ' and ' + row_id)
        claims[key] = row_id


def _state(selected, amount, as_of, observed, window_end, freshness):
    if not selected:
        return 'not_selected'
    if amount is None:
        return 'unknown'
    if max((as_of - observed).total_seconds(), (as_of - window_end).total_seconds()) > freshness:
        return 'stale'
    return 'included'


def _overview(data: dict) -> dict:
    """Return grouped supplied observations, never a universal combined hashrate.

    ``as_of_utc`` is caller supplied for reproducible historical reports. This
    function does not read the clock or verify a live miner, pool or valuation.
    """
    root = _object(data, 'input', {'schema_version', 'as_of_utc', 'freshness_seconds', 'devices', 'measurements'},
                   {'income_estimates'})
    if type(root['schema_version']) is not int or root['schema_version'] != 1:
        raise InputError('schema_version must be integer 1')
    freshness = root['freshness_seconds']
    if type(freshness) is not int or not 1 <= freshness <= 3600:
        raise InputError('freshness_seconds must be integer 1 through 3600')
    as_of = _timestamp(root['as_of_utc'], 'as_of_utc')
    devices = {}
    for row in _rows(root['devices'], 'devices', 256):
        _object(row, 'device', {'device_id', 'kind'})
        identifier = _identifier(row['device_id'], 'device_id')
        if identifier in devices or row['kind'] not in ('CPU', 'GPU', 'NPU'):
            raise InputError('Devices require unique physical IDs and CPU/GPU/NPU kinds')
        devices[identifier] = row['kind']
    observed_rows, groups, measurement_ids, selected_coverage = [], {}, set(), {}
    selected_device_coins = set()
    for row in _rows(root['measurements'], 'measurements', 1024):
        _object(row, 'measurement', {'measurement_id', 'device_ids', 'source', 'source_id', 'coin', 'algorithm',
                'rate_definition', 'basis', 'unit', 'value', 'window_start_utc', 'window_end_utc',
                'observed_at_utc', 'accounting', 'selected'})
        identifier = _identifier(row['measurement_id'], 'measurement_id')
        if identifier in measurement_ids:
            raise InputError('Duplicate measurement_id')
        measurement_ids.add(identifier)
        ids = _coverage(row, devices, 'measurement')
        coin = _identifier(row['coin'], 'coin', _COIN)
        algorithm = _identifier(row['algorithm'], 'algorithm')
        definition = _identifier(row['rate_definition'], 'rate_definition')
        source_id = _identifier(row['source_id'], 'source_id')
        if type(row['unit']) is not str or row['unit'] not in _UNITS:
            raise InputError('Unsupported rate unit; H/s and MAC/s have separate dimensions')
        unit, factor = _UNITS[row['unit']]
        if type(row['basis']) is not str or _BASES.get(row['basis']) != (row['source'], unit):
            raise InputError('Rate basis, source and unit dimension do not match')
        if row['accounting'] not in ('user', 'developer', 'gross'):
            raise InputError('accounting must be user, developer or gross')
        start = _timestamp(row['window_start_utc'], 'window_start_utc')
        end = _timestamp(row['window_end_utc'], 'window_end_utc')
        observed = _timestamp(row['observed_at_utc'], 'observed_at_utc')
        if not start < end <= observed <= as_of:
            raise InputError('Require window_start < window_end <= observed_at <= as_of')
        amount = _amount(row['value'], 'rate value')
        normalized = None if amount is None else amount * factor
        status = _state(row['selected'], amount, as_of, observed, end, freshness)
        output = dict(row, device_ids=ids, canonical_unit=unit, normalized_value=_number(normalized), status=status)
        observed_rows.append(output)
        if not row['selected']:
            continue
        _claim_coverage(selected_coverage, ((device, coin, algorithm) for device in ids), identifier)
        selected_device_coins.update((device, coin) for device in ids)
        key = (coin, algorithm, definition, row['basis'], row['source'], row['accounting'], unit, _iso(start), _iso(end))
        group = groups.setdefault(key, {'total': Decimal(0), 'included': [], 'excluded': [], 'devices': set(), 'sources': set()})
        group['devices'].update(ids)
        group['sources'].add(source_id)
        group['included' if status == 'included' else 'excluded'].append(identifier)
        if status == 'included':
            group['total'] += normalized
    rate_groups = []
    for key, group in sorted(groups.items()):
        fields = ('coin', 'algorithm', 'rate_definition', 'basis', 'source', 'accounting', 'unit',
                  'window_start_utc', 'window_end_utc')
        rate_groups.append(dict(zip(fields, key), known_subtotal=_number(group['total']) if group['included'] else None,
                 selected_coverage_complete=not group['excluded'], included_measurement_ids=group['included'],
                 excluded_measurement_ids=group['excluded'], device_ids=sorted(group['devices']), source_ids=sorted(group['sources'])))

    income_rows, income_groups, income_ids, income_coverage, income_included = [], {}, set(), {}, set()
    for row in _rows(root.get('income_estimates', []), 'income_estimates', 1024):
        _object(row, 'income estimate', {'estimate_id', 'device_ids', 'coin', 'currency', 'period', 'amount',
            'source_id', 'market_snapshot_id', 'market_observed_at_utc', 'observed_at_utc',
            'net_of_all_miner_and_pool_fees', 'selected'})
        identifier = _identifier(row['estimate_id'], 'estimate_id')
        if identifier in income_ids:
            raise InputError('Duplicate estimate_id')
        income_ids.add(identifier)
        ids = _coverage(row, devices, 'income estimate')
        coin = _identifier(row['coin'], 'income coin', _COIN)
        currency = _identifier(row['currency'], 'currency', _CURRENCY)
        source_id = _identifier(row['source_id'], 'income source_id')
        market_id = _identifier(row['market_snapshot_id'], 'market_snapshot_id')
        if row['period'] != 'day' or row['net_of_all_miner_and_pool_fees'] is not True:
            raise InputError('Income must be per day and explicitly net of all miner and pool fees')
        market_time = _timestamp(row['market_observed_at_utc'], 'market_observed_at_utc')
        observed = _timestamp(row['observed_at_utc'], 'income observed_at_utc')
        if not market_time <= observed <= as_of:
            raise InputError('Require market timestamp <= observed_at <= as_of')
        amount = _amount(row['amount'], 'income amount')
        status = _state(row['selected'], amount, as_of, observed, market_time, freshness)
        income_rows.append(dict(row, device_ids=ids, status=status))
        if not row['selected']:
            continue
        _claim_coverage(income_coverage, ((device, coin) for device in ids), identifier)
        key = (currency, 'day', market_id, _iso(market_time), _iso(observed))
        group = income_groups.setdefault(key, {'amount': Decimal(0), 'included': [], 'excluded': [], 'sources': set()})
        group['sources'].add(source_id)
        group['included' if status == 'included' else 'excluded'].append(identifier)
        if status == 'included':
            group['amount'] += amount
            income_included.update((device, coin) for device in ids)
    estimates = []
    for key, group in sorted(income_groups.items()):
        fields = ('currency', 'period', 'market_snapshot_id', 'market_observed_at_utc', 'observed_at_utc')
        estimates.append(dict(zip(fields, key), known_subtotal_after_fees_before_electricity=
            _number(group['amount']) if group['included'] else None,
            selected_estimates_complete=not group['excluded'], included_estimate_ids=group['included'],
            excluded_estimate_ids=group['excluded'], source_ids=sorted(group['sources']),
            scope='Supplied selected estimates only; not payout, profit or a complete device valuation'))
    return {'schema_version': 1, 'status': 'SUPPLIED_SNAPSHOT_NOT_LIVE_VERIFICATION', 'as_of_utc': _iso(as_of),
            'freshness_seconds': freshness, 'devices': [{'device_id': k, 'kind': v} for k, v in sorted(devices.items())],
            'measurements': observed_rows, 'rate_groups': rate_groups,
            'combined_cpu_gpu_npu_hashrate': None,
            'combined_hashrate_reason': 'Different algorithms, dimensions, rate definitions or windows cannot be added; use the comparable groups.',
            'income_estimates': income_rows, 'income_groups': estimates,
            'selected_device_coins_without_fresh_income': [{'device_id': d, 'coin': c} for d, c in sorted(selected_device_coins - income_included)],
            'network_used': False, 'miner_started_or_modified': False, 'fees_collected': False,
            'assumptions': ['Physical device IDs and rate definitions are supplied by the caller, not independently verified.',
                            'Only SI prefixes within H/s or within MAC/s are normalized; no MAC-to-hash equivalence is assumed.',
                            'Unknown and stale selected observations are excluded, never replaced with zero or extrapolated.',
                            'Income estimates are caller supplied, net of miner/pool fees and before electricity; none are inferred from rates.',
                            'The explicit as_of timestamp permits historical replay; it is not a claim of freshness at the present time.']}


def load_input(path: Path) -> dict:
    """Load bounded strict JSON, rejecting duplicate keys and nonfinite tokens."""
    try:
        return _load_strict_json(path)
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def overview(data: dict) -> dict:
    """Group a supplied snapshot without reading devices, network or the clock."""
    # Keep exact SI scaling and sums across the finite JSON float exponent
    # range; never change the caller's process-wide Decimal context.
    with localcontext() as context:
        context.prec = 700
        return _overview(data)
