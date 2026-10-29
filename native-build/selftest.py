"""Explicit offline native proof cache conformance; no NPU, pool, or device I/O.

Uses about 0.5 GiB RAM and full cert3 matrices; not part of lightweight CI.
Compares 8 cached/stateless proofs, frozen vectors, invalid inputs and lifetimes.
Never interpret easy-target acceptance or proof-build timing as mining revenue.
"""
from __future__ import annotations
import argparse, ctypes as C, datetime, gc, hashlib, json, os, platform
import resource, sys, time, traceback
from pathlib import Path

HERE=Path(__file__).resolve().parent
M=N=16384
K=4096
RANK=128
MATRIX_BYTES=M*K
PROOF_CAP=2*1024*1024
CONFIG52=bytes.fromhex("00100000800000001f0100000000003f00000000")+bytes(32)
LOCATIONS=((0,0),(31,64),(64,192),(95,256),(223,0),(256,16128),(16320,16256),(16351,16320))

class PreparedJob:
    def __init__(self, libdir, header76: bytes, seed32: bytes | None = None,
                 m: int = M, n: int = N, cert_version: int = 3, retain_originals_for_testing: bool = False):
        if (m, n, cert_version) != (M, N, 3):
            raise ValueError("only M=N16384, K4096, rank128 and certificate3 are supported")
        if not isinstance(header76, bytes) or len(header76) != 76:
            raise ValueError("header must be exactly 76 bytes")
        if seed32 is None:
            seed32 = os.urandom(32)
        if not isinstance(seed32, bytes) or len(seed32) != 32:
            raise ValueError("seed must be exactly 32 bytes")
        for name in ("OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "RAYON_NUM_THREADS"):
            os.environ[name] = "1"
        self.closed = False
        self._cache_handle = 0
        self.m, self.n, self.k, self.rank, self.cert_version = m, n, K, RANK, cert_version
        self.header76 = header76
        self.seed_commitment = hashlib.sha256(seed32).hexdigest()
        self._header = C.create_string_buffer(header76, 76)
        self._prep = C.CDLL(str(Path(libdir) / "libpearl_prep.so"))
        self._proof = C.CDLL(str(Path(libdir) / "libpearl_edge_proof.so"))
        pointer, size = C.c_void_p, C.c_size_t
        self._proof.prl_config_and_key.argtypes = [pointer, size, pointer, pointer, pointer, size]
        self._proof.prl_config_and_key.restype = C.c_ssize_t
        self._proof.prl_build_proof.argtypes = [pointer, size, pointer, size, pointer, size, size, pointer, size, pointer, size]
        self._proof.prl_build_proof.restype = C.c_ssize_t
        self._proof.prl_verify_proof.argtypes = [pointer, size, pointer, size, C.c_uint32, C.c_uint32,
                                               pointer, pointer, pointer, pointer, pointer, size]
        self._proof.prl_verify_proof.restype = C.c_ssize_t
        self._proof.prl_proof_cache_open.argtypes = [pointer,size,pointer,size,pointer,pointer,pointer,pointer,size]
        self._proof.prl_proof_cache_open.restype = C.c_ssize_t
        self._proof.prl_build_proof_cached.argtypes = [size,size,size,pointer,size,pointer,size]
        self._proof.prl_build_proof_cached.restype = C.c_ssize_t
        self._proof.prl_proof_cache_close.argtypes = [size,pointer,size]
        self._proof.prl_proof_cache_close.restype = C.c_ssize_t
        self._prep.prep_from_ab.argtypes = [pointer, pointer, pointer, C.c_int64, C.c_int64, C.c_int64, C.c_int,
                                          pointer, pointer, pointer, pointer, pointer, pointer, pointer, pointer, C.c_int, C.c_int]
        self._prep.prep_from_ab.restype = C.c_int
        self._key = (C.c_uint8 * 32)()
        configuration = (C.c_uint8 * 52)()
        error = C.create_string_buffer(2048)
        result = self._proof.prl_config_and_key(self._header, 76, configuration, self._key, error, len(error))
        if result != 0:
            raise RuntimeError(error.value.decode(errors="replace"))
        self.config52, self.job_key = bytes(configuration), bytes(self._key)
        if self.config52 != CONFIG52:
            raise RuntimeError("official configuration serialization differs from expected config52")
        started = time.monotonic()
        # Legal sparse originals: all-zero matrices with unique bounded seed stamps.
        # The stamp is inside both opened first strips in the (0,0) correctness test.
        self.original_a = (C.c_int8 * (M*K))()
        self.original_bt = (C.c_int8 * (N*K))()
        seed_a = hashlib.sha256(b"PRL-edge-original-A-v1" + seed32).digest()
        seed_b = hashlib.sha256(b"PRL-edge-original-Bt-v1" + seed32).digest()
        for i in range(32):
            self.original_a[i] = (seed_a[i] & 127) - 64
            self.original_bt[i] = (seed_b[i] & 127) - 64
        self.a_noised = (C.c_int8 * (M*K))()
        self.b_noised = (C.c_int8 * (K*N))()
        eal, ebr = (C.c_int8 * (M*RANK))(), (C.c_int8 * (N*RANK))()
        roots = [(C.c_uint8 * 32)() for _ in range(4)]
        result = self._prep.prep_from_ab(self.original_a, self.original_bt, self._key, M, N, K, RANK,
                                        self.a_noised, self.b_noised, eal, ebr, *roots, 1, cert_version)
        if result != 0:
            self.close()
            raise RuntimeError(f"prep_from_ab failed: {result}")
        self.root_a, self.root_b, self.commit_a, self.commit_b = (bytes(value) for value in roots)
        cache_start = time.monotonic()
        cached_roots = [(C.c_uint8 * 32)() for _ in range(2)]
        handle = self._proof.prl_proof_cache_open(self.original_a, M*K, self.original_bt, N*K,
                                               self._key, *cached_roots, error, len(error))
        if handle <= 0:
            self.close()
            raise RuntimeError("Proof cache creation failed: " + error.value.decode(errors="replace"))
        self._cache_handle = handle
        if [bytes(x) for x in cached_roots] != [self.root_a, self.root_b]:
            self.close()
            raise RuntimeError("Cached Merkle roots differ from official prepared job")
        self.proof_cache_create_seconds = time.monotonic() - cache_start
        # MerkleTree owns immutable copies. Avoid retaining a second128MiB.
        # Tests may retain caller buffers solely to compare the legacy API.
        if not retain_originals_for_testing:
            self.original_a = None
            self.original_bt = None
        self.preparation_seconds = time.monotonic() - started
        self.matrix_bytes_resident = 2 * (M+N) * K
        # eal and ebr are released here; callers need only the four persistent matrices.

    def _open(self):
        if self.closed:
            raise RuntimeError("PreparedJob is closed")

    def build_proof(self, row: int, col_base: int) -> bytes:
        self._open()
        if not (isinstance(row, int) and isinstance(col_base, int) and 0 <= row < M-32
                and row % 64 < 32 and 0 <= col_base <= N-64 and col_base % 64 == 0):
            raise ValueError("invalid [row,row+32] /64-column band")
        output, error = C.create_string_buffer(PROOF_CAP), C.create_string_buffer(2048)
        length = self._proof.prl_build_proof_cached(self._cache_handle, row, col_base,
                                                  output, len(output), error, len(error))
        if length <= 0:
            raise RuntimeError(error.value.decode(errors="replace"))
        return output.raw[:length]

    def verify(self, proof_bytes: bytes, nbits_override: int, expected_jackpot: bytes | None = None) -> dict:
        self._open()
        if not isinstance(proof_bytes, bytes) or not 0 < len(proof_bytes) <= PROOF_CAP:
            raise ValueError("proof size/type invalid")
        if not isinstance(nbits_override, int) or not 0 < nbits_override <= 0xffffffff:
            raise ValueError("explicit uint32 compact target required")
        if expected_jackpot is not None and (not isinstance(expected_jackpot, bytes) or len(expected_jackpot) != 32):
            raise ValueError("expected jackpot must be32 bytes")
        proof = C.create_string_buffer(proof_bytes, len(proof_bytes))
        expected = C.create_string_buffer(expected_jackpot, 32) if expected_jackpot is not None else None
        jackpot, transcript = (C.c_uint8 * 32)(), (C.c_uint8 * 64)()
        oracle_ready, error = C.c_uint32(0), C.create_string_buffer(2048)
        result = self._proof.prl_verify_proof(self._header, 76, proof, len(proof_bytes), self.cert_version, nbits_override,
                                            expected, jackpot, transcript, C.byref(oracle_ready), error, len(error))
        return {"valid": result == 0, "error": error.value.decode(errors="replace"), "return_code": result,
                "oracle_available": bool(oracle_ready.value),
                "jackpot_hex": bytes(jackpot).hex() if oracle_ready.value else None,
                "transcript_hex": bytes(transcript).hex() if oracle_ready.value else None,
                "nbits_override": nbits_override, "cert_version": self.cert_version,
                "official_commit": "0a17bb345a838147a4c14fc618de1839cbc16c1e"}

    def close(self):
        # The coordinator must wait for its NPU scan to finish before calling close.
        if getattr(self, "_cache_handle", 0):
            error = C.create_string_buffer(2048)
            handle = self._cache_handle
            rc = self._proof.prl_proof_cache_close(handle, error, len(error))
            if rc != 0:
                raise RuntimeError("Proof cache close failed: " + error.value.decode(errors="replace"))
            self._cache_handle = 0
        self.closed = True
        for name in ("original_a", "original_bt", "a_noised", "b_noised"):
            setattr(self, name, None)

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *_):
        self.close()


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def rss():
    rows={}
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith(('VmRSS:','VmHWM:','VmSize:')):
            key,value,*_=line.split();rows[key.rstrip(':')+'_KiB']=int(value)
    rows['process_maxrss_KiB']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rows
def bind_legacy(lib):
    p=C.c_void_p;s=C.c_size_t
    lib.prl_build_proof.argtypes=[p,s,p,s,p,s,s,p,s,p,s];lib.prl_build_proof.restype=C.c_ssize_t
    lib.prl_verify_proof.argtypes=[p,s,p,s,C.c_uint32,C.c_uint32,p,p,p,p,p,s];lib.prl_verify_proof.restype=C.c_ssize_t
def verify(lib,header,proof,nbits,expected=None):
    h=C.create_string_buffer(header,76);blob=C.create_string_buffer(proof,len(proof));err=C.create_string_buffer(2048)
    exp=C.create_string_buffer(expected,32) if expected is not None else None
    jackpot=(C.c_uint8*32)();transcript=(C.c_uint8*64)();ready=C.c_uint32()
    rc=lib.prl_verify_proof(h,76,blob,len(proof),3,nbits,exp,jackpot,transcript,C.byref(ready),err,len(err))
    return {'valid':rc==0,'return_code':rc,'oracle_available':bool(ready.value),'error':err.value.decode(errors='replace'),
            'jackpot_hex':bytes(jackpot).hex() if ready.value else None,'transcript_hex':bytes(transcript).hex() if ready.value else None}
def legacy_build(lib,p,row,col):
    out=C.create_string_buffer(PROOF_CAP);err=C.create_string_buffer(2048)
    length=lib.prl_build_proof(p.original_a,MATRIX_BYTES,p.original_bt,MATRIX_BYTES,p._key,row,col,out,len(out),err,len(err))
    if length<=0:raise RuntimeError('Legacy build failed: '+err.value.decode(errors='replace'))
    return out.raw[:length]
def native_build(lib,handle,row=0,col=0,cap=PROOF_CAP,null_output=False):
    out=C.create_string_buffer(PROOF_CAP);err=C.create_string_buffer(2048)
    C.memset(out,0xA5,PROOF_CAP)
    rc=lib.prl_build_proof_cached(handle,row,col,None if null_output else out,cap,err,len(err))
    return {'rc':rc,'error':err.value.decode(errors='replace'),'output_unchanged_on_failure':rc>=0 or out.raw==b'\xA5'*PROOF_CAP}
def native_close(lib,handle):
    err=C.create_string_buffer(2048);rc=lib.prl_proof_cache_close(handle,err,len(err))
    return {'rc':rc,'error':err.value.decode(errors='replace')}
def expect_rejected(record,label,checks):
    assert record['rc']<0,(label,record)
    assert record.get('output_unchanged_on_failure',True),(label,'output changed after rejection')
    checks.append({'check':label,**record})

def run(args,out):
    if sys.platform!='linux' or platform.machine() not in ('x86_64','amd64','aarch64','arm64'):
        raise RuntimeError('Run on Linux with a matching x86_64 or aarch64 native library')
    for key in ('OMP_NUM_THREADS','OMP_THREAD_LIMIT','RAYON_NUM_THREADS'):os.environ[key]='1'
    fixture=json.loads(args.fixture.read_text());header=bytes.fromhex(fixture['source_job']['header_hex']);seed=bytes.fromhex(fixture['seed32_hex'])
    assert len(header)==76 and len(seed)==32
    # Offline only: maximum adjusted target for fixed dense cert3 factor 2**19.
    nbits=0x1e1fffff
    legacy=C.CDLL(str(args.legacy_library));bind_legacy(legacy)
    result={'scope':'LOCAL_NATIVE_CACHE_PARITY_AND_LIFETIME_NO_NPU_NO_POOL','status':'IN_PROGRESS','started_at_utc':utc(),
            'sources':{str(p):sha(p) for p in (HERE/'selftest.py',HERE/'proof/src/cache_api.rs',HERE/'proof/src/lib.rs',args.legacy_library,args.libdir/'libpearl_edge_proof.so',args.libdir/'libpearl_prep.so',args.fixture)},
            'checks':[],'cases':[],'sequential_jobs':[],'rss_initial':rss(),'limits':{'proof_comparisons':8,'locations':[list(x) for x in LOCATIONS],'sequential_additional_jobs':2}}
    checks=result['checks'];p=None;old_handle=None
    try:
        tick=time.perf_counter();p=PreparedJob(args.libdir,header,seed32=seed,retain_originals_for_testing=True)
        result['prepare_total_seconds']=time.perf_counter()-tick;result['cache_create_seconds']=p.proof_cache_create_seconds;result['rss_after_retained_prepare']=rss()
        assert p.original_a is not None and p.original_bt is not None
        old_handle=int(p._cache_handle);assert old_handle>0
        for name in ('job_key','root_a','root_b','commit_a','commit_b'):
            assert getattr(p,name).hex()==fixture[name+'_hex'],name
        checks.append({'check':'original_frozen_fixture_roots_keys_and_commits_match'})
        new=p._proof;bind_legacy(new)
        baseline_proof=None;baseline_jackpot=None
        for row,col in LOCATIONS:
            tick=time.perf_counter();old=legacy_build(legacy,p,row,col);legacy_seconds=time.perf_counter()-tick
            tick=time.perf_counter();cached=p.build_proof(row,col);cached_seconds=time.perf_counter()-tick
            assert old==cached,(row,col,'cached proof differs from frozen stateless builder')
            tick=time.perf_counter();old_v=verify(legacy,header,old,nbits);assert old_v['valid'] and old_v['oracle_available'],old_v
            jackpot=bytes.fromhex(old_v['jackpot_hex']);new_v=p.verify(cached,nbits,expected_jackpot=jackpot)
            for frozen in fixture['tiles']:
                if frozen['row_pair'][0]==row and frozen['col_base']==col:
                    assert old_v['jackpot_hex']==frozen['jackpot_hex'],'frozen jackpot mismatch'
                    assert old_v['transcript_hex']==frozen['transcript_hex'],'frozen transcript mismatch'
            assert new_v['valid'] and new_v['jackpot_hex']==old_v['jackpot_hex'] and new_v['transcript_hex']==old_v['transcript_hex'],new_v
            official_seconds=time.perf_counter()-tick
            name=f'proof-r{row}-c{col}.bin';(out/name).write_bytes(cached)
            item={'row':row,'col':col,'bytes':len(cached),'proof_file':name,'sha256':hashlib.sha256(cached).hexdigest(),
                  'byte_identical':True,'legacy_build_seconds':legacy_seconds,'cached_build_seconds':cached_seconds,
                  'two_verifications_seconds':official_seconds,'legacy_verification':old_v,'cache_verification':new_v}
            result['cases'].append(item);print(json.dumps({'event':'proof_parity','row':row,'col':col,'legacy_seconds':legacy_seconds,'cached_seconds':cached_seconds}),flush=True)
            if row==0 and col==0:baseline_proof=cached;baseline_jackpot=jackpot
        assert baseline_proof is not None
        bad_header=bytearray(header);bad_header[4]^=1
        bad_expected=bytes([baseline_jackpot[0]^1])+baseline_jackpot[1:]
        negatives=[('wrong_header',bytes(bad_header),baseline_proof,baseline_jackpot),
                   ('wrong_expected_jackpot',header,baseline_proof,bad_expected),
                   ('trailing_proof_byte',header,baseline_proof+b'\x00',baseline_jackpot)]
        for label,h,proof,expected in negatives:
            old_v=verify(legacy,h,proof,nbits,expected);new_v=verify(new,h,proof,nbits,expected)
            assert not old_v['valid'] and not new_v['valid'],(label,old_v,new_v)
            checks.append({'check':label+'_rejected_by_both_verifiers','old':old_v,'new':new_v})
        for label,h,row,col,cap,null in [('zero_handle',0,0,0,PROOF_CAP,False),('unknown_handle',old_handle+100,0,0,PROOF_CAP,False),
             ('row_pair_gap',old_handle,32,0,PROOF_CAP,False),('row_past_end',old_handle,16352,0,PROOF_CAP,False),
             ('column_unaligned',old_handle,0,1,PROOF_CAP,False),('column_past_end',old_handle,0,16384,PROOF_CAP,False),
             ('output_too_small',old_handle,0,0,512*1024-1,False),('output_too_large',old_handle,0,0,PROOF_CAP+1,False),
             ('null_output',old_handle,0,0,PROOF_CAP,True)]:
            expect_rejected(native_build(new,h,row,col,cap,null),label,checks)
        err=C.create_string_buffer(2048);roots=[(C.c_uint8*32)() for _ in range(2)]
        for label,alen,blen in [('second_concurrent_cache',MATRIX_BYTES,MATRIX_BYTES),('bad_A_length',MATRIX_BYTES-1,MATRIX_BYTES),('bad_B_length',MATRIX_BYTES,MATRIX_BYTES+1)]:
            rc=new.prl_proof_cache_open(p.original_a,alen,p.original_bt,blen,p._key,*roots,err,len(err))
            expect_rejected({'rc':rc,'error':err.value.decode(errors='replace')},label,checks)
        expect_rejected(native_close(new,0),'zero_close',checks)
        expect_rejected(native_close(new,old_handle+100),'unknown_close',checks)
        assert p.build_proof(0,0)==baseline_proof,'invalid operations harmed active cache'
        # Mutate the foreign buffers after opening: the immutable native cache
        # must own snapshots, and its proof remains valid for the old roots.
        a_before=int(p.original_a[0]);b_before=int(p.original_bt[0])
        p.original_a[0]=-63 if a_before!=-63 else -62;p.original_bt[0]=62 if b_before!=62 else 61
        assert p.build_proof(0,0)==baseline_proof,'native cache borrowed mutable caller buffers'
        assert verify(legacy,header,p.build_proof(0,0),nbits,baseline_jackpot)['valid']
        p.original_a[0]=a_before;p.original_bt[0]=b_before
        checks.append({'check':'immutable_owned_snapshot_survives_foreign_input_mutation'})
        # Additional hot calls exclude verification and cache creation.
        hot=[]
        for _ in range(8):
            tick=time.perf_counter();proof=p.build_proof(0,0);hot.append(time.perf_counter()-tick);assert proof==baseline_proof
        result['hot_cached_seconds']=hot;result['rss_before_close']=rss()
        p.close();assert p._cache_handle==0 and p.closed
        p.close();checks.append({'check':'wrapper_close_is_idempotent'})
        for fn in (lambda:p.build_proof(0,0),lambda:p.verify(baseline_proof,nbits)):
            try:fn()
            except RuntimeError:pass
            else:raise AssertionError('closed wrapper accepted an operation')
        expect_rejected(native_build(new,old_handle),'native_stale_after_close',checks)
        expect_rejected(native_close(new,old_handle),'native_double_close_rejected',checks)
        p=None;gc.collect();result['rss_after_retained_close']=rss()
        previous=old_handle
        for index in (1,2):
            h=bytearray(header);h[68]^=index;s=hashlib.sha256(seed+bytes([index])).digest()
            tick=time.perf_counter();p=PreparedJob(args.libdir,bytes(h),seed32=s)
            assert p.original_a is None and p.original_bt is None,'default wrapper retained original Python matrices'
            handle=int(p._cache_handle);assert handle>previous and handle!=old_handle
            proof=p.build_proof(0,0);v=verify(legacy,bytes(h),proof,nbits);assert v['valid'] and v['oracle_available'],v
            expect_rejected(native_build(p._proof,previous),'old_handle_rejected_with_new_job_'+str(index),checks)
            result['sequential_jobs'].append({'index':index,'handle':handle,'previous_handle':previous,'header_sha256':hashlib.sha256(h).hexdigest(),
                'seed_sha256':hashlib.sha256(s).hexdigest(),'job_key_hex':p.job_key.hex(),'commit_a_hex':p.commit_a.hex(),
                'default_originals_released':True,'prepare_plus_proof_seconds':time.perf_counter()-tick,'cache_create_seconds':p.proof_cache_create_seconds,
                'rss_after_default_prepare':rss(),'proof_sha256':hashlib.sha256(proof).hexdigest(),'legacy_verification':v})
            previous=handle;p.close();p=None;gc.collect()
        assert len({x['job_key_hex'] for x in result['sequential_jobs']})==2
        assert len({x['commit_a_hex'] for x in result['sequential_jobs']})==2
        result.update(status='PASS',finished_at_utc=utc(),rss_final=rss(),
                      legacy_build_total_seconds=sum(x['legacy_build_seconds'] for x in result['cases']),
                      cached_build_total_seconds=sum(x['cached_build_seconds'] for x in result['cases']))
        result['proof_build_only_speedup']=result['legacy_build_total_seconds']/result['cached_build_total_seconds']
        result['timing_limit']='Local native proof-build timing only; excludes mining, NPU, live transport, cache creation amortization and XMR interference. Not a Pi speedup claim.'
        return result
    finally:
        if p is not None:p.close()

def main():
    from build import validate_output
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--libdir',type=Path,required=True)
    ap.add_argument('--legacy-library',type=Path,help='Optional independently built older proof library; default stateless API in --libdir')
    ap.add_argument('--fixture',type=Path,default=HERE.parent/'engine/proof/host-reference.json')
    ap.add_argument('--output',type=Path,required=True,help='New directory under repository local/')
    args=ap.parse_args()
    if sys.flags.optimize:
        raise RuntimeError('Do not use python -O: this conformance harness requires assertions')
    if args.legacy_library is None:
        args.legacy_library=args.libdir/'libpearl_edge_proof.so'
    out=validate_output(args.output)
    out.mkdir(parents=True,exist_ok=False)
    try:result=run(args,out)
    except BaseException as exc:
        result={'status':'FAILED','scope':'LOCAL_NATIVE_CACHE_PARITY_AND_LIFETIME_NO_NPU_NO_POOL',
                'error':repr(exc),'traceback':traceback.format_exc()}
    result['test_source_sha256']=sha(Path(__file__))
    result['separate_legacy_library_requested']=args.legacy_library.resolve()!=(args.libdir/'libpearl_edge_proof.so').resolve()
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    assert json.loads((out/'receipt.json').read_text())==result
    print(json.dumps({'status':result['status'],'output':str(out),'receipt_sha256':sha(out/'receipt.json'),
                     'error':result.get('error'),'proof_build_only_speedup':result.get('proof_build_only_speedup')},ensure_ascii=True))
    return 0 if result['status']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
