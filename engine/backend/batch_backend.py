"""Experimental256x256 exact backend adapter; no proof, pool or device policy.

Caller owns prepared array lifetime/immutability, job freshness, thermal checks,
and an independent process watchdog. Never import the host stub in production.
"""
import ctypes as C
import hashlib
from pathlib import Path

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
    def error(self):
        return (self.lib.prl_batch_last_error() or b'unknown batch error').decode(errors='replace')
    def profile(self):
        profile=Profile();rc=self.lib.prl_batch_get_profile(self.handle,C.byref(profile))
        if rc:raise RuntimeError(self.error())
        if profile.abi_version!=1 or profile.struct_bytes!=C.sizeof(profile):
            raise RuntimeError('Batch profile ABI mismatch')
        return {name:getattr(profile,name) for name,_ in profile._fields_}
    def scan_addresses(self,an_address,bn_address,m,n,row_base,col_base,commit_a):
        if not self.handle:raise RuntimeError('Backend closed')
        if not isinstance(commit_a,bytes) or len(commit_a)!=32:
            raise ValueError('commit_a must contain32 bytes')
        if any(not isinstance(v,int) or not 0<=v<=0xffffffff for v in (m,n,row_base,col_base)):
            raise ValueError('Shape/base outside uint32')
        key=(C.c_uint8*32).from_buffer_copy(commit_a);out=(C.c_uint8*16384)();macs=C.c_uint64()
        rc=self.lib.prl_scan_batch(self.handle,an_address,m*4096,bn_address,n*4096,m,n,4096,
            row_base,col_base,key,out,16384,C.byref(macs))
        error=self.error() if rc else None;profile=self.profile()
        if rc:raise RuntimeError(f'prl_scan_batch rc={rc}: {error}; profile={profile}')
        if macs.value!=268435456 or profile['segments']!=32 or profile['completed_macs']!=macs.value:
            raise RuntimeError('Incomplete batch returned success')
        return [bytes(out[i*32:(i+1)*32]) for i in range(512)],profile
    def scan(self,prepared,row_base,col_base):
        return self.scan_addresses(C.cast(prepared.a_noised,C.c_void_p),
            C.cast(prepared.b_noised,C.c_void_p),prepared.m,prepared.n,row_base,col_base,prepared.commit_a)
    def close(self):
        if self.handle:self.lib.prl_batch_destroy(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*unused):self.close()
