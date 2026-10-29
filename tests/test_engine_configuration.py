"""No device access, libraries, network, or miner processes in these tests."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.configuration import MODEL_SHA256,validate_config,stop_reason,check_old_service,preflight,compatible_is_a733

def fixture():
    return dict(hostname='edge-test',device_confirmed=True,account='krxTEST',worker='test',dev_fee=0,
        npu_variant='f32-256',npu_width=256,model_sha256=MODEL_SHA256,backend_profile='cached-neon-a',
        model='/home/test/artifacts/model.nb',libdir='/home/test/artifacts/lib',runtime_dir='/home/test/run',
        log_dir='/home/test/log',validation_gate='/home/test/gate.json',cpu_affinity=[4],cpu_threads=1,
        fan_pwm_path='/sys/class/hwmon/hwmon9/pwm1',fan_enable_path='/sys/class/hwmon/hwmon9/pwm1_enable',
        pool_kind='kryptex',pool_host='prl-us.kryptex.network',pool_port=8048,allow_plaintext=False,max_runtime_seconds=60)

class ConfigurationTests(unittest.TestCase):
    def test_vendor_a733_pair_requires_both_exact_compatible_strings(self):
        self.assertTrue(compatible_is_a733([b'xunlong,orangepi-4-pro',b'arm,sun60iw2p1',b'']))
        self.assertTrue(compatible_is_a733([b'allwinner,sun60i-a733']))
        for values in ([b'arm,sun60iw2p1'],[b'xunlong,orangepi-4-pro'],
                       [b'xunlong,orangepi-4-pro',b'arm,sun60iw2p2'],
                       [b'xunlong,orangepi-other',b'arm,sun60iw2p1'],
                       [b'xunlong,orangepi-4-pro',b'arm,sun60'],[b'unknown,a733']):
            with self.subTest(values=values):self.assertFalse(compatible_is_a733(values))

    def test_format_check_does_not_read_device_or_open_network(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('file read')),patch.object(Path,'read_bytes',side_effect=AssertionError('file read')):
            self.assertEqual(validate_config(fixture()),dict(fixture(),backend_id='a733-viplite-v2-f32-256'))

    def test_explicit_fields_and_exact_types(self):
        bad_values={'device_confirmed':[False,1,None], 'dev_fee':[2,False,'0'], 'cpu_threads':[2,True,0],
            'cpu_affinity':[[],[4,4],[True],[-1]], 'max_runtime_seconds':[0,2401,True,1.5],
            'hostname':['','a b'], 'allow_plaintext':[0,'false'], 'model_sha256':['0'*64]}
        for key,values in bad_values.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    cfg=fixture();cfg[key]=value
                    with self.assertRaises(ValueError):validate_config(cfg)

    def test_no_arbitrary_pool_or_silent_plaintext(self):
        for key,value in [('pool_host','example.org'),('pool_port',7048),('allow_plaintext',True),('account','prl1invalid')]:
            cfg=fixture();cfg[key]=value
            with self.assertRaises(ValueError):validate_config(cfg)
        cfg=fixture();cfg.update(pool_kind='suprnova',pool_host='stratum-us.suprnova.cc',pool_port=3370,account='prl1'+'q'*30,allow_plaintext=True)
        self.assertEqual(validate_config(cfg)['pool_kind'],'suprnova')
        cfg['allow_plaintext']=False
        with self.assertRaises(ValueError):validate_config(cfg)

    def test_paths_are_target_linux_and_outputs_cannot_overwrite_inputs(self):
        for value in ('relative/model.nb','/home/test/../model.nb','C:\\model.nb','/'):
            cfg=fixture();cfg['model']=value
            with self.assertRaises(ValueError):validate_config(cfg)
        cfg=fixture();cfg['validation_gate']=cfg['model']
        with self.assertRaises(ValueError):validate_config(cfg)

    def test_temperature_and_memory_limits_are_preserved(self):
        reading=dict(fan_pwm=255,fan_enabled=1,temperature_C=75,memory_available_MiB=800)
        self.assertIsNone(stop_reason(reading,True))
        for change,expected in [({'temperature_C':76},'TEMPERATURE_76C'),({'fan_pwm':254},'FAN_COMMAND_NOT_FULL'),({'memory_available_MiB':319},'LOW_AVAILABLE_MEMORY')]:
            self.assertEqual(stop_reason(dict(reading,**change),True),expected)
        self.assertEqual(stop_reason(dict(reading,memory_available_MiB=767),False),'LOW_AVAILABLE_MEMORY')

    def test_legacy_service_active_or_unknown_refused_without_control(self):
        from types import SimpleNamespace
        with patch.object(Path,'exists',return_value=True),patch('engine.configuration.subprocess.run') as run:
            for state in ('active','activating','deactivating',''):
                run.return_value=SimpleNamespace(returncode=0,stdout='LoadState=loaded\nActiveState='+state+'\n')
                with self.assertRaises(RuntimeError):check_old_service()
            run.return_value=SimpleNamespace(returncode=0,stdout='LoadState=loaded\nActiveState=inactive\n')
            self.assertEqual(check_old_service(),'inactive')
            for call in run.call_args_list:
                self.assertEqual(call.args[0][1],'show')

    def test_wrong_platform_rejected_before_artifact_access(self):
        with patch('engine.configuration.platform.system',return_value='Windows'):
            with self.assertRaisesRegex(RuntimeError,'Linux AArch64'):preflight(fixture())

if __name__=='__main__':unittest.main()
