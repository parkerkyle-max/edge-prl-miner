"""Explicit Suprnova low-port experimental client.

Official protocol: https://prl.suprnova.cc/stratum-spec.html
Only the explicitly selected plaintext 3370 endpoint is allowed. This client
never infers a missing certificate version, never changes a received target,
and inherits the existing official-proof callback and correlated submit checks.
The 40-minute network ceiling is a secondary bound; the foreground guard also
terminates its owned miner process at the configured deadline. No service is
started, stopped, or restored by this package.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import select
import socket
import sys
import time
import uuid

from .kryptex import MAX_LINE, ProtocolError, StratumClient, _bounded_connect

HOSTS = frozenset(('prl.suprnova.cc', 'stratum-us.suprnova.cc',
                   'stratum-eu2.suprnova.cc', 'stratum-apac.suprnova.cc'))


class SuprnovaClient(StratumClient):
    def __init__(self, account, worker, config=None, verify_proof=None,
                 on_event=None, *, host='stratum-us.suprnova.cc', port=3370,
                 allow_plaintext=False, max_runtime_seconds=2400,
                 agent='edge-prl-experimental/0.1',
                 connect_timeout=10, read_timeout=300, submission_deadline=None):
        if allow_plaintext is not True or host not in HOSTS or port != 3370:
            raise ValueError('Explicit plaintext opt-in and official Suprnova port3370 required')
        if not isinstance(account, str) or not re.fullmatch(r'prl1[023456789acdefghjklmnpqrstuvwxyz]{20,120}', account):
            raise ValueError('A bounded public prl1 wallet address is required; krx accounts are not supported')
        if type(max_runtime_seconds) not in (int, float) or not 1 <= max_runtime_seconds <= 2400:
            raise ValueError('Runtime must be finite and between1 and2400 seconds')
        super().__init__(account, worker, config, verify_proof, on_event,
                         host=host, port=port, password='', agent=agent,
                         connect_timeout=min(connect_timeout, 10),
                         read_timeout=min(read_timeout, 300),submission_deadline=submission_deadline)
        self.wallet = account
        self._experiment_deadline = time.monotonic() + max_runtime_seconds
        if submission_deadline is not None:
            self._experiment_deadline=min(self._experiment_deadline,submission_deadline)
        self._terminal_error = None

    def status(self):
        result = super().status()
        # The inherited internal _gzip flag means encoding is usable. There is
        # no Kryptex type=v2 negotiation on this pool, so do not report one.
        result.pop('gzip_v2', None)
        result.update(pool_name='suprnova', transport='explicit_plaintext_tcp',
                      proof_encoding='gzip_autodetect',
                      terminal_error=self._terminal_error,
                      runtime_remaining_seconds=max(0, self._experiment_deadline-time.monotonic()))
        return result

    def _request(self, kind, method, params, metadata=None):
        if kind == 'submit':
            # Base submit_proof supplies the historical Kryptex worker field.
            # Suprnova submissions are bound to their authorized connection.
            params = {'job_id': params['job_id'], 'plain_proof': params['plain_proof']}
        return super()._request(kind, method, params, metadata)

    def _handle_message(self, message):
        if not isinstance(message, dict):
            raise ProtocolError('message must be a JSON object')
        with self._lock:
            rid = message.get('id')
            pending = self._pending.get(rid) if type(rid) is int else None
            if (not message.get('method') and pending and
                    pending['session_id'] == self._session and pending['kind'] == 'authorize'):
                self._pending.pop(rid)
                if message.get('result') is not True or message.get('error') is not None:
                    self._authorized = self._gzip = False
                    self._invalidate('suprnova_authorization_failed')
                    raise ProtocolError('Suprnova authorization rejected: '+str(message.get('error'))[:200])
                self._authorized = self._gzip = True
                self.stats['authorized_sessions'] += 1
                self._changed.notify_all()
                self._emit('authorized_suprnova_gzip_autodetect', request_id=rid)
                return
        # Includes unchanged explicit cert3 parsing, compact floor, target
        # scaling, immutable generations, and matching submit response checks.
        return super()._handle_message(message)

    def _run(self):
        delay = 5
        while not self._stop.is_set() and time.monotonic() < self._experiment_deadline:
            connection = None
            healthy_session = False
            try:
                left = self._experiment_deadline-time.monotonic()
                connection = _bounded_connect(self.host, self.port, min(self.connect_timeout, left))
                if self._stop.is_set():
                    break
                connection.settimeout(min(1, max(.01, self._experiment_deadline-time.monotonic())))
                with self._lock:
                    self._sock = connection
                    self._session = uuid.uuid4().hex
                    self._authorized = self._gzip = False
                    self._pending.clear()
                    self._invalidate('new_explicit_plaintext_suprnova_session')
                    self.stats['sessions'] += 1
                    self._emit('suprnova_tcp_connected', session_id=self._session,
                               host=self.host, port=self.port, transport='plaintext')
                    # No subscribe, no account/worker concatenation, no fixed
                    # difficulty request. Let port3370's server VarDiff operate.
                    self._request('authorize', 'mining.authorize', {
                        'wallet': self.wallet, 'worker': self.worker,
                        'pass': 'x', 'agent': self.agent})
                buf = bytearray()
                self._last_message_monotonic = time.monotonic()
                session_started = time.monotonic()
                while not self._stop.is_set() and time.monotonic() < self._experiment_deadline:
                    if not self._authorized and time.monotonic()-session_started > 20:
                        raise TimeoutError('authorization deadline')
                    if time.monotonic()-self._last_message_monotonic > self.read_timeout:
                        raise TimeoutError('pool idle deadline')
                    wait = min(1, max(0, self._experiment_deadline-time.monotonic()))
                    if not select.select([connection], [], [], wait)[0]:
                        continue
                    try:
                        with self._io_lock:
                            chunk = connection.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError('pool disconnected')
                    buf.extend(chunk)
                    if len(buf) > MAX_LINE:
                        raise ProtocolError('protocol line limit')
                    while b'\n' in buf:
                        line, _, remainder = buf.partition(b'\n')
                        buf = bytearray(remainder)
                        if line.strip():
                            self._handle_message(json.loads(line))
                            self._last_message_monotonic = time.monotonic()
                            healthy_session = healthy_session or self.get_latest_job() is not None
            except Exception as exc:
                self._emit('connection_error', error_type=type(exc).__name__, message=str(exc)[:300])
                if isinstance(exc, (ProtocolError, json.JSONDecodeError)):
                    self._terminal_error = type(exc).__name__+': '+str(exc)[:300]
                    self._stop.set()
            finally:
                with self._lock:
                    self.stats['submit_disconnect_unknown'] += sum(p['kind']=='submit' for p in self._pending.values())
                    self._pending.clear()
                    self._sock = None
                    self._authorized = self._gzip = False
                    self._invalidate('disconnected')
                if connection:
                    try:
                        connection.close()
                    except OSError:
                        pass
            if healthy_session:
                delay = 5
            remaining = max(0, self._experiment_deadline-time.monotonic())
            if self._stop.wait(min(delay, remaining)):
                break
            if not healthy_session:
                delay = min(delay*2, 60)
        if time.monotonic() >= self._experiment_deadline:
            self._emit('experiment_network_deadline_reached')
        self._stop.set()
