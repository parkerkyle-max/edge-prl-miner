"""Explicit target configuration and read-only A733 checks; no device writes."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import subprocess

MODEL_SHA256 = 'e0ad42a6e40b4e675d95bce537c9ca45bd1bb984d23e45b4bbf5d3fd16537e93'
NATIVE_LIBRARIES = ('libprl_npu.so', 'libpearl_prep.so', 'libpearl_edge_proof.so')
KRYPTO_POOL_HOSTS = frozenset(('prl-us.kryptex.network', 'prl.kryptex.network'))
SUPRNOVA_HOSTS = frozenset(('prl.suprnova.cc', 'stratum-us.suprnova.cc', 'stratum-eu2.suprnova.cc', 'stratum-apac.suprnova.cc'))
PATH_FIELDS = ('model','libdir','runtime_dir','log_dir','validation_gate','fan_pwm_path','fan_enable_path')

def compatible_is_a733(compatible):
    """Match exact SoC naming or the vendor's jointly declared board/SoC pair.

    Orange Pi's official sun60i-a733-orangepi-4-pro.dts declares board A733
    together with both strings below; sun60 prefixes alone are not sufficient.
    See docs/ENGINE.md for the primary vendor source.
    """
    values = {value.lower() for value in compatible if isinstance(value, bytes)}
    return (b'allwinner,sun60i-a733' in values or
            {b'xunlong,orangepi-4-pro', b'arm,sun60iw2p1'}.issubset(values))

def validate_config(cfg, hardware=False):
    """Validate a Linux target config without opening paths unless hardware=True."""
    if not isinstance(cfg, dict):
        raise ValueError('Configuration must be a JSON object')
    result = dict(cfg)
    if result.get('device_confirmed') is not True:
        raise ValueError('device_confirmed must explicitly be true after identifying the target')
    if not isinstance(result.get('hostname'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,252}', result['hostname']):
        raise ValueError('Explicit target hostname required')
    if type(result.get('dev_fee')) is not int or result['dev_fee'] not in (0, 1):
        raise ValueError('dev_fee must be integer percent 0 or 1')
    if result.get('npu_variant') != 'f32-256' or type(result.get('npu_width')) is not int or result['npu_width'] != 256:
        raise ValueError('Only f32-256 width 256 is supported')
    if result.get('model_sha256') != MODEL_SHA256:
        raise ValueError('Expected the pinned exact f32-256 model SHA256')
    profile = result.setdefault('backend_profile', 'cached-neon-a')
    if profile != 'cached-neon-a':
        raise ValueError('Only cached-neon-a is supported')
    if type(result.get('cpu_threads')) is not int or result['cpu_threads'] != 1:
        raise ValueError('The verified preparation backend requires cpu_threads=1')
    affinity = result.get('cpu_affinity')
    if not isinstance(affinity, list) or not affinity or len(affinity) > 8 or any(type(x) is not int or not 0 <= x < 256 for x in affinity) or len(set(affinity)) != len(affinity):
        raise ValueError('Explicit unique cpu_affinity indices required')
    duration = result.get('max_runtime_seconds')
    if type(duration) is not int or not 1 <= duration <= 2400:
        raise ValueError('max_runtime_seconds must be an integer from 1 to 2400')
    for field in PATH_FIELDS:
        value = result.get(field)
        if not isinstance(value, str) or '\x00' in value or '\\' in value or not PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or value == '/':
            raise ValueError(field + ' must be an explicit absolute Linux path without traversal')
        result[field] = str(PurePosixPath(value))
    if result['fan_pwm_path'] == result['fan_enable_path']:
        raise ValueError('Fan PWM and enable paths must differ')
    for field in ('fan_pwm_path','fan_enable_path'):
        if not result[field].startswith('/sys/'):
            raise ValueError(field + ' must identify a sysfs attribute')
    protected = {result['model'], *(str(PurePosixPath(result['libdir'])/name) for name in NATIVE_LIBRARIES)}
    if result['validation_gate'] in protected:
        raise ValueError('Validation output must not overwrite an input artifact')
    if not isinstance(result.get('worker'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',result['worker']):
        raise ValueError('Explicit bounded worker name required')
    kind, host, port = result.get('pool_kind'), result.get('pool_host'), result.get('pool_port')
    if type(port) is not int or type(result.get('allow_plaintext')) is not bool:
        raise ValueError('Explicit integer pool_port and boolean allow_plaintext required')
    account = result.get('account')
    if kind == 'kryptex':
        if result['dev_fee']:
            raise ValueError('Kryptex requires dev_fee=0; the fee implementation never downgrades TLS or changes pools')
        if host not in KRYPTO_POOL_HOSTS or port != 8048 or result['allow_plaintext']:
            raise ValueError('Kryptex requires an approved TLS host on port 8048')
        if not isinstance(account,str) or not re.fullmatch(r'krx[A-Za-z0-9]{1,125}',account):
            raise ValueError('A user-supplied Kryptex mining username is required')
    elif kind == 'suprnova':
        if host not in SUPRNOVA_HOSTS or port != 3370 or result['allow_plaintext'] is not True:
            raise ValueError('Suprnova requires official port 3370 and explicit plaintext opt-in')
        if not isinstance(account,str) or not re.fullmatch(r'prl1[023456789acdefghjklmnpqrstuvwxyz]{20,120}',account):
            raise ValueError('A user-supplied public PRL address is required')
    else:
        raise ValueError('pool_kind must be kryptex or suprnova')
    if hardware:
        preflight(result)
    return result

def snapshot(cfg):
    temperatures = [int(p.read_text()) / 1000 for p in Path('/sys/class/thermal').glob('thermal_zone*/temp')]
    if not temperatures or any(not -20 <= t <= 150 for t in temperatures):
        raise RuntimeError('Missing or invalid thermal sensors')
    memory = {line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split()) >= 2 and line.split()[1].isdigit()}
    return {'temperature_C':max(temperatures),'memory_available_MiB':memory['MemAvailable']/1024,
            'memory_total_kib':memory['MemTotal'], 'fan_pwm':int(Path(cfg['fan_pwm_path']).read_text()),
            'fan_enabled':int(Path(cfg['fan_enable_path']).read_text())}

def stop_reason(reading, active):
    if reading['fan_pwm'] != 255 or reading['fan_enabled'] != 1:
        return 'FAN_COMMAND_NOT_FULL'
    if reading['temperature_C'] >= 76:
        return 'TEMPERATURE_76C'
    if reading['memory_available_MiB'] < (320 if active else 768):
        return 'LOW_AVAILABLE_MEMORY'
    return None

def check_old_service():
    """Refuse existing legacy NPU mining; never stop or reconfigure it."""
    systemctl = Path('/usr/bin/systemctl')
    if not systemctl.exists():
        return 'systemctl_absent'
    checked = subprocess.run([str(systemctl),'show','opi-prl-npu.service','--property=LoadState','--property=ActiveState'],capture_output=True,text=True,timeout=5)
    states = dict(line.split('=',1) for line in checked.stdout.splitlines() if '=' in line)
    if states.get('ActiveState') not in ('inactive','failed'):
        if states.get('LoadState') == 'not-found':
            return 'not_installed'
        raise RuntimeError('Existing opi-prl-npu.service is active or its state is unknown; stop it separately before using this engine')
    if checked.returncode != 0:
        raise RuntimeError('Cannot verify legacy NPU service state')
    return states['ActiveState']

def preflight(cfg):
    """Read-only device checks. Does not load native libraries or write fan PWM."""
    cfg = validate_config(cfg)
    if platform.system() != 'Linux' or platform.machine().lower() not in ('aarch64','arm64'):
        raise RuntimeError('Only Linux AArch64 targets are supported')
    if socket.gethostname() != cfg['hostname']:
        raise RuntimeError('Configured target hostname does not match this device')
    compatible = Path('/proc/device-tree/compatible').read_bytes().lower().split(b'\x00')
    if not compatible_is_a733(compatible):
        raise RuntimeError('Device tree does not identify an Allwinner A733')
    reading = snapshot(cfg)
    if not 3500000 < reading['memory_total_kib'] < 4500000:
        raise RuntimeError('This initial engine is qualified only for the 4GB A733 board')
    if not set(cfg['cpu_affinity']).issubset(os.sched_getaffinity(0)):
        raise RuntimeError('Requested CPU affinity is not available to this process')
    reason = stop_reason(reading,False)
    if reason:
        raise RuntimeError(reason)
    if hashlib.sha256(Path(cfg['model']).read_bytes()).hexdigest() != MODEL_SHA256:
        raise RuntimeError('Pinned model file hash mismatch')
    libraries = []
    for name in NATIVE_LIBRARIES:
        path = Path(cfg['libdir']) / name
        with path.open('rb') as handle:
            header = handle.read(20)
        if len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20],'little') != 183:
            raise RuntimeError('Expected little-endian ELF64 AArch64 library: '+name)
        libraries.append(str(path))
    legacy = check_old_service()
    return {'status':'PASS','checks':['explicit_config','linux_aarch64','hostname','a733_device_tree','4gb_memory','available_cpu_affinity','temperature_memory_fan_command','fixed_model_sha256','arm64_native_library_headers','legacy_npu_service_inactive'],
            'reading':reading,'libraries':libraries,'legacy_service':legacy,'fan_rotation_verified':False,
            'native_linking_verified':False,'hardware_self_test_required':True}
