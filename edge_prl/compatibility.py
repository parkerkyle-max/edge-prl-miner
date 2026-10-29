"""Read-only, privacy-limited discovery; detection never authorizes mining.

No vendor library is imported, no device node is opened, and no subprocess or
network request is made. Unknown hardware never falls back to a known backend.
"""
from __future__ import annotations

import json
from pathlib import Path
import platform

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'compatibility/backends.json'
IMPLEMENTED_BACKEND = 'a733-viplite-v2-f32-256'
_SYSTEMS = {'linux', 'windows', 'darwin'}
_ARCHITECTURES = {'aarch64': 'aarch64', 'arm64': 'aarch64', 'x86_64': 'x86_64',
                  'amd64': 'x86_64', 'armv7l': 'arm', 'armv8l': 'arm', 'arm': 'arm'}
_COMPATIBLES = {
    'allwinner,sun60i-a733', 'xunlong,orangepi-4-pro', 'arm,sun60iw2p1',
    'rockchip,rk3588', 'rockchip,rk3588s', 'rockchip,rk3566', 'rockchip,rk3568',
    'rockchip,rk3562',
}
_NODE_PATHS = {
    'vipcore': 'dev/vipcore', 'rknpu': 'dev/rknpu',
    'ascend_davinci': 'dev/davinci0', 'ascend_manager': 'dev/davinci_manager',
    'hailo': 'dev/hailo0', 'coral_pcie': 'dev/apex_0',
    'qcom_cdsp_rpc': 'dev/fastrpc-cdsp',
}
_DRIVERS = {'intel_vpu', 'ivpu', 'amdxdna', 'rknpu', 'hailo_pci', 'apex'}
_ERRORS = {'device_tree_unreadable', 'memory_unreadable', 'driver_metadata_unreadable'}


def load_catalog() -> dict:
    """Read the shipped catalog, not an arbitrary user-selected plugin."""
    with CATALOG_PATH.open('rb') as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise ValueError('Backend catalog exceeds its size limit')
    catalog = json.loads(data)
    entries = catalog.get('backends', [])
    ids = [entry['id'] for entry in entries]
    if catalog.get('schema_version') != 1 or len(ids) != len(set(ids)):
        raise ValueError('Invalid backend catalog')
    implemented = [entry['id'] for entry in entries if entry.get('supported_live') is True]
    if implemented != [IMPLEMENTED_BACKEND]:
        raise ValueError('Catalog does not match the implemented mining backend')
    return catalog


def catalog_report() -> dict:
    """Return catalog claims with their scope; performs no hardware discovery."""
    return load_catalog()


def get_backend(identifier: str) -> dict:
    for entry in load_catalog()['backends']:
        if entry['id'] == identifier:
            return entry
    raise ValueError('Unknown backend identifier')


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open('rb') as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError('Discovery metadata exceeds its size limit')
    return content


def collect_snapshot(root: Path = Path('/'), *, system=None, architecture=None) -> dict:
    """Collect only allowlisted hints. ``root`` enables synthetic filesystem tests.

    Non-Linux platforms are deliberately unprobed; a CPU brand, generic GPU or
    ARM64 operating system is not sufficient evidence of a usable NPU.
    """
    system = str(platform.system() if system is None else system).lower()
    architecture = str(platform.machine() if architecture is None else architecture).lower()
    value = {'system': system if system in _SYSTEMS else 'unknown',
             'architecture': _ARCHITECTURES.get(architecture, 'unknown'),
             'memory_total_kib': None, 'compatible_tokens': [],
             'device_nodes': [], 'drivers': [], 'read_errors': []}
    if value['system'] != 'linux':
        return value
    root = Path(root)
    try:
        raw = _read_bounded(root / 'proc/device-tree/compatible', 4096)
        value['compatible_tokens'] = sorted({part.decode('ascii', 'ignore').lower()
            for part in raw.split(b'\0')} & _COMPATIBLES)
    except FileNotFoundError:
        pass  # Device trees are not expected on every Linux host.
    except (OSError, ValueError):
        value['read_errors'].append('device_tree_unreadable')
    try:
        raw = _read_bounded(root / 'proc/meminfo', 65536).decode('ascii')
        for line in raw.splitlines():
            if line.startswith('MemTotal:'):
                pieces = line.split()
                if len(pieces) != 3 or pieces[2] != 'kB':
                    raise ValueError('Unexpected memory units')
                memory = int(pieces[1])
                if not 0 < memory < 2 ** 50:
                    raise ValueError('Invalid memory amount')
                value['memory_total_kib'] = memory
                break
        if value['memory_total_kib'] is None:
            raise ValueError('Missing memory total')
    except (OSError, UnicodeError, ValueError):
        value['read_errors'].append('memory_unreadable')
    for name, relative in _NODE_PATHS.items():
        try:
            if (root / relative).exists():
                value['device_nodes'].append(name)
        except OSError:
            pass
    # Limit enumeration to fixed slots; read only a recognized driver basename.
    # Never expose PCI addresses, serials, symlink targets or arbitrary strings.
    candidates = [f'sys/class/accel/accel{i}/device/driver' for i in range(16)]
    candidates += [f'sys/class/drm/renderD{i}/device/driver' for i in range(128, 144)]
    drivers = set()
    for relative in candidates:
        try:
            path = root / relative
            if path.is_symlink():
                name = path.resolve(strict=True).name
                if name in _DRIVERS:
                    drivers.add(name)
        except (OSError, RuntimeError):
            value['read_errors'].append('driver_metadata_unreadable')
    value['drivers'] = sorted(drivers)
    value['read_errors'] = sorted(set(value['read_errors']))
    return value


def _known_items(snapshot, field, allowed):
    items = snapshot.get(field, [])
    if not isinstance(items, (list, tuple, set, frozenset)):
        return set()
    return {item for item in items if isinstance(item, str) and item in allowed}


def build_report(snapshot=None) -> dict:
    """Build a shareable JSON report without copying arbitrary input fields."""
    snapshot = collect_snapshot() if snapshot is None else snapshot
    if not isinstance(snapshot, dict):
        raise ValueError('Discovery snapshot must be an object')
    system = snapshot.get('system')
    system = system if isinstance(system, str) and system in _SYSTEMS else 'unknown'
    architecture = snapshot.get('architecture')
    architecture = _ARCHITECTURES.get(architecture, 'unknown') if isinstance(architecture, str) else 'unknown'
    memory = snapshot.get('memory_total_kib')
    memory = memory if type(memory) is int and 0 < memory < 2 ** 50 else None
    compatible = _known_items(snapshot, 'compatible_tokens', _COMPATIBLES)
    nodes = _known_items(snapshot, 'device_nodes', _NODE_PATHS)
    drivers = _known_items(snapshot, 'drivers', _DRIVERS)
    errors = _known_items(snapshot, 'read_errors', _ERRORS)
    # Synthetic snapshots cannot use Linux hints to claim native Windows/macOS
    # discovery that the real collector does not implement.
    if system != 'linux':
        compatible, nodes, drivers = set(), set(), set()
    a733 = ('allwinner,sun60i-a733' in compatible or
            {'xunlong,orangepi-4-pro', 'arm,sun60iw2p1'} <= compatible)
    hints = {
        IMPLEMENTED_BACKEND: (["a733_device_tree"] if a733 else []) + (["vipcore_node"] if 'vipcore' in nodes else []),
        'rockchip-rk3588-rknn': ['rk3588_device_tree'] if compatible & {'rockchip,rk3588', 'rockchip,rk3588s'} else [],
        'rockchip-rk356x-rknn': ['rk356x_device_tree'] if compatible & {'rockchip,rk3562', 'rockchip,rk3566', 'rockchip,rk3568'} else [],
        'ascend-cann': ['ascend_device_node'] if nodes & {'ascend_davinci', 'ascend_manager'} else [],
        'intel-openvino-npu': ['intel_npu_driver'] if drivers & {'intel_vpu', 'ivpu'} else [],
        'amd-ryzen-ai-xdna': ['amdxdna_driver'] if 'amdxdna' in drivers else [],
        'qualcomm-qnn-htp': ['cdsp_rpc_node_only'] if 'qcom_cdsp_rpc' in nodes else [],
        'apple-coreml-ane': [],
        'hailo-hailort': ['hailo_node_or_driver'] if 'hailo' in nodes or 'hailo_pci' in drivers else [],
        'coral-edgetpu': ['coral_pcie_node_or_driver'] if 'coral_pcie' in nodes or 'apex' in drivers else [],
    }
    if 'rknpu' in nodes or 'rknpu' in drivers:
        # The device interface alone cannot distinguish RK3588 from RK356x.
        for backend in ('rockchip-rk3588-rknn', 'rockchip-rk356x-rknn'):
            hints[backend].append('rknpu_interface_family_only')
    entries = []
    for backend in load_catalog()['backends']:
        identifier = backend['id']
        evidence = hints[identifier]
        eligible = False
        reasons = []
        if system != 'linux':
            state = 'unprobed_platform'
            reasons.append('native_device_discovery_not_implemented_on_this_platform')
        elif identifier == IMPLEMENTED_BACKEND:
            if architecture != 'aarch64': reasons.append('requires_linux_aarch64')
            if not a733: reasons.append('exact_a733_device_tree_not_identified')
            if memory is None: reasons.append('memory_unknown')
            elif not 3500000 < memory < 4500000: reasons.append('outside_qualified_4gb_memory_range')
            if 'vipcore' not in nodes: reasons.append('vipcore_device_node_not_observed')
            eligible = not reasons
            state = 'needs_device_validation' if eligible else ('detected_blocked' if evidence else 'not_detected')
            if eligible:
                reasons.append('driver_abi_model_libraries_cooling_exact_proofs_and_pool_still_unverified')
        else:
            state = 'detected_unimplemented' if evidence else 'not_detected'
            reasons.append('no_mining_adapter_implemented')
        entries.append({'backend_id': identifier, 'implementation_status': backend['implementation_status'],
                        'discovery_status': state, 'evidence': evidence, 'reasons': reasons,
                        'eligible_for_device_validation': eligible,
                        'can_mine_now': False, 'runtime_library_loaded': False,
                        'capabilities_required': backend['capabilities_required']})
    return {'schema_version': 1, 'report_type': 'read_only_npu_discovery',
            'scope': 'Allowlisted hints only; not a driver, arithmetic, pool or profitability test.',
            'host': {'system': system, 'architecture': architecture,
                     'memory_total_kib': memory},
            'read_errors': sorted(errors), 'backends': entries,
            'can_mine_now': False, 'network_used': False, 'device_nodes_opened': False,
            'privacy': {'includes_hostname_serial_ip_accounts_or_private_paths': False},
            'limitations': ['No detection is not proof that hardware is absent.',
                            'Generic GPU and accelerator nodes are not identified as an NPU vendor.',
                            'Windows, macOS and USB-only Coral discovery are not implemented.',
                            'Installed interfaces do not prove that an SDK can execute exact PRL work.']}
