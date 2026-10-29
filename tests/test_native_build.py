"""Offline guard tests for the explicit native source build command."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_build', ROOT / 'native-build/build.py')
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class NativeBuildTests(unittest.TestCase):
    def test_shipped_source_hashes_and_license_notices(self):
        pins = json.loads((build.HERE / 'pins.json').read_text())
        build.check_hashes(build.HERE, pins['wrapper_source_sha256'])
        for name in pins['license_notices']:
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertEqual(len(pins['official_commit']), 40)
        self.assertEqual(len(pins['prep_upstream_commit']), 40)

    def test_changed_source_and_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            file = source / 'code.c'
            file.write_text('first')
            expected = build.sha(file)
            build.check_hashes(source, {'code.c': expected})
            file.write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
                build.check_hashes(source, {'code.c': expected})
            (root / 'outside').write_text('first')
            with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
                build.check_hashes(source, {'../outside': build.sha(root / 'outside')})

    def test_output_must_be_new_local_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / 'local'
            local.mkdir()
            self.assertEqual(build.validate_output(local / 'new', root), (local / 'new').resolve())
            for output in (root / 'outside', local, local / '../outside'):
                with self.assertRaises(ValueError):
                    build.validate_output(output, root)
            (local / 'existing').mkdir()
            with self.assertRaises(FileExistsError):
                build.validate_output(local / 'existing', root)

    def test_output_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, outside = root / 'repo', root / 'outside'
            repo.mkdir(); outside.mkdir()
            try:
                (repo / 'local').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks not available')
            with self.assertRaises(ValueError):
                build.validate_output(repo / 'local/build', repo)

    def test_elf_architecture_and_endianness_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / 'lib.so'
            header = bytearray(20)
            header[:6] = b'\x7fELF\x02\x01'
            header[18:20] = (183).to_bytes(2, 'little')
            library.write_bytes(header)
            build.validate_elf(library, 183)
            with self.assertRaises(RuntimeError):
                build.validate_elf(library, 62)
            header[5] = 2
            library.write_bytes(header)
            with self.assertRaises(RuntimeError):
                build.validate_elf(library, 183)
            library.write_bytes(b'ELF')
            with self.assertRaises(RuntimeError):
                build.validate_elf(library, 183)

    def test_prep_architecture_has_no_silent_neon_fallback(self):
        arm = build.prepare_command('cross cc', 'aarch64', Path('/source with spaces'), Path('/output/lib.so'))
        host = build.prepare_command('gcc', 'x86_64', Path('/source'), Path('/output/lib.so'))
        self.assertIn('-DBLAKE3_USE_NEON=1', arm)
        self.assertIn('-DBLAKE3_USE_NEON=0', host)
        self.assertTrue(any(Path(name).name == 'blake3_neon.c' for name in arm))
        self.assertFalse(any(Path(name).name == 'blake3_neon.c' for name in host))
        self.assertEqual(arm[0], 'cross cc')
        self.assertIn('-I' + str(Path('/source with spaces') / 'c'), arm)

    def test_manifest_paths_are_toml_quoted(self):
        template = 'a={path=@PEARL_BLAKE3_PATH@}\nb={path=@ZK_POW_PATH@}'
        result = build.manifest(template, Path('/source with spaces'))
        self.assertNotIn('@PEARL', result)
        self.assertNotIn('@ZK', result)
        self.assertIn('/pearl-blake3"', result)
        self.assertIn('/zk-pow"', result)


if __name__ == '__main__':
    unittest.main()
