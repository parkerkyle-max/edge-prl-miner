"""Discovery/selection cannot silently activate unqualified hardware."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from engine.backend.selection import require_backend, DEFAULT_BACKEND_ID
from engine.configuration import validate_config
from test_engine_configuration import fixture


class BackendSelectionTests(unittest.TestCase):
    def test_old_explicit_configs_use_only_existing_backend(self):
        cfg = fixture()
        self.assertNotIn('backend_id', cfg)
        result = validate_config(cfg)
        self.assertEqual(result['backend_id'], DEFAULT_BACKEND_ID)
        self.assertNotIn('backend_id', cfg)

    def test_unimplemented_backend_fails_before_hardware_reads(self):
        for backend in ('auto', 'cpu', 'some-npu', '', True, None, ['a733']):
            with self.subTest(backend=backend), patch('pathlib.Path.read_bytes', side_effect=AssertionError('device read')):
                with self.assertRaisesRegex(ValueError, 'No qualified mining implementation'):
                    validate_config(dict(fixture(), backend_id=backend), hardware=True)

    def test_fixed_scan_contract_matches_actual_adapter_geometry(self):
        from engine.backend.batch_backend import candidate_offsets
        contract = require_backend()
        self.assertEqual(len(candidate_offsets()), contract.output_hashes_per_scan)
        self.assertEqual(contract.complete_macs_per_scan,
                         contract.scan_width ** 2 * contract.partial_inner_dimension * contract.partial_count)
        self.assertFalse(contract.cpu_matmul_fallback_allowed)
        self.assertTrue(contract.fresh_device_proof_gate_required)

    def test_template_explicitly_selects_executable_backend(self):
        cfg = json.loads((Path(__file__).resolve().parents[1] / 'examples/config.template.json').read_text())
        self.assertEqual(require_backend(cfg['backend_id']).backend_id, DEFAULT_BACKEND_ID)
