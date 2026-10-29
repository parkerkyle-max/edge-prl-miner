import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from edge_prl.bundle import create_bundle, assert_public_text, PUBLIC_DEV_FEE_ADDRESS, LICENSE_FILES


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'source'
        (self.root / 'engine').mkdir(parents=True)
        (self.root / 'engine/miner.py').write_text('# synthetic engine\n')
        self.destination = Path(self.temporary.name) / 'source.tar.gz'

    def test_private_runtime_and_vendor_artifacts_never_packaged(self):
        (self.root / 'local').mkdir()
        (self.root / 'local/config.json').write_text('{"private":true}')
        (self.root / 'engine/model.nb').write_bytes(b'not-redistributable')
        (self.root / 'engine/library.so').write_bytes(b'not-redistributable')
        receipt = create_bundle(self.root, self.destination)
        self.assertTrue(receipt['reread_verified'])
        with tarfile.open(self.destination) as archive:
            self.assertEqual(set(archive.getnames()), {'edge-prl-miner/engine/miner.py', 'edge-prl-miner/SOURCE-MANIFEST.json'})
            manifest = json.load(archive.extractfile('edge-prl-miner/SOURCE-MANIFEST.json'))
            self.assertFalse(manifest['includes_vendor_runtime_or_models'])

    def test_private_address_in_source_refuses_archive(self):
        (self.root / 'engine/miner.py').write_text('address=' + 'prl1q' + 'a' * 58)
        with self.assertRaises(ValueError):
            create_bundle(self.root, self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_archive_never_overwritten(self):
        self.destination.write_bytes(b'keep this')
        with self.assertRaises(FileExistsError):
            create_bundle(self.root, self.destination)
        self.assertEqual(self.destination.read_bytes(), b'keep this')

    def test_original_and_extensionless_dependency_licenses_are_preserved(self):
        (self.root / 'LICENSE').write_text('Synthetic original license')
        for name in LICENSE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic third-party notice')
        create_bundle(self.root, self.destination)
        with tarfile.open(self.destination) as archive:
            names = set(archive.getnames())
            self.assertIn('edge-prl-miner/LICENSE', names)
            for name in LICENSE_FILES:
                self.assertIn('edge-prl-miner/' + name, names)

    def test_only_explicit_public_fee_address_and_approved_files_are_allowed(self):
        address = PUBLIC_DEV_FEE_ADDRESS.encode()
        for name in ('engine/devfee.py', 'docs/DEV-FEE.md', 'edge_prl/bundle.py'):
            assert_public_text(name, address)
            for unexpected in (address + b'q', ('prl1q' + 'a' * 58).encode()):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    assert_public_text(name, unexpected)
        with self.assertRaises(ValueError):
            assert_public_text('examples/config.template.json', address)

    def test_archive_requires_real_engine_source(self):
        (self.root / 'engine/miner.py').unlink()
        (self.root / 'README.md').write_text('Only documentation')
        with self.assertRaises(ValueError):
            create_bundle(self.root, self.destination)


if __name__ == '__main__':
    unittest.main()
