"""Keep public fee/evidence summaries aligned with code; no network or device I/O."""
import json
from pathlib import Path
import re
import unittest
from urllib.parse import urlparse

from edge_prl import __version__
from engine.devfee import CYCLE_SECONDS, FEE_SECONDS, USER_SECONDS

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/parkerkyle-max/edge-prl-miner'
RAW_PREFIX = 'https://raw.githubusercontent.com/parkerkyle-max/edge-prl-miner/main/'


class PublicMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata = json.loads((ROOT / 'project-metadata.json').read_text(encoding='utf-8'))
        cls.template = json.loads((ROOT / 'examples/config.template.json').read_text(encoding='utf-8'))

    def test_fee_facts_match_current_code_and_template(self):
        fee = self.metadata['developer_fee']
        self.assertEqual(fee['default_percent'], self.template['dev_fee'])
        self.assertEqual(fee['config_field'], 'dev_fee')
        self.assertEqual(fee['config_unit'], 'integer_percent')
        self.assertEqual(fee['allowed_values'], [0, 1])
        self.assertEqual(fee['disable_value'], 0)
        self.assertTrue(fee['optional'])
        self.assertTrue(fee['retention_requested'])
        self.assertFalse(fee['retention_is_license_condition'])
        self.assertEqual(fee['cycle_seconds'], CYCLE_SECONDS)
        self.assertEqual(fee['user_seconds_per_complete_cycle'], USER_SECONDS)
        self.assertEqual(fee['developer_window_seconds_per_complete_cycle'], FEE_SECONDS)
        self.assertEqual(FEE_SECONDS * 100, fee['default_percent'] * CYCLE_SECONDS)
        self.assertTrue(fee['startup_and_network_inside_developer_window'])
        self.assertFalse(fee['missed_time_catchup'])
        self.assertFalse(fee['wallet_balance_deduction'])
        self.assertFalse(fee['applies_to_external_cpu_miner'])
        self.assertFalse(fee['guarantees_exact_reward_percentage'])
        self.assertEqual(fee['supported_pool_kinds'], ['suprnova'])
        self.assertEqual(fee['kryptex_required_percent'], 0)

    def test_project_identity_license_and_evidence_are_explicit(self):
        data = self.metadata
        self.assertEqual(data['schema'], 'edge-prl-project-metadata-v1')
        self.assertEqual(data['metadata_version'], 1)
        self.assertEqual(data['software_version'], __version__)
        self.assertEqual(data['repository'], REPOSITORY)
        self.assertEqual(data['maintainer_namespace'], 'parkerkyle-max')
        self.assertEqual(data['license']['original_code_spdx'], 'MIT')
        historical = data['historical_validation']
        self.assertEqual(historical['developer_fee_percent'], 0)
        self.assertEqual(historical['accepted_replies'], 35)
        for key in ('validates_current_fee_scheduler', 'validates_other_npu_families',
                    'establishes_positive_total_income', 'establishes_withdrawal'):
            self.assertIs(historical[key], False)
        self.assertFalse(data['compatibility']['detection_is_mining_support'])
        self.assertFalse(data['compatibility']['all_npu_families_supported'])
        self.assertFalse(data['artifacts']['clone_alone_can_mine'])
        self.assertFalse(data['machine_reading']['crawler_indexing_guaranteed'])

    def test_local_metadata_links_resolve_inside_repository(self):
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.endswith('_path') or key in ('path', 'recipient_source'):
                        path = (ROOT / item.split('#', 1)[0]).resolve()
                        self.assertTrue(path.is_relative_to(ROOT), key)
                        self.assertTrue(path.is_file(), item)
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(self.metadata)

    def test_implemented_backend_summary_matches_catalog_without_promoting_research(self):
        path = ROOT / self.metadata['compatibility']['catalog_path']
        catalog = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(catalog['schema_version'], 1)
        implemented = sorted(row['id'] for row in catalog['backends'] if row['supported_live'])
        self.assertEqual(implemented, sorted(self.metadata['compatibility']['implemented_backend_ids']))
        self.assertTrue(any(not row['supported_live'] for row in catalog['backends']))

    def test_canonical_raw_links_resolve_to_reviewable_local_files(self):
        index = (ROOT / 'llms.txt').read_text(encoding='utf-8')
        urls = re.findall(r'\]\((https://[^)]+)\)', index)
        self.assertGreaterEqual(len(urls), 10)
        for url in urls:
            self.assertTrue(url.startswith(RAW_PREFIX), url)
            relative = url[len(RAW_PREFIX):]
            self.assertNotIn('?', relative)
            self.assertTrue((ROOT / relative).is_file(), url)
        for key in ('metadata_url', 'index_url', 'facts_url'):
            url = self.metadata['machine_reading'][key]
            self.assertEqual(urlparse(url).netloc, 'raw.githubusercontent.com')
            self.assertTrue(url.startswith(RAW_PREFIX))
            self.assertTrue((ROOT / url[len(RAW_PREFIX):]).is_file())


if __name__ == '__main__':
    unittest.main()
