import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from contextlib import ExitStack

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import runtime_guard as guard
from engine import miner

class GuardTests(unittest.TestCase):
    def test_stop_escalates_only_owned_group_and_verifies_exit(self):
        child=Mock();child.pid=4321;child.poll.return_value=None
        waits=0
        def waited(timeout):
            nonlocal waits
            waits+=1
            if waits==1:raise guard.subprocess.TimeoutExpired('owned-test-child',timeout)
            child.poll.return_value=-9
            return -9
        child.wait.side_effect=waited
        with patch.object(guard.os,'killpg',create=True) as kill,patch.object(guard.signal,'SIGKILL',9,create=True):
            guard.stop_child(child)
            self.assertEqual([call.args[0] for call in kill.call_args_list],[4321,4321])
            self.assertEqual([call.args[1] for call in kill.call_args_list],[guard.signal.SIGTERM,9])
            self.assertEqual(child.poll(),-9)

    def test_stop_refuses_to_claim_termination_if_child_still_alive(self):
        child=Mock();child.pid=4321;child.poll.return_value=None;child.wait.return_value=0
        with patch.object(guard.os,'killpg',create=True),patch.object(guard.signal,'SIGKILL',9,create=True):
            with self.assertRaisesRegex(RuntimeError,'still alive'):guard.stop_child(child,force=True)

    def test_lock_conflict_closes_descriptor_and_refuses_run(self):
        fake=SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=Mock(side_effect=BlockingIOError('owned')))
        with patch.dict(sys.modules,{'fcntl':fake}),patch.object(guard.os,'open',return_value=99),patch.object(guard.os,'close') as close,patch.object(guard.os,'O_NOFOLLOW',0,create=True),patch.object(guard.os,'O_CLOEXEC',0,create=True):
            with self.assertRaisesRegex(RuntimeError,'Another engine'):
                guard.acquire_device_lock('/tmp/test-only.lock')
            close.assert_called_once_with(99)

    def test_deadline_kills_owned_child_even_without_status_or_network(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            cfg={'runtime_dir':directory,'max_runtime_seconds':1}
            stack.enter_context(patch.object(guard,'validate_config',side_effect=lambda x:x))
            stack.enter_context(patch.object(guard,'preflight',return_value={'status':'PASS'}))
            stack.enter_context(patch.object(guard,'check_old_service',return_value='inactive'))
            stack.enter_context(patch.object(guard,'acquire_device_lock',return_value=999))
            close=stack.enter_context(patch.object(guard.os,'close'))
            stack.enter_context(patch.object(guard.signal,'signal'))
            stack.enter_context(patch.object(guard.time,'monotonic',side_effect=[0,0,0,2,2,2,2]))
            stack.enter_context(patch.object(guard,'snapshot',return_value={'temperature_C':50,'fan_pwm':255,'fan_enabled':1,'memory_available_MiB':1000}))
            child=Mock();child.pid=123;child.poll.return_value=None
            stack.enter_context(patch.object(guard.subprocess,'Popen',return_value=child))
            stop=stack.enter_context(patch.object(guard,'stop_child',side_effect=lambda child,force=False:setattr(child.poll,'return_value',0)))
            self.assertEqual(guard.run_guard(cfg,Path(directory)/'config.json'),0)
            self.assertTrue(any(call.kwargs.get('force') is True for call in stop.call_args_list))
            close.assert_called_once_with(999)
            status=json.loads((Path(directory)/'supervisor.json').read_text())
            self.assertEqual(status['reason'],'RUNTIME_LIMIT')
            self.assertEqual(status['state'],'STOPPED')

    def test_native_child_requires_owning_guard(self):
        with patch.dict(guard.os.environ,{'EDGE_PRL_GUARD_PID':'0'}):
            with self.assertRaises(RuntimeError):guard.bind_parent_death()

    def test_stale_heartbeat_from_other_process_is_not_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'
            path.write_text(json.dumps({'pid':1,'launch_token':'older-run'}))
            self.assertEqual(guard.heartbeat_age(path,2,'new-run',190),190)

    def test_pool_factory_preserves_explicit_transport_and_runtime(self):
        cfg={'pool_kind':'suprnova','pool_host':'stratum-us.suprnova.cc','pool_port':3370,
             'allow_plaintext':True,'max_runtime_seconds':21,'account':'prl1'+'q'*30,'worker':'test'}
        with patch.object(miner,'SuprnovaClient') as client:
            miner.make_client(cfg,lambda *a:True,lambda *a:None)
            self.assertEqual(client.call_args.kwargs['max_runtime_seconds'],21)
            self.assertTrue(client.call_args.kwargs['allow_plaintext'])

    def test_candidate_offsets_remain_512_unique_valid_pairs(self):
        offsets=miner.candidate_offsets(256)
        self.assertEqual(len(offsets),512)
        self.assertEqual(len(set(offsets)),512)
        for i,(row,col) in enumerate(offsets):
            self.assertEqual(miner.candidate_index(256,row,col),i)

if __name__=='__main__':unittest.main()
