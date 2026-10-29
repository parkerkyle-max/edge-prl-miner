"""Kryptex Pearl object Stratum, exact integer target/config, verified TLS only.

References: the frozen official zk-pow proof_utils.rs/sanity_checks.rs and the
maintainer-recommended ascend_prl fork's pools/kryptex.c and proofgz.c.
The verifier callback MUST run official cert-3 PlainProof verification using
the supplied immutable job's header and nbits_override. No callback means no
submissions. Subscription/authorization responses never increment share counts.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import datetime
import gzip
import hashlib
import json
import math
import queue
import re
import select
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional
import uuid

MAX256 = (1 << 256) - 1
MIN_DIFFICULTY = 2097152
MAX_DIFFICULTY = 4294967296
MAX_LINE = 2 * 1024 * 1024
MAX_PROOF = 4 * 1024 * 1024


class ProtocolError(ValueError):
    pass


class StaleJob(RuntimeError):
    pass


def _int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f'{name} must be an integer in [{minimum}, {maximum}]')
    return value


def _hex(value, size=None, max_size=None):
    if not isinstance(value, str) or not value or len(value) % 2 or not re.fullmatch('[0-9a-fA-F]+', value):
        raise ProtocolError('expected nonempty even-length hex without prefix')
    raw = bytes.fromhex(value)
    if size is not None and len(raw) != size:
        raise ProtocolError(f'expected {size} bytes, got {len(raw)}')
    if max_size is not None and len(raw) > max_size:
        raise ProtocolError('hex value exceeds bound')
    return raw


def target_to_compact(target: int) -> int:
    """Canonical positive Bitcoin compact representation, truncating downward."""
    _int(target, 'target', 1, MAX256)
    size = (target.bit_length() + 7) // 8
    word = target << (8 * (3-size)) if size <= 3 else target >> (8 * (size-3))
    if word & 0x00800000:
        word >>= 8
        size += 1
    return (size << 24) | word


def compact_to_target(nbits: int) -> int:
    _int(nbits, 'nbits', 1, 0xffffffff)
    exponent, mantissa = nbits >> 24, nbits & 0xffffff
    if mantissa & 0x800000 or not mantissa:
        raise ProtocolError('negative or zero compact target')
    target = mantissa >> (8 * (3-exponent)) if exponent <= 3 else mantissa << (8 * (exponent-3))
    if not 0 < target <= MAX256:
        raise ProtocolError('compact target overflow or underflow')
    return target


def pattern_from_bytes(raw: bytes) -> tuple[int, ...]:
    if len(raw) != 6:
        raise ProtocolError('periodic pattern is exactly 6 bytes')
    stride_min, done, points = 1, False, [0]
    for i in range(0, 6, 2):
        factor, length = raw[i]+1, raw[i+1]+1
        if length == 1 or done:
            if factor != 1 or length != 1:
                raise ProtocolError('noncanonical trailing pattern')
            done = True
        elif factor <= 1 and stride_min != 1:
            raise ProtocolError('single stride broken across dimensions')
        if stride_min > (1 << 24) // (factor * length):
            raise ProtocolError('pattern period overflow')
        stride = factor * stride_min
        points = [p+j*stride for j in range(length) for p in points]
        stride_min = stride * length
    return tuple(points)


def pattern_to_bytes(points: tuple[int, ...]) -> bytes:
    if not points or points[0] != 0 or any(type(x) is not int or x < 0 for x in points) or tuple(sorted(set(points))) != points:
        raise ProtocolError('pattern must be sorted, unique and start at zero')
    shape, p = [], list(points)
    while len(p) > 1:
        for period in range(1, len(p)):
            if len(p) % period == 0 and all(p[i]+p[period] == p[i+period] for i in range(len(p)-period)):
                shape.append((p[period], len(p)//period))
                p = p[:period]
                break
        else:
            raise ProtocolError('nonperiodic indices')
    if len(shape) > 3:
        raise ProtocolError('pattern exceeds three dimensions')
    shape.reverse()
    period = shape[-1][0]*shape[-1][1] if shape else 1
    shape += [(period, 1)]*(3-len(shape))
    result, stride_min = [], 1
    for stride, length in shape:
        if stride % stride_min or not 1 <= stride//stride_min <= 256 or not 1 <= length <= 256:
            raise ProtocolError('pattern cannot be canonically serialized')
        result += [stride//stride_min-1, length-1]
        stride_min = stride*length
    raw = bytes(result)
    if pattern_from_bytes(raw) != points:
        raise ProtocolError('pattern roundtrip mismatch')
    return raw


@dataclass(frozen=True)
class MiningConfig:
    m: int = 16384
    n: int = 16384
    k: int = 4096
    rank: int = 128
    rows: tuple[int, ...] = (0, 32)
    cols: tuple[int, ...] = tuple(range(64))

    def __post_init__(self):
        _int(self.m, 'm', 1, 1 << 24)
        _int(self.n, 'n', 1, 1 << 24)
        if self.k != 4096 or self.rank != 128:
            raise ProtocolError('this backend is pinned to K4096 rank128')
        if type(self.rows) is not tuple or type(self.cols) is not tuple:
            raise ProtocolError('patterns must be immutable tuples')
        pattern_to_bytes(self.rows); pattern_to_bytes(self.cols)
        if len(self.rows) % 2 or len(self.cols) % 2 or not 32 <= len(self.rows)*len(self.cols) <= 256:
            raise ProtocolError('invalid official jackpot tile shape')
        if max(self.rows) >= self.m or max(self.cols) >= self.n:
            raise ProtocolError('pattern lies outside matrix dimensions')
        if (len(self.rows)+len(self.cols))*self.k > 1 << 22:
            raise ProtocolError('witness exceeds official worker input limit')

    def to_bytes(self) -> bytes:
        raw = struct.pack('<IHH', self.k, self.rank, 0)+pattern_to_bytes(self.rows)+pattern_to_bytes(self.cols)+bytes(32)
        assert len(raw) == 52
        return raw

    @property
    def adjustment_factor(self) -> int:
        return len(self.rows)*len(self.cols)*(self.k//self.rank)*128


@dataclass(frozen=True)
class JobSnapshot:
    session_id: str
    generation: int
    job_id: str
    header: bytes
    cert_version: int
    height: int
    config: MiningConfig
    target: int
    nbits_override: int
    scaled_target: int
    received_at_utc: str

    @property
    def config_bytes(self):
        return self.config.to_bytes()

    @property
    def target_be32(self):
        return self.target.to_bytes(32, 'big')

    @property
    def scaled_target_le32(self):
        return self.scaled_target.to_bytes(32, 'little')

    @property
    def token(self):
        return f'{self.session_id}:{self.generation}:{self.job_id}'

    def to_envelope(self):
        return {'schema':'prl-npu-job-v1','session_id':self.session_id,'generation':self.generation,
                'job_id':self.job_id,'header_hex':self.header.hex(),'cert_version':self.cert_version,'height':self.height,
                'config_hex':self.config_bytes.hex(),'m':self.config.m,'n':self.config.n,'k':self.config.k,
                'rank':self.config.rank,'rows':list(self.config.rows),'cols':list(self.config.cols),
                'target_be_hex':self.target_be32.hex(),'nbits_override':self.nbits_override,
                'rounded_base_target_be_hex':compact_to_target(self.nbits_override).to_bytes(32,'big').hex(),
                'scaled_target_le_hex':self.scaled_target_le32.hex(),'received_at_utc':self.received_at_utc,
                'target_policy':'compact floor then exact rank128 scale; reject overflow',
                'job_token':self.token}


def parse_job(params, config: MiningConfig, session_id: str, generation: int):
    if not isinstance(params, dict):
        raise ProtocolError('Kryptex notify must be an object')
    jid = params.get('job_id')
    if not isinstance(jid, str) or not 1 <= len(jid) <= 256 or any(ord(c) < 32 for c in jid):
        raise ProtocolError('invalid job ID')
    cert = params.get('cert_version')
    if type(cert) is not int or cert != 3:
        raise ProtocolError('unsupported certificate version; only explicit cert3 accepted')
    header = _hex(params.get('header'), size=76)
    height = _int(params.get('height'), 'height', 0, 2**63-1)
    target = int.from_bytes(_hex(params.get('target'), max_size=32), 'big')
    if target <= 0:
        raise ProtocolError('zero target')
    nbits = target_to_compact(target)
    rounded = compact_to_target(nbits)
    factor = config.adjustment_factor
    if rounded > MAX256 // factor:
        raise ProtocolError('unusable scaled target overflows uint256')
    if rounded > target:
        raise ProtocolError('compact conversion unexpectedly loosened target')
    return JobSnapshot(session_id,generation,jid,header,cert,height,config,target,nbits,rounded*factor,
                       datetime.datetime.now(datetime.timezone.utc).isoformat())


def _bounded_connect(host, port, timeout):
    deadline = time.monotonic()+timeout
    result = queue.Queue(maxsize=1)
    def resolve():
        try:result.put((socket.getaddrinfo(host,port,type=socket.SOCK_STREAM),None))
        except Exception as exc:result.put((None,exc))
    threading.Thread(target=resolve,daemon=True).start()
    try:addresses,error=result.get(timeout=timeout)
    except queue.Empty:raise TimeoutError('DNS deadline') from None
    if error:raise error
    last = None
    for family, socktype, proto, _, address in addresses:
        raw=socket.socket(family,socktype,proto)
        try:
            left=deadline-time.monotonic()
            if left<=0:raise TimeoutError('connect deadline')
            raw.settimeout(left);raw.connect(address)
            raw.settimeout(max(.1,deadline-time.monotonic()))
            return raw
        except Exception as exc:raw.close();last=exc
    raise last or OSError('DNS returned no addresses')


class StratumClient:
    def __init__(self, account: str, worker: str, config: Optional[MiningConfig]=None,
                 verify_proof: Optional[Callable[[JobSnapshot,bytes],bool]]=None,
                 on_event: Optional[Callable[[dict],None]]=None,
                 host='prl-us.kryptex.network',port=8048,password='d=2097152',
                 agent='edge-prl-experimental/0.1',connect_timeout=10,read_timeout=300,
                 submission_deadline=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',account) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',worker):
            raise ValueError('account/worker must be bounded simple identifiers')
        if password:
            if not re.fullmatch(r'd=[0-9]+',password) or not MIN_DIFFICULTY<=int(password[2:])<=MAX_DIFFICULTY:
                raise ValueError('password must request documented supported d=2097152..4294967296 or be empty')
        self.identity=account+'/'+worker
        self.worker=worker;self.config=config or MiningConfig();self.verify_proof=verify_proof;self.on_event=on_event
        self.host=host;self.port=port;self.password=password;self.agent=agent
        self.connect_timeout=connect_timeout;self.read_timeout=read_timeout
        if submission_deadline is not None and (type(submission_deadline) not in (int,float) or not math.isfinite(submission_deadline) or submission_deadline<=0):
            raise ValueError('submission_deadline must be a finite absolute monotonic timestamp')
        self.submission_deadline=submission_deadline
        self._lock=threading.RLock();self._io_lock=threading.Lock();self._stop=threading.Event();self._changed=threading.Condition(self._lock)
        self._thread=None;self._sock=None;self._session='';self._generation=0;self._job=None
        self._authorized=False;self._gzip=False;self._next_id=0;self._pending={};self._sent_proofs=set()
        self._last_message_monotonic=None
        self.stats={'sessions':0,'authorized_sessions':0,'jobs':0,'submitted':0,'accepted':0,'rejected':0,
                    'submit_response_unknown':0,'submit_disconnect_unknown':0,'local_stale_dropped':0,'local_verification_rejected':0}

    def _emit(self,event,**fields):
        item={'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event':event,**fields}
        if self.on_event:
            try:self.on_event(item)
            except Exception:pass  # A logging consumer must not break protocol processing.

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():return self
            self._stop.clear();self._thread=threading.Thread(target=self._run,name='prl-tls-reader',daemon=True);self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            sock=self._sock
            self._invalidate('client_stopped')
            if sock:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
        if self._thread and self._thread is not threading.current_thread():self._thread.join(self.connect_timeout+2)

    def get_latest_job(self):
        with self._lock:return self._job if self._authorized and self._gzip and self._sock else None

    def is_current(self,job):
        with self._lock:return self._job == job and self._authorized and self._gzip and self._sock is not None and not self._stop.is_set()

    def wait_for_job(self,after_token=None,timeout=1):
        with self._changed:
            self._changed.wait_for(lambda:self._stop.is_set() or (self.get_latest_job() is not None and self._job.token!=after_token),timeout)
            return self.get_latest_job()

    def status(self):
        with self._lock:return {**self.stats,'authorized':self._authorized,'gzip_v2':self._gzip,'session_id':self._session,
                                'job_token':self._job.token if self._job else None,'pending_submits':sum(p['kind']=='submit' for p in self._pending.values()),
                                'ui_visibility':'NOT_ESTABLISHED_BY_CONNECTION_OR_AUTHORIZATION'}

    def _invalidate(self,reason):
        self._generation+=1;self._job=None;self._sent_proofs.clear();self._changed.notify_all()
        self._emit('job_invalidated',reason=reason,generation=self._generation)

    def _ensure_before_deadline(self):
        if self.submission_deadline is not None and time.monotonic()>=self.submission_deadline:
            raise StaleJob('Guard mining phase deadline expired; no further request may be sent')

    def _request(self,kind,method,params,metadata=None):
        # Caller holds _lock, serializing job replacement with final submit send.
        self._ensure_before_deadline()
        if self._sock is None:raise ConnectionError('not connected')
        self._next_id+=1;rid=self._next_id
        message={'id':rid,'method':method,'params':params}
        wire=(json.dumps(message,separators=(',',':'))+'\n').encode()
        if len(wire)>MAX_LINE:raise ProtocolError('outgoing message too large')
        self._pending[rid]={'kind':kind,'session_id':self._session,**(metadata or {})}
        try:
            # SSLSocket/OpenSSL object calls are serialized with reader recv.
            with self._io_lock:
                self._ensure_before_deadline()
                self._sock.sendall(wire)
        except StaleJob:
            self._pending.pop(rid,None)
            if kind=='submit':self.stats['local_stale_dropped']+=1
            raise
        except Exception:
            self._pending.pop(rid,None)
            if kind=='submit':self.stats['submit_disconnect_unknown']+=1
            self._authorized=False;self._gzip=False;self._invalidate('send_failed')
            try:self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            raise
        return rid

    def submit_proof(self,job: JobSnapshot,raw_bincode: bytes):
        self._ensure_before_deadline()
        if type(raw_bincode) is not bytes or not 1<=len(raw_bincode)<=MAX_PROOF:
            raise ValueError('proof must be bounded immutable raw bincode bytes')
        if not self.is_current(job):
            with self._lock:self.stats['local_stale_dropped']+=1
            raise StaleJob('job changed before verification')
        if self.verify_proof is None:
            raise RuntimeError('official cert3 proof verifier callback is required for live submission')
        verified=self.verify_proof(job,raw_bincode)
        self._ensure_before_deadline()
        if verified is not True:
            with self._lock:self.stats['local_verification_rejected']+=1
            raise ProtocolError('official local proof verification did not pass')
        proof_sha=hashlib.sha256(raw_bincode).hexdigest()
        encoded=base64.b64encode(gzip.compress(raw_bincode,compresslevel=6,mtime=0)).decode('ascii')
        with self._lock:
            if not self.is_current(job):
                self.stats['local_stale_dropped']+=1
                raise StaleJob('job changed during verification/encoding')
            if proof_sha in self._sent_proofs:raise ProtocolError('duplicate proof for current job')
            if sum(p['kind']=='submit' for p in self._pending.values())>=16:raise ProtocolError('too many pending submissions')
            rid=self._request('submit','mining.submit',{'worker':self.identity,'job_id':job.job_id,'plain_proof':encoded},
                              {'job_token':job.token,'proof_sha256':proof_sha})
            self._sent_proofs.add(proof_sha);self.stats['submitted']+=1
        self._emit('share_submitted',request_id=rid,job_token=job.token,proof_sha256=proof_sha,proof_bytes=len(raw_bincode))
        return rid

    def _handle_message(self,message):
        if not isinstance(message,dict):raise ProtocolError('message must be a JSON object')
        with self._lock:
            method=message.get('method')
            if method=='mining.notify':
                self._invalidate('new_notify')
                try:self._job=parse_job(message.get('params'),self.config,self._session,self._generation)
                except Exception:
                    self._emit('job_rejected',reason='malformed_or_unsupported_job');raise
                self.stats['jobs']+=1;self._changed.notify_all()
                self._emit('job_received',job=self._job.to_envelope(),authorized=self._authorized)
                return
            if method=='mining.set_difficulty':
                params=message.get('params')
                if not isinstance(params,list) or len(params)!=1 or type(params[0]) not in (int,float) or not math.isfinite(params[0]) or params[0]<=0:
                    raise ProtocolError('invalid difficulty notification')
                self._invalidate('difficulty_changed_waiting_for_authoritative_notify_target')
                self._emit('difficulty_notification',difficulty=params[0]);return
            if method:
                self._emit('ignored_notification',method=str(method)[:100]);return
            rid=message.get('id')
            if type(rid) is not int:return
            pending=self._pending.pop(rid,None)
            if not pending or pending['session_id']!=self._session:
                self._emit('unmatched_response',request_id=rid);return
            success=message.get('result') is True and message.get('error') is None
            if pending['kind']=='authorize':
                if not success or message.get('type')!='v2':
                    self._authorized=False;self._gzip=False;self._invalidate('authorization_or_gzip_negotiation_failed')
                    raise ProtocolError('pool did not authorize requested gzip-v2 protocol')
                self._authorized=True;self._gzip=True;self.stats['authorized_sessions']+=1;self._changed.notify_all()
                self._emit('authorized_v2',request_id=rid);return
            if pending['kind']=='submit':
                if success:self.stats['accepted']+=1;event='share_accepted'
                elif message.get('error') is not None or message.get('result') is False:self.stats['rejected']+=1;event='share_rejected'
                else:self.stats['submit_response_unknown']+=1;event='share_response_unknown'
                self._emit(event,request_id=rid,job_token=pending['job_token'],proof_sha256=pending['proof_sha256'],
                           error=message.get('error'),result=message.get('result'))
            else:self._emit('subscription_response',request_id=rid,error=message.get('error'))

    def _run(self):
        delay=5
        while not self._stop.is_set():
            connection=None;healthy_session=False
            try:
                raw=_bounded_connect(self.host,self.port,self.connect_timeout)
                try:connection=ssl.create_default_context().wrap_socket(raw,server_hostname=self.host)
                except Exception:raw.close();raise
                if self._stop.is_set():break
                connection.settimeout(1)
                with self._lock:
                    self._sock=connection;self._session=uuid.uuid4().hex;self._authorized=False;self._gzip=False;self._pending.clear();self._invalidate('new_tls_session')
                    self.stats['sessions']+=1
                    self._emit('tls_connected',session_id=self._session,tls_version=connection.version(),
                               certificate_sha256=hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest(),hostname_verified=True)
                    self._request('subscribe','mining.subscribe',[self.agent])
                    self._request('authorize','mining.authorize',{'wallet':self.identity,'agent':self.agent,'password':self.password,'type':'v2'})
                buf=bytearray();self._last_message_monotonic=time.monotonic();session_started=time.monotonic()
                while not self._stop.is_set():
                    if not self._authorized and time.monotonic()-session_started>20:raise TimeoutError('authorization deadline')
                    if time.monotonic()-self._last_message_monotonic>self.read_timeout:raise TimeoutError('pool idle deadline')
                    try:
                        # Do not hold the SSL object lock while waiting for bytes:
                        # repeated timed recv/reacquire can starve a submitting
                        # thread. Decrypted pending bytes need no socket readiness.
                        with self._io_lock:has_pending=connection.pending()>0
                        if not has_pending and not select.select([connection],[],[],1)[0]:
                            continue
                        with self._io_lock:chunk=connection.recv(65536)
                    except socket.timeout:continue
                    if not chunk:raise ConnectionError('pool disconnected')
                    buf.extend(chunk)
                    if len(buf)>MAX_LINE:raise ProtocolError('protocol line limit')
                    while b'\n' in buf:
                        line,_,remainder=buf.partition(b'\n');buf=bytearray(remainder)
                        if line.strip():
                            self._handle_message(json.loads(line))
                            self._last_message_monotonic=time.monotonic()
                            healthy_session=healthy_session or self.get_latest_job() is not None
            except Exception as exc:
                self._emit('connection_error',error_type=type(exc).__name__,message=str(exc)[:300])
            finally:
                with self._lock:
                    self.stats['submit_disconnect_unknown']+=sum(p['kind']=='submit' for p in self._pending.values())
                    self._pending.clear();self._sock=None;self._authorized=False;self._gzip=False;self._invalidate('disconnected')
                if connection:
                    try:connection.close()
                    except OSError:pass
            if healthy_session:delay=5
            if self._stop.wait(delay):break
            if not healthy_session:delay=min(delay*2,60)
