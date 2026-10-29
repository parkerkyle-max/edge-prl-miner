"""Offline protocol tests. All proof bytes and accepts below use a fake socket."""
import base64
from dataclasses import FrozenInstanceError
import gzip
import json
from pathlib import Path
import sys
import socket
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from engine.pool.kryptex import (MAX256,MiningConfig,ProtocolError,StaleJob,StratumClient,
                          compact_to_target,target_to_compact,parse_job,
                          pattern_to_bytes,pattern_from_bytes)

HEADER='00000020c2a757010728eb134fcf177bc7750882fdca2eedbbb967ec5c6375617fa88600ddfa9fd9c3478322c8df6846e621e51d106d7b64fc6a3f8854c8ff00e56ed7b69ba3e16a9f9a5717'
TARGET='00000000000007ffffffffffffffffffffffffffffffffffffffffffffffffff'

def notify(job_id='unit-test-job'):
    return {'method':'mining.notify','params':{'job_id':job_id,'header':HEADER,'target':TARGET,'height':134843,'cert_version':3}}

class FakeSocket:
    def __init__(self):self.sent=[]
    def sendall(self,wire):self.sent.append(json.loads(wire))

def client(verifier=lambda job,proof:True):
    c=StratumClient('krxTEST','offline-unit-only',verify_proof=verifier)
    c._sock=FakeSocket();c._session='offline-session'
    with c._lock:
        auth=c._request('authorize','mining.authorize',{})
    c._handle_message({'id':auth,'result':True,'error':None,'type':'v2'})
    c._handle_message(notify())
    return c

class TargetsAndConfig(unittest.TestCase):
    def test_known_bitcoin_target_and_rounding(self):
        self.assertEqual(compact_to_target(0x1d00ffff),65535<<208)
        self.assertEqual(target_to_compact(65535<<208),0x1d00ffff)
        for value in (1,127,128,0x123456,0x80ffff,MAX256,int(TARGET,16)):
            rounded=compact_to_target(target_to_compact(value))
            self.assertLessEqual(rounded,value)
            self.assertEqual(target_to_compact(rounded),target_to_compact(value))
        for n in (0x1d800000,0x23000001,0):
            with self.assertRaises(ProtocolError):compact_to_target(n)

    def test_config_known_official_layout(self):
        cfg=MiningConfig()
        self.assertEqual(cfg.to_bytes(),bytes.fromhex('00100000800000001f0100000000003f00000000')+bytes(32))
        self.assertEqual(len(cfg.to_bytes()),52)
        self.assertEqual(cfg.adjustment_factor,2*64*4096)
        for points in ((0,8,64,72),(0,1,8,9),tuple(range(64))):
            self.assertEqual(pattern_from_bytes(pattern_to_bytes(points)),points)
        with self.assertRaises(ProtocolError):pattern_to_bytes((0,1,3))
        with self.assertRaises(ProtocolError):MiningConfig(rank=64)
        with self.assertRaises(ProtocolError):MiningConfig(rows=[0,32])

    def test_snapshot_exact_and_frozen(self):
        j=parse_job(notify()['params'],MiningConfig(),'s',1)
        self.assertEqual(len(j.header),76)
        self.assertEqual(j.scaled_target,compact_to_target(j.nbits_override)*524288)
        self.assertEqual(int.from_bytes(j.scaled_target_le32,'little'),j.scaled_target)
        self.assertLessEqual(j.scaled_target,int(TARGET,16)*524288)
        with self.assertRaises(FrozenInstanceError):j.generation=3
        for change in ({'header':HEADER+'00'},{'cert_version':2},{'cert_version':True},{'target':'ff'*32},{'target':'zz'},{'target':'00'}):
            with self.assertRaises(ProtocolError):parse_job({**notify()['params'],**change},MiningConfig(),'s',1)

class ProtocolLifecycle(unittest.TestCase):
    def test_idle_reader_does_not_hold_submit_io_lock(self):
        # Anonymous local socketpair and fake TLS wrapper, never real pool I/O.
        left,right=socket.socketpair()
        class FakeTLS:
            def __getattr__(self,name):return getattr(left,name)
            def pending(self):return 0
            def version(self):return 'OFFLINE-FAKE-TLS'
            def getpeercert(self,binary_form=False):return b'offline-test-cert'
        class FakeContext:
            def wrap_socket(self,raw,server_hostname):return FakeTLS()
        c=StratumClient('krxTEST','offline-unit-only',verify_proof=lambda j,p:True)
        def server():
            buffer=b''
            while buffer.count(b'\n')<2:buffer+=right.recv(65536)
            sent=[json.loads(x) for x in buffer.splitlines()]
            auth=next(x for x in sent if x['method']=='mining.authorize')
            reply={'id':auth['id'],'result':True,'error':None,'type':'v2'}
            right.sendall((json.dumps(reply)+'\n'+json.dumps(notify())+'\n').encode())
        with patch('engine.pool.kryptex._bounded_connect',return_value=left),patch('engine.pool.kryptex.ssl.create_default_context',return_value=FakeContext()):
            thread=threading.Thread(target=server,daemon=True);thread.start();c.start()
            try:
                deadline=time.monotonic()+2
                job=None
                while job is None and time.monotonic()<deadline:job=c.wait_for_job(timeout=.1)
                self.assertIsNotNone(job)
                time.sleep(.05) # Reader should now wait for incoming bytes.
                begun=time.monotonic()
                rid=c.submit_proof(job,b'offline-only-proof')
                self.assertLess(time.monotonic()-begun,.5)
                self.assertIsInstance(rid,int)
                self.assertEqual(c.stats['accepted'],0)
            finally:c.stop();right.close();thread.join(1)

    def test_auth_is_not_share_and_pending_id_only(self):
        c=client()
        self.assertEqual(c.stats['accepted'],0)
        c._handle_message({'id':999,'result':True,'error':None})
        self.assertEqual(c.stats['accepted'],0)
        rid=c.submit_proof(c.get_latest_job(),b'offline-fake-proof')
        c._handle_message({'id':str(rid),'result':True,'error':None})
        self.assertEqual(c.stats['accepted'],0)
        c._handle_message({'id':rid,'result':True,'error':None})
        c._handle_message({'id':rid,'result':True,'error':None})
        self.assertEqual(c.stats['accepted'],1)

    def test_gzip_is_of_binary_not_base64(self):
        c=client();raw=b'\x00\x01OFFLINE\xff'*20
        c.submit_proof(c.get_latest_job(),raw)
        sent=c._sock.sent[-1]
        self.assertEqual(sent['method'],'mining.submit')
        self.assertEqual(gzip.decompress(base64.b64decode(sent['params']['plain_proof'])),raw)
        self.assertEqual(sent['params']['worker'],'krxTEST/offline-unit-only')

    def test_requires_authorization_and_confirmed_v2(self):
        c=StratumClient('krxTEST','offline-unit-only')
        c._sock=FakeSocket();c._session='offline'
        c._handle_message(notify())
        self.assertIsNone(c.get_latest_job())
        with c._lock:rid=c._request('authorize','mining.authorize',{})
        with self.assertRaises(ProtocolError):c._handle_message({'id':rid,'result':True,'error':None})
        self.assertIsNone(c.get_latest_job());self.assertEqual(c.stats['accepted'],0)

    def test_reject_invalid_proof_locally_and_no_verifier(self):
        for verifier in (None,lambda job,proof:False,lambda job,proof:{'valid':True}):
            c=client(verifier)
            before=len(c._sock.sent)
            with self.assertRaises((ProtocolError,RuntimeError)):c.submit_proof(c.get_latest_job(),b'bad-proof')
            self.assertEqual(len(c._sock.sent),before)

    def test_target_change_and_job_change_invalidate_snapshot(self):
        c=client();old=c.get_latest_job()
        c._handle_message({'method':'mining.set_difficulty','params':[2097152]})
        self.assertIsNone(c.get_latest_job())
        with self.assertRaises(StaleJob):c.submit_proof(old,b'proof')
        c._handle_message(notify('new'))
        with self.assertRaises(StaleJob):c.submit_proof(old,b'proof')
        self.assertEqual(c.stats['submitted'],0)

    def test_job_changes_during_verification(self):
        c=client()
        def verify(job,proof):c._handle_message(notify('replacement'));return True
        c.verify_proof=verify
        with self.assertRaises(StaleJob):c.submit_proof(c.get_latest_job(),b'proof')
        self.assertEqual(c.stats['submitted'],0)

    def test_bad_new_job_never_reuses_old_target(self):
        c=client()
        with self.assertRaises(ProtocolError):c._handle_message({'method':'mining.notify','params':{'job_id':'broken'}})
        self.assertIsNone(c.get_latest_job())

    def test_duplicate_reject_and_error_counting(self):
        c=client();j=c.get_latest_job();rid=c.submit_proof(j,b'proof')
        with self.assertRaises(ProtocolError):c.submit_proof(j,b'proof')
        c._handle_message({'id':rid,'result':False,'error':[23,'unit-test rejection',None]})
        self.assertEqual(c.stats['rejected'],1)
        self.assertEqual(c.stats['accepted'],0)

if __name__=='__main__':unittest.main(verbosity=2)
