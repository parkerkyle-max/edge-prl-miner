"""Synthetic discovery and support-boundary tests; no NPU runtime is loaded."""
import copy
import ctypes
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from edge_prl.compatibility import (IMPLEMENTED_BACKEND, build_report,
                                    catalog_report, collect_snapshot, get_backend)


def qualified_hints():
    return {'system': 'linux', 'architecture': 'aarch64', 'memory_total_kib': 4000000,
            'compatible_tokens': ['xunlong,orangepi-4-pro', 'arm,sun60iw2p1'],
            'device_nodes': ['vipcore'], 'drivers': [], 'read_errors': []}


def entry(report, backend=IMPLEMENTED_BACKEND):
    return next(row for row in report['backends'] if row['backend_id'] == backend)


class CompatibilityTests(unittest.TestCase):
    def test_catalog_has_only_one_implemented_backend_and_explicit_research(self):
        catalog = catalog_report()
        self.assertEqual(len(catalog['backends']), 10)
        implemented = {r['id'] for r in catalog['backends'] if r['supported_live']}
        self.assertEqual(implemented, {IMPLEMENTED_BACKEND})
        from engine.backend.selection import SUPPORTED_BACKEND_IDS
        self.assertEqual(implemented, set(SUPPORTED_BACKEND_IDS))
        for row in catalog['backends']:
            self.assertTrue(row['capabilities_required'])
            self.assertTrue(all(url.startswith('https://') for url in row['primary_sources']))
            if not row['supported_live']:
                self.assertEqual(row['implementation_status'], 'research_only')
        with self.assertRaises(ValueError): get_backend('arbitrary-plugin-name')

    def test_complete_a733_hints_still_do_not_authorize_mining(self):
        original = qualified_hints()
        snapshot = copy.deepcopy(original)
        report = build_report(snapshot)
        row = entry(report)
        self.assertEqual(row['discovery_status'], 'needs_device_validation')
        self.assertTrue(row['eligible_for_device_validation'])
        self.assertFalse(row['can_mine_now'])
        self.assertFalse(report['can_mine_now'])
        self.assertFalse(row['runtime_library_loaded'])
        self.assertEqual(snapshot, original)

    def test_exact_device_pair_and_four_gb_scope_cannot_be_relaxed(self):
        for compatible in (['xunlong,orangepi-4-pro'], ['arm,sun60iw2p1'],
                           ['arm,sun60iw2p2'], ['allwinner,sun60i-a733-other']):
            snapshot = qualified_hints(); snapshot['compatible_tokens'] = compatible
            row = entry(build_report(snapshot))
            self.assertFalse(row['eligible_for_device_validation'])
            self.assertIn('exact_a733_device_tree_not_identified', row['reasons'])
        for memory in (None, True, 3500000, 4500000, 8000000, 12000000):
            snapshot = qualified_hints(); snapshot['memory_total_kib'] = memory
            self.assertFalse(entry(build_report(snapshot))['eligible_for_device_validation'])
        for architecture in ('x86_64', 'arm', 'unknown'):
            snapshot = qualified_hints(); snapshot['architecture'] = architecture
            self.assertFalse(entry(build_report(snapshot))['eligible_for_device_validation'])

    def test_known_soc_without_device_interface_reports_blocker(self):
        snapshot = qualified_hints(); snapshot['device_nodes'] = []
        row = entry(build_report(snapshot))
        self.assertEqual(row['discovery_status'], 'detected_blocked')
        self.assertIn('vipcore_device_node_not_observed', row['reasons'])

    def test_other_vendor_hints_never_enable_mining_or_reuse_a733(self):
        cases = [('rockchip-rk3588-rknn', {'compatible_tokens': ['rockchip,rk3588']}),
                 ('rockchip-rk356x-rknn', {'compatible_tokens': ['rockchip,rk3566']}),
                 ('ascend-cann', {'device_nodes': ['ascend_davinci']}),
                 ('intel-openvino-npu', {'drivers': ['ivpu']}),
                 ('amd-ryzen-ai-xdna', {'drivers': ['amdxdna']}),
                 ('qualcomm-qnn-htp', {'device_nodes': ['qcom_cdsp_rpc']}),
                 ('hailo-hailort', {'device_nodes': ['hailo']}),
                 ('coral-edgetpu', {'device_nodes': ['coral_pcie']})]
        for backend, hints in cases:
            with self.subTest(backend=backend):
                report = build_report(dict(system='linux', architecture='aarch64', **hints))
                self.assertEqual(entry(report, backend)['discovery_status'], 'detected_unimplemented')
                self.assertFalse(any(row['eligible_for_device_validation'] for row in report['backends']))
                self.assertFalse(any(row['can_mine_now'] for row in report['backends']))

    def test_generic_gpu_and_accelerator_names_do_not_identify_npu(self):
        report = build_report({'system': 'linux', 'architecture': 'x86_64',
                               'drivers': ['nvidia', 'amdgpu', 'i915', 'accel'],
                               'device_nodes': ['renderD128', 'accel0']})
        self.assertTrue(all(row['discovery_status'] == 'not_detected' for row in report['backends']))

    def test_nonlinux_is_unprobed_not_absent_even_with_synthetic_linux_hints(self):
        for system in ('windows', 'darwin', 'unknown'):
            snapshot = qualified_hints(); snapshot['system'] = system
            report = build_report(snapshot)
            self.assertTrue(all(row['discovery_status'] == 'unprobed_platform' for row in report['backends']))
            self.assertFalse(any(row['evidence'] for row in report['backends']))
            self.assertFalse(any(row['eligible_for_device_validation'] for row in report['backends']))

    def test_report_drops_arbitrary_private_fields_and_errors(self):
        marker = 'PRIVATE_SENTINEL_NOT_FOR_PUBLIC_REPORT'
        snapshot = qualified_hints()
        snapshot.update(hostname=marker, serial=marker, ip=marker, private_path=marker)
        snapshot['compatible_tokens'].append(marker)
        snapshot['drivers'] = [marker]
        snapshot['read_errors'] = [marker, 'memory_unreadable']
        report = build_report(snapshot)
        self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(report['read_errors'], ['memory_unreadable'])
        self.assertTrue(all(not row['runtime_library_loaded'] for row in report['backends']))

    def test_synthetic_filesystem_has_bounded_metadata_and_no_active_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'proc/device-tree').mkdir(parents=True)
            (root / 'proc/device-tree/compatible').write_bytes(b'allwinner,sun60i-a733\0private-board-label\0')
            (root / 'proc/meminfo').write_text('MemTotal: 4000000 kB\n')
            (root / 'dev').mkdir(); (root / 'dev/vipcore').write_bytes(b'NEVER OPEN THIS DEVICE')
            with patch.object(ctypes, 'CDLL', side_effect=AssertionError('No runtime loading')), \
                 patch.object(socket, 'socket', side_effect=AssertionError('No network')), \
                 patch.object(subprocess, 'run', side_effect=AssertionError('No child process')):
                snapshot = collect_snapshot(root, system='Linux', architecture='aarch64')
                report = build_report(snapshot)
            self.assertTrue(entry(report)['eligible_for_device_validation'])
            self.assertNotIn('private-board-label', json.dumps(snapshot))
            (root / 'proc/device-tree/compatible').write_bytes(b'x' * 4097)
            (root / 'proc/meminfo').write_bytes(b'MemTotal: nonsense kB\n')
            snapshot = collect_snapshot(root, system='Linux', architecture='aarch64')
            self.assertEqual(snapshot['read_errors'], ['device_tree_unreadable', 'memory_unreadable'])
            self.assertFalse(entry(build_report(snapshot))['eligible_for_device_validation'])

    def test_native_nonlinux_probe_does_not_read_linux_files(self):
        with patch('edge_prl.compatibility._read_bounded', side_effect=AssertionError('No Linux reads')):
            snapshot = collect_snapshot(system='Windows', architecture='AMD64')
        self.assertEqual(snapshot['architecture'], 'x86_64')
        self.assertEqual(snapshot['device_nodes'], [])
        self.assertEqual(entry(build_report(snapshot))['discovery_status'], 'unprobed_platform')


if __name__ == '__main__':
    unittest.main()
