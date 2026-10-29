"""Experimental256x256 exact backend adapter; no proof, pool or device policy.

Caller owns prepared array lifetime/immutability, job freshness, thermal checks,
and an independent process watchdog. Never import the host stub in production.
"""
import ctypes as C
import hashlib
from pathlib import Path
import threading

class Profile(C.Structure):
    _fields_=[(name,C.c_uint64) for name in ('abi_version','struct_bytes',
        'segments','executed_macs','completed_macs','wall_ns','thread_cpu_ns',
        'driver_wall_ns','driver_us','driver_cycles')]+[
        ('status',C.c_int32),('poisoned',C.c_uint32)]

def candidate_offsets():
    return [(rb*64+i,cb*64) for rb in range(4) for cb in range(4) for i in range(32)]

class BatchBackend:
    def __init__(self,library,model,format,model_sha256):
        if format not in ('i32','f32'):
            raise ValueError('format must be i32 or f32')
        if len(model_sha256)!=64 or hashlib.sha256(Path(model).read_bytes()).hexdigest()!=model_sha256:
            raise ValueError('Model SHA256 mismatch')
        self.lib=C.CDLL(str(Path(library).resolve()));self.handle=None
        lib=self.lib
        lib.prl_batch_create.argtypes=[C.c_char_p,C.c_uint];lib.prl_batch_create.restype=C.c_void_p
        lib.prl_batch_last_error.argtypes=[];lib.prl_batch_last_error.restype=C.c_char_p
        lib.prl_scan_batch.argtypes=[C.c_void_p,C.c_void_p,C.c_uint64,C.c_void_p,C.c_uint64,
            C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.c_void_p,C.c_void_p,
            C.c_uint64,C.POINTER(C.c_uint64)]
        lib.prl_scan_batch.restype=C.c_int
        lib.prl_batch_get_profile.argtypes=[C.c_void_p,C.POINTER(Profile)]
        lib.prl_batch_get_profile.restype=C.c_int
        lib.prl_batch_destroy.argtypes=[C.c_void_p];lib.prl_batch_destroy.restype=None
        self.handle=lib.prl_batch_create(str(Path(model).resolve()).encode(),format=='f32')
        if not self.handle:raise RuntimeError(self.error())
        # One owned scan at a time. The C runtime also enforces this, but the
        # Python lock protects these reusable buffers while ctypes releases GIL.
        self._scan_lock=threading.Lock()
        self._output=(C.c_uint8*16384)()
        self._macs=C.c_uint64()
        self._key=None;self._key_bytes=None
    def error(self):
        return (self.lib.prl_batch_last_error() or b'unknown batch error').decode(errors='replace')
    def profile(self):
        profile=Profile();rc=self.lib.prl_batch_get_profile(self.handle,C.byref(profile))
        if rc:raise RuntimeError(self.error())
        if profile.abi_version!=1 or profile.struct_bytes!=C.sizeof(profile):
            raise RuntimeError('Batch profile ABI mismatch')
        return {name:getattr(profile,name) for name,_ in profile._fields_}
    def scan_packed_addresses(self,an_address,bn_address,m,n,row_base,col_base,commit_a):
        """Return immutable packed hashes and profile; no per-hash int lists.

        Returned bytes remain valid after the next scan. A failed/partial native
        call never publishes the reusable buffer's old contents.
        """
        if not self.handle:raise RuntimeError('Backend closed')
        if not isinstance(commit_a,bytes) or len(commit_a)!=32:
            raise ValueError('commit_a must contain32 bytes')
        if any(type(v) is not int or not 0<=v<=0xffffffff for v in (m,n,row_base,col_base)):
            raise ValueError('Shape/base outside uint32')
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError('Concurrent scan or close is not allowed')
        try:
            if not self.handle:raise RuntimeError('Backend closed')
            if commit_a!=self._key_bytes:
                self._key=(C.c_uint8*32).from_buffer_copy(commit_a)
                self._key_bytes=commit_a
            self._macs.value=0
            rc=self.lib.prl_scan_batch(self.handle,an_address,m*4096,bn_address,n*4096,m,n,4096,
                row_base,col_base,self._key,self._output,16384,C.byref(self._macs))
            error=self.error() if rc else None;profile=self.profile()
            if rc:raise RuntimeError(f'prl_scan_batch rc={rc}: {error}; profile={profile}')
            if (self._macs.value!=268435456 or profile['segments']!=32 or
                    profile['completed_macs']!=self._macs.value or profile['status']!=0 or profile['poisoned']):
                raise RuntimeError('Incomplete or poisoned batch returned success')
            return bytes(self._output),profile
        finally:self._scan_lock.release()
    def scan_addresses(self,an_address,bn_address,m,n,row_base,col_base,commit_a):
        packed,profile=self.scan_packed_addresses(an_address,bn_address,m,n,row_base,col_base,commit_a)
        return [packed[offset:offset+32] for offset in range(0,len(packed),32)],profile
    def scan_packed(self,prepared,row_base,col_base):
        return self.scan_packed_addresses(C.cast(prepared.a_noised,C.c_void_p),
            C.cast(prepared.b_noised,C.c_void_p),prepared.m,prepared.n,row_base,col_base,prepared.commit_a)
    def scan(self,prepared,row_base,col_base):
        return self.scan_addresses(C.cast(prepared.a_noised,C.c_void_p),
            C.cast(prepared.b_noised,C.c_void_p),prepared.m,prepared.n,row_base,col_base,prepared.commit_a)
    def close(self):
        if not self.handle:return
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError('Concurrent scan or close is not allowed')
        try:
            if self.handle:self.lib.prl_batch_destroy(self.handle);self.handle=None
        finally:self._scan_lock.release()
    def __enter__(self):return self
    def __exit__(self,*unused):self.close()
