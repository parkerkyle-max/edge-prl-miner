"""One bounded foreground run, exclusive NPU lock, no services or fan writes."""
from __future__ import annotations
import argparse
import ctypes
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.configuration import validate_config,preflight,snapshot,stop_reason,check_old_service
from engine.devfee import phase_window,disclosure,CYCLE_SECONDS

LOCK_PATH = '/tmp/edge-prl-a733-device.lock'

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as handle:
            json.dump(data,handle,ensure_ascii=False,sort_keys=True);handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()

def acquire_device_lock(path=LOCK_PATH):
    import fcntl
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise RuntimeError('Another engine owns the A733 NPU lock; no second run was started')
    return descriptor

def bind_parent_death():
    """Prevent a native child surviving an unexpected death of its guard."""
    expected=int(os.environ.get('EDGE_PRL_GUARD_PID','0'))
    if expected<=1 or os.getppid()!=expected:
        raise RuntimeError('Native work requires its owning foreground guard')
    libc=ctypes.CDLL(None,use_errno=True)
    libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
    libc.prctl.restype=ctypes.c_int
    if libc.prctl(1,int(signal.SIGKILL),0,0,0)!=0:
        raise OSError(ctypes.get_errno(),'Cannot set parent-death signal')
    if os.getppid()!=expected:
        raise RuntimeError('Guard exited during child initialization')

def stop_child(child,force=False):
    if child is None or child.poll() is not None:return
    try:os.killpg(child.pid,signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        if child.poll() is not None:return
        raise RuntimeError('Owned child termination could not be verified')
    try:child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        child.wait(timeout=3)
    if child.poll() is None:
        raise RuntimeError('Owned child is still alive after termination')


def snapshot_counters(path,pid,launch_token,role):
    """Last saved lower-bound counts, never inference from pending work."""
    empty={'submitted':0,'accepted':0,'rejected':0,'submit_response_unknown':0,'submit_disconnect_unknown':0}
    try:
        saved=json.loads(Path(path).read_text())
        if (saved.get('pid')!=pid or saved.get('launch_token')!=launch_token or
                saved.get('mining_role')!=role):return empty
        pool=saved.get('pool',{})
        if not isinstance(pool,dict):return empty
        return {key:pool[key] if type(pool.get(key)) is int and pool[key]>=0 else 0 for key in empty}
    except (OSError,ValueError,TypeError):return empty

def heartbeat_age(status_path,child_pid,launch_token,elapsed):
    try:
        saved=json.loads(Path(status_path).read_text())
        if saved.get('pid')==child_pid and saved.get('launch_token')==launch_token:
            return max(0,time.time()-Path(status_path).stat().st_mtime)
    except (OSError,ValueError):pass
    return elapsed

def run_guard(cfg,config_path,mode='live'):
    cfg=validate_config(cfg)
    if mode not in ('live','self-test'):raise ValueError('Unsupported guard mode')
    preflight(cfg)
    child=None;reason='STARTING';return_code=1
    runtime=Path(cfg['runtime_dir']);runtime.mkdir(parents=True,exist_ok=True)
    supervisor_path=runtime/'supervisor.json'
    alive=True
    def interrupted(*unused):
        nonlocal alive
        alive=False
    previous={}
    lock=acquire_device_lock()
    started=time.monotonic();deadline=started+cfg['max_runtime_seconds']
    fee_percent=cfg.get('dev_fee',0) if mode=='live' else 0
    token=None;role=None;phase_deadline=deadline;phase_started=None
    skipped_cycles=set();history=[]
    totals={name:{'submitted':0,'accepted':0,'rejected':0,'submit_response_unknown':0,'submit_disconnect_unknown':0}
            for name in ('user','developer')}
    def emit(event,**fields):
        print(json.dumps({'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          'event':event,**fields},sort_keys=True),flush=True)
    def finish_child(why,force=True):
        nonlocal child,phase_started
        if child is None:return
        stop_child(child,force=force)
        # No replacement is launched unless the owned process is proven dead.
        if child.poll() is None:raise RuntimeError('Refusing overlapping mining children')
        counts=snapshot_counters(runtime/'miner.json',child.pid,token,role)
        for key,value in counts.items():totals[role][key]+=value
        ended=time.monotonic()
        entry={'role':role,'pid':child.pid,'launch_token':token,'reason':why,
               'start_elapsed_seconds':phase_started-started,'end_elapsed_seconds':ended-started,
               'scheduled_deadline_elapsed_seconds':phase_deadline-started,
               'last_snapshot_counters':counts}
        history.append(entry);emit('guard_phase_stopped',**entry)
        child=None;phase_started=None
    def receipt(state,**extra):
        return {'state':state,'mode':mode,'elapsed_seconds':time.monotonic()-started,
                'max_runtime_seconds':cfg['max_runtime_seconds'],'dev_fee':cfg.get('dev_fee',0),
                'active_mining_role':role if child is not None else None,
                'developer_fee_policy':disclosure(cfg),'completed_phase_history':history,
                'fee_active_in_this_mode':bool(fee_percent),
                'role_share_counts_last_saved_lower_bound':totals,
                'share_count_scope':'Saved child snapshots only; pending/unknown never accepted; consult role-tagged events for complete replies',
                'XMR_control':'UNMODIFIED',**extra}
    try:
        previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGTERM,signal.SIGINT)}
        # Recheck under the global lock; this never controls the old service.
        check_old_service()
        root=Path(__file__).resolve().parents[1]
        command=[sys.executable,'-u','-m','engine.miner','--config',str(Path(config_path).resolve()),'--'+mode,'--guard-child']
        emit('guard_fee_disclosure',mode=mode,policy=disclosure(cfg),fee_active_in_this_mode=bool(fee_percent))
        while True:
            now=time.monotonic()
            if not alive:reason='USER_STOP';return_code=130;break
            if now>=deadline:reason='RUNTIME_LIMIT';return_code=0 if mode=='live' else 1;break
            if child is not None:
                if now>=phase_deadline:
                    next_role,next_deadline=phase_window(now-started,cfg['max_runtime_seconds'],fee_percent,skipped_cycles)
                    if next_role==role=='user':
                        # A skipped fee window must not restart useful user work.
                        phase_deadline=started+next_deadline
                        continue
                    finish_child('PHASE_DEADLINE')
                    # Re-evaluate against the original wall clock: never recover
                    # missed fee time after slow process shutdown or scheduling.
                    continue
                exit_code=child.poll()
                if exit_code is not None:
                    if role=='developer':
                        skipped_cycles.add(int((phase_started-started)//CYCLE_SECONDS))
                        finish_child('DEVELOPER_CHILD_EXIT')
                        continue
                    reason='CHILD_EXIT';return_code=exit_code;break
            try:
                reading=snapshot(cfg)
                failure=stop_reason(reading,True)
            except Exception as exc:
                reason='SENSOR_READ_FAILED: '+str(exc);break
            if failure:reason=failure;break
            if child is None:
                next_role,phase_elapsed_deadline=phase_window(now-started,cfg['max_runtime_seconds'],fee_percent,skipped_cycles)
                if next_role is None:continue
                role=next_role;phase_deadline=started+phase_elapsed_deadline
                token=uuid.uuid4().hex
                env=os.environ.copy()
                env.update(EDGE_PRL_GUARD_PID=str(os.getpid()),PRL_LAUNCH_TOKEN=token,EDGE_PRL_MINING_ROLE=role,
                           EDGE_PRL_PHASE_DEADLINE_MONOTONIC=repr(phase_deadline if role=='developer' else deadline),
                           OMP_NUM_THREADS='1',OMP_THREAD_LIMIT='1',RAYON_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
                phase_started=time.monotonic()
                # A sensor check or scheduler delay may consume a whole window.
                if phase_started>=phase_deadline:phase_started=None;continue
                child=subprocess.Popen(command,cwd=root,stdin=subprocess.DEVNULL,env=env,start_new_session=True)
                emit('guard_phase_started',role=role,pid=child.pid,launch_token=token,
                     start_elapsed_seconds=phase_started-started,
                     scheduled_deadline_elapsed_seconds=phase_deadline-started)
                continue
            if mode=='live' and heartbeat_age(runtime/'miner.json',child.pid,token,now-phase_started)>180:
                reason='NO_PROGRESS_HEARTBEAT_180S';break
            atomic_json(supervisor_path,receipt('RUNNING',pid=child.pid,launch_token=token,reading=reading,
                phase_deadline_elapsed_seconds=phase_deadline-started))
            time.sleep(min(.5,max(0,min(deadline,phase_deadline)-time.monotonic())))
    except BaseException as exc:
        reason='GUARD_ERROR: '+type(exc).__name__+': '+str(exc)
        return_code=1
        raise
    finally:
        try:
            finish_child(reason,force=role=='developer' or reason in ('RUNTIME_LIMIT','TEMPERATURE_76C','FAN_COMMAND_NOT_FULL','LOW_AVAILABLE_MEMORY') or reason.startswith('SENSOR_READ_FAILED'))
            atomic_json(supervisor_path,receipt('STOPPED',reason=reason,exit_code=return_code,
                ended_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()))
        finally:
            os.close(lock)
            for sig,handler in previous.items():signal.signal(sig,handler)
    return return_code

def main():
    parser=argparse.ArgumentParser(description='Bounded foreground PRL NPU run; no autostart and no fan writes')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--self-test',action='store_true',help='Run offline hardware gate instead of live mining')
    args=parser.parse_args()
    cfg=validate_config(json.loads(args.config.read_text()))
    return run_guard(cfg,args.config,'self-test' if args.self_test else 'live')

if __name__=='__main__':raise SystemExit(main())
