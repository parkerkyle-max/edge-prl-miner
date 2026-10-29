"""Offline fee scheduling and account isolation; no device or network access."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import runtime_guard as guard,miner
from engine.configuration import validate_config
from engine.devfee import FEE_RECIPIENT,FEE_WORKER,client_config,phase_window
from test_engine_configuration import fixture
from test_engine_protocol import notify,FakeSocket,client as protocol_client
from engine.pool.suprnova import SuprnovaClient
from engine.pool.kryptex import StaleJob


def config():
    cfg=fixture()
    cfg.update(pool_kind='suprnova',pool_host='stratum-us.suprnova.cc',pool_port=3370,
               allow_plaintext=True,account='prl1'+'q'*30,dev_fee=1,max_runtime_seconds=610)
    return cfg


class ScheduleAndIdentityTests(unittest.TestCase):
    def test_window_is_not_upfront_and_never_catches_up(self):
        for elapsed,expected in [(0,('user',594)),(593.999,('user',594)),
                (594,('developer',600)),(599.9,('developer',600)),
                (600,('user',1194)),(1194,('developer',1200)),(1200,('user',1794))]:
            self.assertEqual(phase_window(elapsed,2400,1),expected)
        self.assertEqual(phase_window(603,2400,1),('user',1194))
        self.assertEqual(phase_window(0,300,1),('user',300))
        self.assertEqual(phase_window(595,2400,1,{0}),('user',600))
        self.assertEqual(phase_window(0,2400,0),('user',2400))

    def test_fee_never_exceeds_one_percent_of_any_schedule_prefix(self):
        # Includes shortened last windows and restarts with no carried debt.
        for end in (1,300,594,595,599,600,601,1199,1200,1800,2399,2400):
            cursor=0;developer=0
            while cursor<end:
                role,deadline=phase_window(cursor,end,1)
                if role=='developer':developer+=deadline-cursor
                cursor=deadline
            self.assertLessEqual(developer/end,.01)

    def test_fee_config_is_explicit_and_cannot_downgrade_tls(self):
        cfg=config()
        self.assertEqual(validate_config(cfg)['dev_fee'],1)
        for value in (-1,2,100,1.0,True,'1',None):
            bad=dict(cfg,dev_fee=value)
            with self.assertRaises(ValueError):validate_config(bad)
        tls=fixture();tls['dev_fee']=1
        with self.assertRaisesRegex(ValueError,'never downgrades TLS'):validate_config(tls)
        tls['dev_fee']=0
        self.assertEqual(validate_config(tls)['dev_fee'],0)

    def test_fee_identity_does_not_mutate_gate_bound_user_config(self):
        cfg=config();before=copy.deepcopy(cfg)
        effective=client_config(cfg,'developer')
        self.assertEqual(cfg,before)
        self.assertEqual(effective['account'],FEE_RECIPIENT)
        self.assertEqual(effective['worker'],FEE_WORKER)
        self.assertEqual(effective['pool_host'],cfg['pool_host'])
        self.assertEqual(client_config(cfg,'user'),cfg)
        with self.assertRaises(ValueError):client_config(dict(cfg,dev_fee=0),'developer')
        with self.assertRaises(ValueError):client_config(cfg,'unexpected')
        with patch.object(miner,'SuprnovaClient') as factory:
            miner.make_client(cfg,lambda *a:True,lambda *a:None,'developer')
            self.assertEqual(factory.call_args.args[:2],(FEE_RECIPIENT,FEE_WORKER))
            self.assertEqual(factory.call_args.kwargs['host'],cfg['pool_host'])

    def test_account_switch_never_reuses_jobs_or_accepted_counts(self):
        clients=[]
        for role in ('user','developer','user'):
            effective=client_config(config(),role)
            c=SuprnovaClient(effective['account'],effective['worker'],verify_proof=lambda *a:True,allow_plaintext=True)
            c._sock=FakeSocket();c._session=role+'-'+str(len(clients))
            with c._lock:rid=c._request('authorize','mining.authorize',{})
            c._handle_message({'id':rid,'result':True,'error':None})
            c._handle_message(notify())
            self.assertEqual(c.stats['accepted'],0)
            clients.append(c)
        previous=clients[0];job=previous.get_latest_job()
        rid=previous.submit_proof(job,b'offline-proof')
        previous._handle_message({'id':rid,'result':True,'error':None})
        self.assertEqual(previous.stats['accepted'],1)
        for replacement in clients[1:]:
            with self.assertRaises(StaleJob):replacement.submit_proof(job,b'offline-proof')
            replacement._handle_message({'id':rid,'result':True,'error':None})
            self.assertEqual(replacement.stats['accepted'],0)

    def test_expired_phase_cannot_submit_before_or_after_local_verification(self):
        c=protocol_client();job=c.get_latest_job();before=len(c._sock.sent)
        c.submission_deadline=10
        with patch('engine.pool.kryptex.time.monotonic',return_value=10):
            with self.assertRaises(StaleJob):c.submit_proof(job,b'offline-proof')
        with patch('engine.pool.kryptex.time.monotonic',side_effect=[9,10]):
            with self.assertRaises(StaleJob):c.submit_proof(job,b'offline-proof')
        self.assertEqual(len(c._sock.sent),before)
        self.assertEqual(c.stats['submitted'],0)
        self.assertEqual(c.stats['accepted'],0)

    def test_expiry_while_waiting_for_io_lock_never_sends_wire(self):
        c=protocol_client();job=c.get_latest_job();before=len(c._sock.sent)
        c.submission_deadline=10
        # Pre-submit, post-verifier, request creation, then the I/O lock.
        with patch('engine.pool.kryptex.time.monotonic',side_effect=[9,9,9,10]):
            with self.assertRaises(StaleJob):c.submit_proof(job,b'offline-proof')
        self.assertEqual(len(c._sock.sent),before)
        self.assertEqual(c.stats['accepted'],0)
        self.assertEqual(c.stats['submitted'],0)

    def test_suprnova_network_deadline_is_bound_to_fee_window(self):
        with patch('engine.pool.suprnova.time.monotonic',return_value=594):
            c=SuprnovaClient(FEE_RECIPIENT,FEE_WORKER,allow_plaintext=True,
                             max_runtime_seconds=2400,submission_deadline=600)
            self.assertEqual(c._experiment_deadline,600)


class FakeClock:
    def __init__(self,jump=None):self.now=0.;self.jump=jump;self.jumped=False
    def monotonic(self):return self.now
    def sleep(self,seconds):
        next_time=self.now+seconds
        if self.jump and not self.jumped and self.now<self.jump[0]<=next_time:
            next_time=self.jump[1];self.jumped=True
        self.now=next_time


class FakeChild:
    def __init__(self,pid,role,clock,fail_fee=False):
        self.pid=pid;self.role=role;self.clock=clock;self.started=clock.now
        self.returncode=None;self.fail_fee=fail_fee
    def poll(self):
        if self.fail_fee and self.role=='developer' and self.clock.now>=self.started+1:
            self.returncode=17
        return self.returncode


class GuardScheduleTests(unittest.TestCase):
    def exercise(self,*,duration=610,fee=1,mode='live',jump=None,fail_fee=False,fail_stop=False,user_stop=None,sensor_failure=None):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            cfg=config();cfg.update(runtime_dir=directory,max_runtime_seconds=duration,dev_fee=fee)
            original=copy.deepcopy(cfg);clock=FakeClock(jump);children=[];events=[];saved=[];signals={}
            def launch(command,**kwargs):
                self.assertTrue(all(c.poll() is not None for c in children),'Children overlap')
                self.assertEqual(cfg,original,'The validated input config was mutated')
                self.assertEqual(command[command.index('--config')+1],str(Path(directory,'config.json').resolve()))
                self.assertIn('--'+mode,command)
                self.assertEqual(command[-1],'--guard-child')
                self.assertTrue(kwargs['start_new_session'])
                env=kwargs['env'];role=env['EDGE_PRL_MINING_ROLE']
                expected_deadline=phase_window(clock.now,duration,fee)[1] if role=='developer' else duration
                self.assertEqual(float(env['EDGE_PRL_PHASE_DEADLINE_MONOTONIC']),expected_deadline)
                c=FakeChild(1000+len(children),role,clock,fail_fee)
                c.token=env['PRL_LAUNCH_TOKEN'];children.append(c);events.append(('start',role,clock.now))
                return c
            def stop(child,force=False):
                events.append(('stop',child.role,clock.now,force))
                if not fail_stop:child.returncode=-9 if force else 0
            def sample(_):
                if sensor_failure is not None and clock.now>=sensor_failure:raise OSError('sensor lost')
                if user_stop is not None and clock.now>=user_stop:signals[guard.signal.SIGINT](2,None)
                return dict(temperature_C=50,fan_pwm=255,fan_enabled=1,memory_available_MiB=1000)
            def register(sig,handler):
                old=signals.get(sig,lambda *a:None);signals[sig]=handler;return old
            for name,value in [('validate_config',lambda x:x),('preflight',lambda x:None),
                               ('check_old_service',lambda:'inactive'),('acquire_device_lock',lambda:99),
                               ('snapshot',sample),('heartbeat_age',lambda *a:0),('stop_child',stop),
                               ('atomic_json',lambda path,data:saved.append(copy.deepcopy(data)))]:
                stack.enter_context(patch.object(guard,name,side_effect=value))
            stack.enter_context(patch.object(guard.os,'close'))
            stack.enter_context(patch.object(guard.signal,'signal',side_effect=register))
            stack.enter_context(patch.object(guard.time,'monotonic',side_effect=clock.monotonic))
            stack.enter_context(patch.object(guard.time,'sleep',side_effect=clock.sleep))
            stack.enter_context(patch.object(guard.subprocess,'Popen',side_effect=launch))
            stack.enter_context(patch('builtins.print'))
            error=None
            try:rc=guard.run_guard(cfg,Path(directory)/'config.json',mode)
            except RuntimeError as exc:error=exc;rc=None
            return dict(rc=rc,error=error,events=events,children=children,saved=saved,clock=clock)

    def test_full_guard_switches_exact_roles_and_stops_before_replacement(self):
        r=self.exercise()
        self.assertIsNone(r['error']);self.assertEqual(r['rc'],0)
        self.assertEqual(r['events'],[('start','user',0),('stop','user',594,True),
            ('start','developer',594),('stop','developer',600,True),
            ('start','user',600),('stop','user',610,True)])
        self.assertEqual(len({c.token for c in r['children']}),3)
        self.assertTrue(all(c.poll() is not None for c in r['children']))
        self.assertEqual(r['saved'][-1]['state'],'STOPPED')
        self.assertIsNone(r['saved'][-1]['active_mining_role'])

    def test_missed_fee_window_does_not_charge_or_restart_user(self):
        r=self.exercise(jump=(594,601))
        self.assertEqual(r['events'],[('start','user',0),('stop','user',610,True)])

    def test_delayed_guard_never_extends_old_developer_into_later_window(self):
        r=self.exercise(duration=1202,jump=(600,1197))
        starts=[e for e in r['events'] if e[0]=='start']
        self.assertEqual(starts,[('start','user',0),('start','developer',594),
            ('start','developer',1197),('start','user',1200)])
        self.assertEqual(r['events'][3],('stop','developer',1197,True))

    def test_failed_fee_returns_to_user_and_does_not_retry_same_window(self):
        r=self.exercise(duration=1202,fail_fee=True)
        starts=[e for e in r['events'] if e[0]=='start']
        self.assertEqual(starts,[('start','user',0),('start','developer',594),
            ('start','user',595),('start','developer',1194),('start','user',1195)])
        self.assertEqual(r['rc'],0)

    def test_unverified_stop_fails_closed_without_replacement_or_stopped_claim(self):
        r=self.exercise(fail_stop=True)
        self.assertIsNotNone(r['error'])
        self.assertEqual(len(r['children']),1)
        self.assertNotEqual(r['saved'][-1]['state'],'STOPPED')

    def test_no_fee_when_disabled_or_session_short_or_offline_mode(self):
        for kwargs in ({'fee':0},{'duration':300},{'mode':'self-test'}):
            with self.subTest(kwargs=kwargs):
                r=self.exercise(**kwargs)
                self.assertEqual(len(r['children']),1)
                self.assertEqual(r['children'][0].role,'user')
                self.assertTrue(all(c.poll() is not None for c in r['children']))

    def test_interrupt_and_sensor_failure_stop_developer_child(self):
        for kwargs,reason in [({'user_stop':595},'USER_STOP'),({'sensor_failure':595},'SENSOR_READ_FAILED')]:
            r=self.exercise(**kwargs)
            self.assertTrue(r['saved'][-1]['reason'].startswith(reason))
            self.assertTrue(all(c.poll() is not None for c in r['children']))
            self.assertEqual(len(r['children']),2)

    def test_saved_counts_require_exact_role_process_and_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'miner.json'
            good=dict(pid=10,launch_token='fresh',mining_role='developer',pool={'accepted':2,'pending_submits':99})
            p.write_text(json.dumps(good))
            self.assertEqual(guard.snapshot_counters(p,10,'fresh','developer')['accepted'],2)
            for pid,token,role in [(11,'fresh','developer'),(10,'old','developer'),(10,'fresh','user')]:
                self.assertEqual(guard.snapshot_counters(p,pid,token,role)['accepted'],0)


if __name__=='__main__':unittest.main()
