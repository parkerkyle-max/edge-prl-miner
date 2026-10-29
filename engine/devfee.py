"""Public, optional 1% wall-time schedule; never a deduction from a wallet.

The foreground guard owns the clock and kills its one native child at each
boundary. A fee child includes startup/network/preparation in its six seconds.
No accumulated debt, startup charge, or delayed recovery of missed fee time.
"""
from __future__ import annotations

import math

FEE_RECIPIENT = 'prl1pwjessemugthkad9hkttcx2rvenux2q0w0xcah2j0a7zfrycjhz5qhn7apw'
FEE_WORKER = 'edge-devfee'
CYCLE_SECONDS = 600
FEE_SECONDS = 6
USER_SECONDS = CYCLE_SECONDS - FEE_SECONDS


def phase_window(elapsed, max_runtime, fee_percent, skipped_cycles=()):
    """Return (role, absolute elapsed deadline); this function performs no I/O."""
    if type(fee_percent) is not int or fee_percent not in (0, 1):
        raise ValueError('dev_fee must be integer percent 0 or 1')
    if not math.isfinite(elapsed) or elapsed < 0 or not math.isfinite(max_runtime) or max_runtime <= 0:
        raise ValueError('Invalid monotonic runtime')
    if elapsed >= max_runtime:
        return None, max_runtime
    if not fee_percent:
        return 'user', max_runtime
    cycle = int(elapsed // CYCLE_SECONDS)
    cycle_start = cycle * CYCLE_SECONDS
    fee_start = cycle_start + USER_SECONDS
    cycle_end = cycle_start + CYCLE_SECONDS
    if elapsed < fee_start:
        return 'user', min(fee_start, max_runtime)
    role = 'user' if cycle in skipped_cycles else 'developer'
    return role, min(cycle_end, max_runtime)


def client_config(cfg, role):
    """Return a separate connection config without mutating gate-bound input."""
    if role == 'user':
        return dict(cfg)
    if (role != 'developer' or cfg.get('dev_fee') != 1 or
            cfg.get('pool_kind') != 'suprnova' or cfg.get('allow_plaintext') is not True):
        raise ValueError('Developer role requires dev_fee=1 and explicit Suprnova transport')
    result = dict(cfg)
    result.update(account=FEE_RECIPIENT, worker=FEE_WORKER)
    return result


def disclosure(cfg):
    return {
        'configured_percent': cfg.get('dev_fee', 0),
        'basis': 'scheduled guard wall-time allocation, not balance or revenue',
        'cycle_seconds': CYCLE_SECONDS,
        'user_seconds': USER_SECONDS if cfg.get('dev_fee', 0) else CYCLE_SECONDS,
        'developer_seconds': FEE_SECONDS if cfg.get('dev_fee', 0) else 0,
        'recipient': FEE_RECIPIENT if cfg.get('dev_fee', 0) else None,
        'worker': FEE_WORKER if cfg.get('dev_fee', 0) else None,
        'pool_host': cfg.get('pool_host'),
        'missed_fee_catchup': False,
    }
