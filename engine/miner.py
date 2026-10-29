"""Actual NPU Pearl scan loop: real header/noise, locally verified proofs only."""
from __future__ import annotations
import argparse,ctypes as C,datetime,gc,hashlib,importlib.util,json,logging,logging.handlers,math,os,signal,socket,sys,time
from pathlib import Path
if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.runtime_guard import atomic_json
from engine.configuration import validate_config,preflight,snapshot,stop_reason,NATIVE_LIBRARIES
from engine.devfee import client_config,disclosure
from engine.pool import MiningConfig,StratumClient
from engine.pool.suprnova import SuprnovaClient
from engine.pool.kryptex import MAX256,StaleJob,target_to_compact,compact_to_target

BASE=Path(__file__).resolve().parent
MODEL_VARIANTS={
    'f32-256':{'width':256,'format':'f32','sha256':'e0ad42a6e40b4e675d95bce537c9ca45bd1bb984d23e45b4bbf5d3fd16537e93'},
}

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def model_spec(cfg):
    variant=cfg.get('npu_variant')
    if variant not in MODEL_VARIANTS:raise RuntimeError('Unknown NPU variant')
    spec=MODEL_VARIANTS[variant]
    if variant!='i32-64' and any(key not in cfg for key in ('npu_width','model_sha256')):
        raise RuntimeError('Optimized model requires explicit width and SHA256')
    width=cfg.get('npu_width',64)
    if type(width) is not int or width!=spec['width']:raise RuntimeError('Model/width mismatch')
    if cfg.get('model_sha256',spec['sha256'])!=spec['sha256'] or sha(cfg['model'])!=spec['sha256']:
        raise RuntimeError('Model hash mismatch')
    return dict(spec,variant=variant)


def candidate_offsets(width):
    if type(width) is not int or width not in (64,128,256):raise ValueError('Unsupported scan width')
    subtiles=width//64
    return tuple((rb*64+i,cb*64) for rb in range(subtiles) for cb in range(subtiles) for i in range(32))


def packed_candidates(packed,target):
    """Exact inclusive comparison; allocate hashes only for MSB-filter survivors.

    A hash's final byte is its most significant byte in the protocol's little-
    endian representation. Rejecting a larger byte is exact, not an approximate
    target. Surviving values still receive the complete 256-bit comparison.
    """
    if not isinstance(packed,bytes) or not packed or len(packed)%32:
        raise ValueError('Expected nonempty immutable packed 32-byte hashes')
    if type(target) is not int or not 0<=target<=MAX256:
        raise ValueError('Expected unsigned 256-bit target')
    high=target>>248
    hits=[]
    for last in range(31,len(packed),32):
        if packed[last]>high:continue
        jackpot=packed[last-31:last+1]
        if int.from_bytes(jackpot,'little')<=target:
            hits.append((last//32,jackpot))
    return hits


class PhaseTimings:
    """Cumulative process-local timings; nested driver/verification fields overlap."""
    NAMES=('npu_initialize','job_prepare','native_scan_and_adapter','candidate_filter',
           'proof_build','proof_verify','submit_including_verify')
    def __init__(self):
        self.nanoseconds={name:0 for name in self.NAMES}
        self.calls={name:0 for name in self.NAMES}
        self.native={'scans':0,'wall_ns':0,'thread_cpu_ns':0,'driver_wall_ns':0}
    def add(self,name,elapsed_ns):
        if name not in self.nanoseconds or type(elapsed_ns) is not int or elapsed_ns<0:
            raise ValueError('Invalid phase timing')
        self.nanoseconds[name]+=elapsed_ns;self.calls[name]+=1
    def add_native(self,profile):
        if profile is None:return
        for key in ('wall_ns','thread_cpu_ns','driver_wall_ns'):
            if type(profile.get(key)) is not int or profile[key]<0:
                raise RuntimeError('Invalid native timing profile')
        self.native['scans']+=1
        for key in ('wall_ns','thread_cpu_ns','driver_wall_ns'):self.native[key]+=profile[key]
    def to_dict(self):
        return {'scope':'Current process/role only; timers overlap as documented; not NPU-only hashrate or revenue',
                'seconds':{key:value/1e9 for key,value in self.nanoseconds.items()},
                'calls':dict(self.calls),'native_cumulative_profile':dict(self.native)}


def candidate_index(width,row_offset,col_offset):
    if (type(width) is not int or width not in (64,128,256) or
        type(row_offset) is not int or type(col_offset) is not int or
        not 0<=row_offset<width or not 0<=col_offset<width or
        row_offset%64>=32 or col_offset%64):
        raise ValueError('Position is not a PRL candidate in this scan')
    return ((row_offset//64)*(width//64)+col_offset//64)*32+row_offset%64


def advance_scan(row_base,col_base,m,n,width):
    if (width not in (64,128,256) or m%width or n%width or
        row_base%width or col_base%width or not 0<=row_base<m or not 0<=col_base<n):
        raise ValueError('Invalid scan geometry')
    col_base+=width
    if col_base>=n:col_base=0;row_base+=width
    return row_base,col_base


def selftest_scans(width,reference,m=16384,n=16384):
    """Cover every frozen reference tile, several subblocks and the final corner."""
    positions={(0,0),(width-33,0),(0,width-64),(width-33,width-64),(m-33,n-64)}
    blocks={(row['row_base'],row['col_base']) for row in reference['tiles']}
    for rb,cb in blocks:positions.add((rb+(0 if (rb,cb)==(0,0) else 7),cb))
    scans={}
    for row,col in sorted(positions):
        rb,cb=(row//width)*width,(col//width)*width
        if not 0<=rb<=m-width or not 0<=cb<=n-width:raise ValueError('Self-test coordinate outside matrix')
        scans.setdefault((rb,cb),set()).add(candidate_index(width,row-rb,col-cb))
    return [(rb,cb,tuple(sorted(indices))) for (rb,cb),indices in sorted(scans.items())]


def check_reference_hashes(hashes,row_base,col_base,width,reference_tiles):
    matched=[]
    for entry in reference_tiles:
        row=entry['row_base']+entry['index'];col=entry['col_base']
        if row_base<=row<row_base+width and col_base<=col<col_base+width:
            index=candidate_index(width,row-row_base,col-col_base)
            if hashes[index].hex()!=entry['jackpot_hex']:
                raise RuntimeError('Actual NPU hashes differ from independent official host oracle')
            matched.append((entry['row_base'],entry['col_base'],entry['index']))
    return matched

def device_binding(cfg):
    return {'hostname':socket.gethostname(),'machine_id':Path('/etc/machine-id').read_text().strip(),'kernel':os.uname().release,'config_sha256':hashlib.sha256(json.dumps(cfg,sort_keys=True,separators=(',',':')).encode()).hexdigest()}

class NPU:
    def __init__(self,library,model,*,width=64,model_sha256=None,variant='i32-64'):
        cfg={'model':model,'npu_width':width,'npu_variant':variant}
        if model_sha256 is not None:cfg['model_sha256']=model_sha256
        spec=model_spec(cfg)
        self.width=spec['width'];self.variant=spec['variant'];self.candidate_offsets=candidate_offsets(self.width)
        self.batch=None;self.ctx=None;self.last_profile=None
        if self.width>64:
            path=BASE/'backend/batch_backend.py'
            module_spec=importlib.util.spec_from_file_location('prl_release_batch_backend',path)
            if module_spec is None or module_spec.loader is None:raise RuntimeError('Missing batch adapter')
            adapter=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(adapter)
            if tuple(adapter.candidate_offsets())!=self.candidate_offsets:raise RuntimeError('Batch adapter/width mismatch')
            self.batch=adapter.BatchBackend(library,model,spec['format'],spec['sha256'])
            return
        self.lib=C.CDLL(str(library))
        self.lib.prl_create.argtypes=[C.c_char_p];self.lib.prl_create.restype=C.c_void_p
        self.lib.prl_last_error.argtypes=[];self.lib.prl_last_error.restype=C.c_char_p
        self.lib.prl_scan_tile.argtypes=[C.c_void_p,C.c_void_p,C.c_uint64,C.c_void_p,C.c_uint64,C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.c_uint32,C.c_void_p,C.c_void_p,C.POINTER(C.c_uint64)]
        self.lib.prl_scan_tile.restype=C.c_int
        self.lib.prl_destroy.argtypes=[C.c_void_p];self.lib.prl_destroy.restype=None
        self.ctx=self.lib.prl_create(os.fsencode(model))
        if not self.ctx:raise RuntimeError(self.error())
    def error(self):
        value=self.lib.prl_last_error();return value.decode(errors='replace') if value else 'unknown NPU error'
    def scan(self,prepared,row_base,col_base):
        if self.batch is not None:
            packed,macs=self.scan_packed(prepared,row_base,col_base)
            return [packed[offset:offset+32] for offset in range(0,len(packed),32)],macs
        output=(C.c_ubyte*1024)();commit=(C.c_ubyte*32).from_buffer_copy(prepared.commit_a);macs=C.c_uint64()
        result=self.lib.prl_scan_tile(self.ctx,C.cast(prepared.a_noised,C.c_void_p),prepared.m*4096,C.cast(prepared.b_noised,C.c_void_p),4096*prepared.n,prepared.m,prepared.n,4096,row_base,col_base,commit,output,C.byref(macs))
        if result!=0:raise RuntimeError(self.error())
        if macs.value!=64*64*4096:raise RuntimeError('Invalid NPU work count')
        return [bytes(output[i*32:(i+1)*32]) for i in range(32)],macs.value
    def scan_packed(self,prepared,row_base,col_base):
        if self.batch is None:
            hashes,macs=self.scan(prepared,row_base,col_base)
            self.last_profile=None
            return b''.join(hashes),macs
        packed,profile=self.batch.scan_packed(prepared,row_base,col_base)
        macs=profile['completed_macs']
        if (not isinstance(packed,bytes) or len(packed)!=len(self.candidate_offsets)*32 or
                macs!=self.width*self.width*4096):
            raise RuntimeError('Invalid packed NPU output/count')
        self.last_profile=profile
        return packed,macs
    def close(self):
        if self.batch is not None:self.batch.close();self.batch=None;return
        if self.ctx:self.lib.prl_destroy(self.ctx);self.ctx=None


def configured_npu(cfg):
    spec=model_spec(cfg)
    return NPU(Path(cfg['libdir'])/'libprl_npu.so',cfg['model'],width=spec['width'],model_sha256=spec['sha256'],variant=spec['variant'])

def artifacts(cfg):
    model_spec(cfg)
    paths=list(BASE.rglob('*.py'))+[BASE/'proof/host-reference.json',Path(cfg['model'])]
    paths.extend(Path(cfg['libdir'])/name for name in NATIVE_LIBRARIES)
    result={str(p.resolve()):sha(p) for p in sorted(paths)}
    result['canonical_config_sha256']=hashlib.sha256(json.dumps(cfg,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return result


def logger_for(cfg,role='user'):
    directory=Path(cfg['log_dir']);directory.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('prl');logger.setLevel(logging.INFO);logger.propagate=False
    file=logging.handlers.RotatingFileHandler(directory/'events.jsonl',maxBytes=5*1024*1024,backupCount=3,encoding='utf-8')
    logger.addHandler(file);logger.addHandler(logging.StreamHandler(sys.stdout))
    def emit(event,**fields):
        entry=dict(event) if isinstance(event,dict) else {'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event':event,**fields}
        entry.update(mining_role=role,launch_token=os.environ.get('PRL_LAUNCH_TOKEN'))
        logger.info(json.dumps(entry,ensure_ascii=False,separators=(',',':')))
    return emit

def check_environment(cfg):
    preflight(cfg)
    os.sched_setaffinity(0,set(cfg['cpu_affinity']));os.nice(10)
    threads=str(cfg['cpu_threads'])
    os.environ.update(OMP_NUM_THREADS=threads,OMP_THREAD_LIMIT=threads,RAYON_NUM_THREADS=threads,OPENBLAS_NUM_THREADS=threads)
    model_spec(cfg)


def make_client(cfg,verify,on_event,role='user',phase_deadline=None):
    cfg=client_config(cfg,role)
    options=dict(host=cfg['pool_host'],port=cfg['pool_port'],verify_proof=verify,on_event=on_event,submission_deadline=phase_deadline)
    if cfg['pool_kind']=='suprnova':
        return SuprnovaClient(cfg['account'],cfg['worker'],MiningConfig(),allow_plaintext=cfg['allow_plaintext'],max_runtime_seconds=cfg['max_runtime_seconds'],**options)
    return StratumClient(cfg['account'],cfg['worker'],MiningConfig(),**options)


def self_test(cfg,header_hex):
    # No network client is created in this mode; easy target can never be submitted.
    from engine.proof.liveproof import PreparedJob
    emit=logger_for(cfg);header=bytes.fromhex(header_hex)
    if len(header)!=76:raise ValueError('Expected76-byte captured header')
    reference=json.loads((BASE/'proof/host-reference.json').read_text())
    if header_hex!=reference['source_job']['header_hex'] or reference['real_pool_submissions']!=0:
        raise RuntimeError('Expected independent offline reference job')
    bound_artifacts=artifacts(cfg);bound_device=device_binding(cfg)
    config=MiningConfig();start=time.monotonic();npu=None
    prepared=None;receipt={'status':'IN_PROGRESS','mode':'OFFLINE_REAL_PREPARATION_NPU_OFFICIAL_VERIFY','pool_submissions':0,'header_hex':header.hex(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':[],'scans':[]}
    atomic_json(cfg['validation_gate'],receipt)
    try:
        npu=configured_npu(cfg)
        receipt.update(npu_width=npu.width,npu_variant=npu.variant)
        prepared=PreparedJob(cfg['libdir'],header,seed32=bytes.fromhex(reference['seed32_hex']),m=16384,n=16384,cert_version=3,proof_mode=cfg.get('proof_mode','legacy'))
        if prepared.config52!=config.to_bytes():raise RuntimeError('Official config differs from Stratum config')
        for field,key in [('job_key','job_key_hex'),('commit_a','commit_a_hex'),('commit_b','commit_b_hex'),('root_a','root_a_hex'),('root_b','root_b_hex')]:
            if getattr(prepared,field).hex()!=reference[key]:raise RuntimeError('ARM preparation differs from independent host reference: '+field)
        nbits=target_to_compact(MAX256//config.adjustment_factor)
        bound=compact_to_target(nbits)*config.adjustment_factor
        matched_reference=set()
        for rowbase,colbase,indices in selftest_scans(npu.width,reference):
            reason=stop_reason(snapshot(cfg),True)
            if reason:raise RuntimeError(reason)
            hashes,macs=npu.scan(prepared,rowbase,colbase)
            known=check_reference_hashes(hashes,rowbase,colbase,npu.width,reference['tiles'])
            matched_reference.update(known)
            receipt['scans'].append({'row_base':rowbase,'col_base':colbase,'width':npu.width,'NPU_MACs':macs,'hashes_hex':[h.hex() for h in hashes],'independent_host_hashes_checked':len(known)})
            for index in indices:
                reason=stop_reason(snapshot(cfg),True)
                if reason:raise RuntimeError(reason)
                hit=hashes[index];dr,dc=npu.candidate_offsets[index];proof_row=rowbase+dr;proof_col=colbase+dc
                if int.from_bytes(hit,'little')>bound:raise RuntimeError('Extremely rare easy-target miss; repeat independent fixture')
                proof=prepared.build_proof(proof_row,proof_col)
                verified=prepared.verify(proof,nbits,expected_jackpot=hit)
                if verified.get('valid') is not True:raise RuntimeError('Official verifier failed: '+json.dumps(verified))
                corrupted=bytearray(proof);corrupted[-1]^=1
                negative=prepared.verify(bytes(corrupted),nbits,expected_jackpot=hit)
                if negative.get('valid') is True:raise RuntimeError('Corrupt proof passed verification')
                actual_target=prepared.verify(proof,reference['source_job']['nbits_override'],expected_jackpot=hit)
                if actual_target.get('valid') is True:raise RuntimeError('Offline fixture unexpectedly meets actual pool target')
                receipt['checks'].append({'row_base':rowbase,'col_base':colbase,'tile_index':index,'proof_row':proof_row,'proof_col':proof_col,'jackpot_hex':hit.hex(),'proof_sha256':hashlib.sha256(proof).hexdigest(),'proof_bytes':len(proof),'official_verification':verified,'actual_target_offline_verification':actual_target,'corruption_rejected':True})
                checkdir=Path(cfg['runtime_dir'])/'selftest';checkdir.mkdir(parents=True,exist_ok=True)
                (checkdir/f'proof-{proof_row}-{proof_col}.bin').write_bytes(proof)
        expected_reference={(row['row_base'],row['col_base'],row['index']) for row in reference['tiles']}
        if len(expected_reference)!=len(reference['tiles']) or matched_reference!=expected_reference:
            raise RuntimeError('Incomplete or duplicate independent host reference coverage')
        receipt['independent_host_hashes_checked_total']=len(matched_reference)
        if artifacts(cfg)!=bound_artifacts or device_binding(cfg)!=bound_device:
            raise RuntimeError('Artifacts or device configuration changed during offline validation')
        receipt.update(status='PASS',elapsed_seconds=time.monotonic()-start,artifact_sha256=bound_artifacts,device_binding=bound_device)
        atomic_json(cfg['validation_gate'],receipt);emit('offline_official_verification_passed',receipt=receipt)
        return receipt
    except Exception as exc:
        receipt.update(status='FAILED',error=repr(exc),elapsed_seconds=time.monotonic()-start)
        atomic_json(cfg['validation_gate'],receipt);raise
    finally:
        if npu is not None:npu.close()
        if prepared is not None and hasattr(prepared,'close'):prepared.close()

def live(cfg,role='user',phase_deadline=None):
    from engine.proof.liveproof import PreparedJob
    gate=json.loads(Path(cfg['validation_gate']).read_text())
    if gate.get('status')!='PASS' or gate.get('pool_submissions')!=0 or gate.get('artifact_sha256')!=artifacts(cfg) or gate.get('device_binding')!=device_binding(cfg):
        raise RuntimeError('Missing or stale device NPU+official-proof validation gate')
    if type(phase_deadline) not in (int,float) or not math.isfinite(phase_deadline) or phase_deadline<=time.monotonic():
        raise RuntimeError('Live mining requires an unexpired absolute guard phase deadline')
    connection_cfg=client_config(cfg,role)
    emit=logger_for(cfg,role);runtime=Path(cfg['runtime_dir']);runtime.mkdir(parents=True,exist_ok=True)
    alive=True;prepared=None;prepared_job=None;pending_verification=None;npu=None;failed_error=None
    started=time.monotonic();last_report=0.;interval_started=started;interval_macs=0;total_macs=0;stale_macs=0;scanned_tiles=0;rounds=0
    timings=PhaseTimings();process_cpu_started=time.process_time()
    def interrupted(*unused):
        nonlocal alive
        alive=False
    signal.signal(signal.SIGINT,interrupted);signal.signal(signal.SIGTERM,interrupted)
    def verify(job,proof):
        if pending_verification is None or prepared_job!=job:return False
        expected,proof_sha=pending_verification
        if hashlib.sha256(proof).hexdigest()!=proof_sha:return False
        tick=time.perf_counter_ns()
        try:result=prepared.verify(proof,job.nbits_override,expected_jackpot=expected)
        finally:timings.add('proof_verify',time.perf_counter_ns()-tick)
        emit('local_real_target_verification',job_token=job.token,result=result)
        return result.get('valid') is True
    client=make_client(cfg,verify,emit,role,phase_deadline)
    emit('developer_fee_disclosure',policy=disclosure(cfg),account=connection_cfg['account'],worker=connection_cfg['worker'])
    def status(state,**extra):
        nonlocal last_report,interval_started,interval_macs
        now=time.monotonic();stats=client.status()
        value={'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'state':state,'worker':connection_cfg['worker'],'account':connection_cfg['account'],'mining_role':role,'host':socket.gethostname(),'pool':stats,'job':prepared_job.to_envelope() if prepared_job else None,'raw_NPU_MACs':total_macs,'stale_NPU_MACs':stale_macs,'effective_logical_MACs':total_macs-stale_macs,'effective_logical_GMAC_per_second':(total_macs-stale_macs)/max(.001,now-started)/1e9,'tile_hashes_scanned':scanned_tiles,'prepared_rounds':rounds,'raw_NPU_MAC_per_second_since_start':total_macs/max(.001,now-started),'interval_raw_NPU_MAC_per_second':interval_macs/max(.001,now-interval_started),'npu_width':npu.width if npu else None,'npu_variant':npu.variant if npu else None,'scope':'Current role/session only; accepted share count only from matching mining.submit responses','dev_fee':cfg['dev_fee'],'developer_fee_policy':disclosure(cfg),**extra}
        value.update(pid=os.getpid(),launch_token=os.environ.get('PRL_LAUNCH_TOKEN'),
                     phase_timings=timings.to_dict(),process_cpu_seconds=max(0,time.process_time()-process_cpu_started))
        atomic_json(runtime/'miner.json',value);last_report=now
        if now-interval_started>=30:
            emit('mining_status',status=value);interval_started=now;interval_macs=0
    try:
        tick=time.perf_counter_ns()
        try:npu=configured_npu(cfg)
        finally:timings.add('npu_initialize',time.perf_counter_ns()-tick)
        client.start();status('WAITING_FOR_JOB')
        while alive:
            if time.monotonic()>=phase_deadline or time.monotonic()-started>=cfg['max_runtime_seconds']:
                emit('runtime_or_phase_limit_reached');break
            if client.status().get('terminal_error'):
                raise RuntimeError('Pool protocol error: '+str(client.status()['terminal_error']))
            reason=stop_reason(snapshot(cfg),True)
            if reason:raise RuntimeError(reason)
            job=client.get_latest_job()
            if job is None:
                if prepared is not None:
                    if hasattr(prepared,'close'):prepared.close()
                    prepared=None;prepared_job=None;gc.collect()
                status('WAITING_FOR_JOB');client.wait_for_job(timeout=min(1,max(0,phase_deadline-time.monotonic()),max(0,cfg['max_runtime_seconds']-(time.monotonic()-started))));continue
            if prepared_job!=job or prepared is None:
                if prepared is not None:
                    if hasattr(prepared,'close'):prepared.close()
                    prepared=None;gc.collect()
                prepared_job=job;status('PREPARING_REAL_JOB')
                if time.monotonic()>=phase_deadline:break
                t=time.monotonic();tick=time.perf_counter_ns()
                try:prepared=PreparedJob(cfg['libdir'],job.header,seed32=os.urandom(32),m=job.config.m,n=job.config.n,cert_version=job.cert_version,proof_mode=cfg.get('proof_mode','legacy'))
                finally:timings.add('job_prepare',time.perf_counter_ns()-tick)
                if prepared.config52!=job.config_bytes:raise RuntimeError('Official config mismatch')
                rounds+=1;emit('real_job_prepared',job_token=job.token,seconds=time.monotonic()-t,commit_a=prepared.commit_a.hex(),
                              proof_mode=cfg.get('proof_mode','legacy'),proof_cache_create_seconds=getattr(prepared,'proof_cache_create_seconds',0))
                rowbase=0;colbase=0
                if time.monotonic()>=phase_deadline or not client.is_current(job):continue
            if time.monotonic()>=phase_deadline:break
            tick=time.perf_counter_ns()
            try:packed,macs=npu.scan_packed(prepared,rowbase,colbase)
            finally:timings.add('native_scan_and_adapter',time.perf_counter_ns()-tick)
            timings.add_native(npu.last_profile)
            total_macs+=macs;interval_macs+=macs
            if not client.is_current(job):stale_macs+=macs;continue
            scanned_tiles+=len(packed)//32
            tick=time.perf_counter_ns()
            hits=packed_candidates(packed,job.scaled_target)
            timings.add('candidate_filter',time.perf_counter_ns()-tick)
            for tile_index,jackpot in hits:
                if time.monotonic()>=phase_deadline or not client.is_current(job):break
                dr,dc=npu.candidate_offsets[tile_index]
                status('BUILDING_REAL_HIT_PROOF',hit_row=rowbase+dr,hit_col=colbase+dc)
                if time.monotonic()>=phase_deadline:break
                tick=time.perf_counter_ns()
                try:proof=prepared.build_proof(rowbase+dr,colbase+dc)
                finally:timings.add('proof_build',time.perf_counter_ns()-tick)
                pending_verification=(jackpot,hashlib.sha256(proof).hexdigest())
                tick=time.perf_counter_ns()
                try:client.submit_proof(job,proof)
                except StaleJob as exc:emit('stale_hit_not_submitted',reason=str(exc))
                finally:
                    timings.add('submit_including_verify',time.perf_counter_ns()-tick)
                    pending_verification=None
            rowbase,colbase=advance_scan(rowbase,colbase,prepared.m,prepared.n,npu.width)
            if rowbase>=prepared.m:
                # A fresh seed creates new candidate work after complete matrix coverage.
                if hasattr(prepared,'close'):prepared.close()
                prepared=None;gc.collect()
            if time.monotonic()-last_report>=5:status('MINING_NPU_REAL_PRL_JOB')
        status('STOPPING')
    except Exception as exc:
        failed_error=repr(exc)
        status('FAILED',error=failed_error);emit('fatal_error',error=failed_error);raise
    finally:
        try:
            client.stop()
            status('FAILED' if failed_error else 'STOPPED',**({'error':failed_error} if failed_error else {}))
        finally:
            if npu is not None:npu.close()
            if prepared is not None and hasattr(prepared,'close'):prepared.close()

def main():
    parser=argparse.ArgumentParser(description='Experimental A733 f32-256 PRL miner; optional disclosed 1% developer fee and bounded foreground guard.')
    parser.add_argument('--config',type=Path,required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--self-test',action='store_true',help='Offline NPU/reference/official-proof gate; no pool connection')
    mode.add_argument('--live',action='store_true',help='Bounded foreground live pool test; requires a fresh offline gate')
    parser.add_argument('--guard-child',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args();cfg=validate_config(json.loads(args.config.read_text()))
    if not args.guard_child:
        from engine.runtime_guard import run_guard
        return run_guard(cfg,args.config,'self-test' if args.self_test else 'live')
    from engine.runtime_guard import bind_parent_death
    bind_parent_death()
    check_environment(cfg)
    if args.self_test:
        reference=json.loads((BASE/'proof/host-reference.json').read_text())
        self_test(cfg,reference['source_job']['header_hex'])
    else:
        deadline=float(os.environ.get('EDGE_PRL_PHASE_DEADLINE_MONOTONIC','nan'))
        live(cfg,os.environ.get('EDGE_PRL_MINING_ROLE','user'),deadline)
    return 0
if __name__=='__main__':raise SystemExit(main())
