"""Exact packed-output optimization tests; no NPU, wallet or network is used."""
from contextlib import ExitStack
import ctypes as C
import hashlib
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import miner
from engine.backend import batch_backend as adapter
from engine.pool.kryptex import MAX256
from test_engine_protocol import client as protocol_client


def legacy_candidates(packed, target):
    return [(i // 32, packed[i:i+32]) for i in range(0, len(packed), 32)
            if int.from_bytes(packed[i:i+32], 'little') <= target]


class CandidateFilterTests(unittest.TestCase):
    def test_unsigned_little_endian_inclusive_boundaries(self):
        for target in (0, 1, 255, 256, 2**64, 2**128-1, 2**248, MAX256-1, MAX256):
            values = sorted({0, MAX256, target, max(0,target-1), min(MAX256,target+1)})
            packed = b''.join(value.to_bytes(32, 'little') for value in values)
            self.assertEqual(miner.packed_candidates(packed, target), legacy_candidates(packed,target))

    def test_every_candidate_and_lowest_target_are_preserved(self):
        packed = bytes(32) + bytes([255])*32*510 + bytes(32)
        self.assertEqual([i for i,_ in miner.packed_candidates(packed,0)], [0,511])
        self.assertEqual(len(miner.packed_candidates(packed, MAX256)),512)

    def test_randomized_vectors_exactly_match_original_integer_filter(self):
        rng=random.Random(512256)
        packed=bytes(rng.randrange(256) for _ in range(512*32))
        targets=[rng.getrandbits(256) for _ in range(30)] + [2**exponent-1 for exponent in range(0,257,16)]
        for target in targets:
            with self.subTest(target=target):
                self.assertEqual(miner.packed_candidates(packed,target),legacy_candidates(packed,target))

    def test_malformed_or_mutable_input_and_invalid_target_rejected(self):
        for data in (b'',bytes(31),bytes(33),bytearray(32),'x'*32,None):
            with self.assertRaises(ValueError):miner.packed_candidates(data,1)
        for target in (-1,MAX256+1,True,1.0,'1'):
            with self.assertRaises(ValueError):miner.packed_candidates(bytes(32),target)


class Function:
    def __init__(self, callback):self.callback=callback
    def __call__(self,*args):return self.callback(*args)


class FakeNative:
    def __init__(self):
        self.calls=0;self.return_code=0;self.poisoned=0;self.incomplete=False
        self.output_ids=[];self.key_ids=[];self.callback=None;self.destroyed=[]
        self.prl_batch_create=Function(lambda *a:17)
        self.prl_batch_last_error=Function(lambda:b'fake native failure')
        self.prl_scan_batch=Function(self.scan)
        self.prl_batch_get_profile=Function(self.profile)
        self.prl_batch_destroy=Function(lambda handle:self.destroyed.append(handle))
    def scan(self,*args):
        self.calls+=1
        self.output_ids.append(id(args[11]));self.key_ids.append(id(args[10]))
        if self.callback:self.callback()
        C.memmove(args[11],bytes([self.calls%256])*16384,16384)
        C.cast(args[13],C.POINTER(C.c_uint64)).contents.value=268435456
        return self.return_code
    def profile(self,handle,out):
        profile=C.cast(out,C.POINTER(adapter.Profile)).contents
        profile.abi_version=1;profile.struct_bytes=C.sizeof(profile)
        profile.segments=31 if self.incomplete else 32
        profile.completed_macs=268435456;profile.executed_macs=268435456
        profile.wall_ns=1000;profile.thread_cpu_ns=500;profile.driver_wall_ns=400
        profile.status=self.return_code;profile.poisoned=self.poisoned
        return 0


class PackedAdapterTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        path=Path(directory.name)/'synthetic-model';path.write_bytes(b'offline fixture')
        self.native=FakeNative()
        with patch.object(adapter.C,'CDLL',return_value=self.native):
            self.backend=adapter.BatchBackend('not-a-real-library',path,'f32',hashlib.sha256(path.read_bytes()).hexdigest())
        self.addCleanup(self.backend.close)
    def scan(self,key=bytes(32)):
        return self.backend.scan_packed_addresses(1,2,256,256,0,0,key)
    def test_buffers_are_reused_but_returned_bytes_do_not_alias(self):
        first,_=self.scan();second,_=self.scan()
        self.assertEqual(first,bytes([1])*16384)
        self.assertEqual(second,bytes([2])*16384)
        self.assertEqual(self.native.output_ids[0],self.native.output_ids[1])
        self.assertEqual(self.native.key_ids[0],self.native.key_ids[1])
        self.scan(bytes([7])*32)
        self.assertEqual(self.backend._key_bytes,bytes([7])*32)
        self.assertEqual(bytes(self.backend._key),bytes([7])*32)
    def test_full_hash_compatibility_api_retains_order_and_bytes(self):
        hashes,profile=self.backend.scan_addresses(1,2,256,256,0,0,bytes(32))
        self.assertEqual(hashes,[bytes([1])*32]*512)
        self.assertEqual(profile['completed_macs'],268435456)
    def test_failed_partial_or_poisoned_scan_never_returns_old_output(self):
        self.scan()
        for setting,value in (('return_code',2),('incomplete',True),('poisoned',1)):
            setattr(self.native,setting,value)
            with self.subTest(setting=setting),self.assertRaises(RuntimeError):self.scan()
            setattr(self.native,setting,0)
        self.assertIsInstance(self.scan()[0],bytes)
    def test_simultaneous_scan_and_close_refused_before_native_buffer_use(self):
        def reenter():
            with self.assertRaisesRegex(RuntimeError,'Concurrent'):self.scan()
            with self.assertRaisesRegex(RuntimeError,'Concurrent'):self.backend.close()
        self.native.callback=reenter
        self.scan()
        self.assertEqual(self.native.calls,1)
        self.assertEqual(self.native.destroyed,[])
        self.native.callback=None
        self.backend.close();self.backend.close()
        self.assertEqual(self.native.destroyed,[17])
        with self.assertRaisesRegex(RuntimeError,'closed'):self.scan()
    def test_npu_wrapper_keeps_full_gate_hashes_and_exposes_native_profile(self):
        npu=miner.NPU.__new__(miner.NPU)
        npu.batch=self.backend;npu.width=256;npu.candidate_offsets=miner.candidate_offsets(256)
        npu.last_profile=None
        prepared=SimpleNamespace(a_noised=(C.c_uint8*1)(),b_noised=(C.c_uint8*1)(),m=256,n=256,commit_a=bytes(32))
        hashes,macs=npu.scan(prepared,0,0)
        self.assertEqual(hashes,[bytes([1])*32]*512)
        self.assertEqual(macs,268435456)
        self.assertEqual(npu.last_profile['driver_wall_ns'],400)


class TimingAndFreshnessTests(unittest.TestCase):
    def test_timings_are_role_local_copies_with_explicit_native_scope(self):
        first=miner.PhaseTimings();other=miner.PhaseTimings()
        first.add('candidate_filter',250000)
        first.add_native({'wall_ns':1000,'thread_cpu_ns':500,'driver_wall_ns':400})
        saved=first.to_dict();saved['calls']['candidate_filter']=99
        self.assertEqual(first.to_dict()['calls']['candidate_filter'],1)
        self.assertEqual(first.to_dict()['seconds']['candidate_filter'],.00025)
        self.assertEqual(other.to_dict()['native_cumulative_profile']['scans'],0)
        for bad in (-1,True,1.0):
            with self.assertRaises(ValueError):first.add('candidate_filter',bad)

    def test_live_hit_path_rechecks_deadline_and_freshness_before_proof(self):
        # Real scan output is mocked, but execute the live coordinator itself.
        # A found hit must not bypass old freshness/deadline guards.
        for scenario in ('expired_during_scan','expired_during_filter','stale_before_proof'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                clock=[0.0];job=protocol_client().get_latest_job()
                cfg={'runtime_dir':directory,'log_dir':directory,'validation_gate':str(Path(directory)/'gate.json'),
                     'libdir':'not-used','max_runtime_seconds':10,'dev_fee':0,'account':'synthetic','worker':'offline',
                     'pool_kind':'kryptex','pool_host':'not-used'}
                Path(cfg['validation_gate']).write_text(json.dumps({'status':'PASS','pool_submissions':0,'artifact_sha256':{},'device_binding':{}}))
                stack.enter_context(patch.object(miner,'artifacts',return_value={}))
                stack.enter_context(patch.object(miner,'device_binding',return_value={}))
                stack.enter_context(patch.object(miner.time,'monotonic',side_effect=lambda:clock[0]))
                stack.enter_context(patch.object(miner.signal,'signal'))
                stack.enter_context(patch.object(miner,'snapshot',return_value={}))
                stack.enter_context(patch.object(miner,'stop_reason',return_value=None))
                stack.enter_context(patch.object(miner,'logger_for',return_value=Mock()))
                stack.enter_context(patch.object(miner,'atomic_json'))
                prepared=Mock(m=256,n=256,config52=job.config_bytes,commit_a=bytes(32))
                stack.enter_context(patch('engine.proof.liveproof.PreparedJob',return_value=prepared))
                native=Mock(width=256,variant='f32-256',candidate_offsets=miner.candidate_offsets(256),last_profile=None)
                def scan(*unused):
                    if scenario=='expired_during_scan':clock[0]=2
                    return bytes(16384),268435456
                native.scan_packed.side_effect=scan
                stack.enter_context(patch.object(miner,'configured_npu',return_value=native))
                client=Mock();client.status.return_value={};client.get_latest_job.return_value=job
                fresh_calls=[0]
                def current(*unused):
                    fresh_calls[0]+=1
                    if scenario=='stale_before_proof' and fresh_calls[0]==3:
                        clock[0]=2;return False
                    return True
                client.is_current.side_effect=current
                stack.enter_context(patch.object(miner,'make_client',return_value=client))
                original=miner.packed_candidates
                def filter_hits(*args):
                    hits=original(*args)
                    if scenario=='expired_during_filter':clock[0]=2
                    return hits
                stack.enter_context(patch.object(miner,'packed_candidates',side_effect=filter_hits))
                miner.live(cfg,phase_deadline=1)
                prepared.build_proof.assert_not_called()
                client.submit_proof.assert_not_called()
                native.close.assert_called_once()


if __name__=='__main__':unittest.main()
