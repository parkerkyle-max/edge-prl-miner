"""Explicit, pinned Linux proof/preparation build. Importing does no work.

The default is offline. Vendor SDKs, NPU models, device access and pool traffic
are outside this command. Outputs must stay in the repository's ignored local/.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TARGETS = {
    'x86_64': ('x86_64-unknown-linux-gnu', 'gcc', 62, False),
    'aarch64': ('aarch64-unknown-linux-gnu', 'aarch64-linux-gnu-gcc', 183, True),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args, *, cwd=None, env=None, timeout=60):
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: ' + str(args[0]) + '\n' + result.stdout[-6000:])
    return result.stdout.strip()


def validate_output(output, root=ROOT):
    root = Path(root).resolve()
    output, allowed = Path(output).resolve(), (root / 'local').resolve()
    if not allowed.is_relative_to(root) or output == allowed or not output.is_relative_to(allowed):
        raise ValueError('Build output must be a new subdirectory of ignored local/')
    if output.exists():
        raise FileExistsError('Refusing to overwrite an existing build directory')
    return output


def validate_elf(path, expected_machine):
    with Path(path).open('rb') as handle:
        header = handle.read(20)
    if len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != expected_machine:
        raise RuntimeError('Build output is not the requested little-endian ELF64 architecture')


def check_hashes(root, hashes):
    root = Path(root).resolve()
    for name, expected in hashes.items():
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file() or sha(target) != expected:
            raise RuntimeError('Pinned source hash mismatch: ' + name)


def manifest(template, official):
    official = Path(official).resolve()
    return template.replace('@PEARL_BLAKE3_PATH@', json.dumps((official / 'pearl-blake3').as_posix())).replace(
        '@ZK_POW_PATH@', json.dumps((official / 'zk-pow').as_posix()))


def prepare_command(cc, target, blake3, output):
    _, _, _, neon = TARGETS[target]
    cdir = Path(blake3) / 'c'
    sources = [HERE / 'prep/prep-single.c'] + [cdir / name for name in
        ('blake3.c', 'blake3_dispatch.c', 'blake3_portable.c')]
    if neon:
        sources.append(cdir / 'blake3_neon.c')
    return [cc, '-O2', '-std=gnu11', '-fPIC', '-fopenmp', '-Wall', '-Wextra', '-shared',
            '-DBLAKE3_NO_SSE2', '-DBLAKE3_NO_SSE41', '-DBLAKE3_NO_AVX2', '-DBLAKE3_NO_AVX512',
            '-DBLAKE3_USE_NEON=' + ('1' if neon else '0'), '-I' + str(cdir),
            *map(str, sources), '-Wl,-z,defs', '-o', str(output)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-source', type=Path, required=True)
    parser.add_argument('--blake3-source', type=Path, required=True, help='Unmodified blake3-1.8.7 crate root')
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rust-toolchain', default='stable', help='An already installed toolchain; exact rustc is checked')
    parser.add_argument('--rustup', default=shutil.which('rustup') or str(Path.home() / '.cargo/bin/rustup'))
    parser.add_argument('--cc', help='Explicit compiler; default gcc or aarch64-linux-gnu-gcc')
    parser.add_argument('--cargo-target-dir', type=Path, help='Optional existing dependency/build cache')
    parser.add_argument('--allow-network', action='store_true', help='Explicitly permit Cargo to fetch locked dependencies')
    parser.add_argument('--check-only', action='store_true', help='Validate inputs and toolchain without compiling or writing')
    args = parser.parse_args(argv)
    if platform.system() != 'Linux':
        raise RuntimeError('Build on Linux or WSL; Windows/macOS native builds are not implemented')
    pins = json.loads((HERE / 'pins.json').read_text())
    check_hashes(HERE, pins['wrapper_source_sha256'])
    official, blake3 = args.official_source.resolve(), args.blake3_source.resolve()
    if run(['git', 'rev-parse', 'HEAD'], cwd=official) != pins['official_commit']:
        raise RuntimeError('Official source commit mismatch')
    if run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=official):
        raise RuntimeError('Official tracked source is modified')
    for relative in ('pearl-blake3/Cargo.toml', 'zk-pow/Cargo.toml', 'plonky2/plonky2/Cargo.toml'):
        if not (official / relative).is_file():
            raise RuntimeError('Official checkout is incomplete: ' + relative)
    check_hashes(blake3, pins['blake3_files'])
    rust = run([args.rustup, 'run', args.rust_toolchain, 'rustc', '--version', '--verbose'])
    rust_fields = dict(line.split(': ', 1) for line in rust.splitlines() if ': ' in line)
    if rust_fields.get('release') != pins['rustc_release'] or rust_fields.get('commit-hash') != pins['rustc_commit_hash']:
        raise RuntimeError('Installed Rust compiler differs from the pinned native-build toolchain')
    triple, default_cc, elf_machine, _ = TARGETS[args.target]
    cc = args.cc or ('gcc' if platform.machine().lower() in ('aarch64', 'arm64') and args.target == 'aarch64' else default_cc)
    compiler = run([cc, '--version'])
    output = validate_output(args.output)
    receipt = {'schema': 'edge-prl-native-build-receipt-v1',
               'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'target': args.target, 'target_triple': triple, 'official_commit': pins['official_commit'],
               'official_tracked_source_clean': True, 'input_hashes_verified': True,
               'rustc': rust, 'cc': compiler, 'network_allowed': args.allow_network,
               'build_script_sha256': sha(__file__), 'pins_sha256': sha(HERE / 'pins.json'),
               'proof_cache_abi_included': True, 'npu_model_or_vendor_sdk_included': False,
               'bit_identical_binary_reproduction_established': False,
               'hardware_or_pool_acceptance_established': False}
    if args.check_only:
        print(json.dumps(dict(receipt, compiled=False), indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=False)
    proof = output / 'proof'
    shutil.copytree(HERE / 'proof', proof)
    (proof / 'Cargo.toml').write_text(manifest((proof / 'Cargo.toml.in').read_text(), official), encoding='utf-8')
    libdir = output / 'lib'
    libdir.mkdir()
    cargo_target = (args.cargo_target_dir or output / 'cargo-target').resolve()
    env = dict(os.environ, CARGO_TARGET_DIR=str(cargo_target), CARGO_BUILD_JOBS='2',
               RUSTC_BOOTSTRAP='1', OMP_NUM_THREADS='1', OMP_THREAD_LIMIT='1', RAYON_NUM_THREADS='1')
    env['CARGO_TARGET_' + triple.upper().replace('-', '_') + '_LINKER'] = cc
    prep_args = prepare_command(cc, args.target, blake3, libdir / 'libpearl_prep.so')
    cargo_args = [args.rustup, 'run', args.rust_toolchain, 'cargo', 'build', '--release', '--locked', '--target', triple]
    if not args.allow_network:
        cargo_args.append('--offline')
    (output / 'prep-build.log').write_text(run(prep_args, env=env, timeout=180) + '\n', encoding='utf-8')
    (output / 'proof-build.log').write_text(run(cargo_args, cwd=proof, env=env, timeout=900) + '\n', encoding='utf-8')
    shutil.copyfile(cargo_target / triple / 'release/libpearl_edge_proof.so', libdir / 'libpearl_edge_proof.so')
    receipt['commands'] = {'prep': prep_args, 'proof': list(map(str, cargo_args))}
    receipt['outputs'] = {}
    for name in ('libpearl_prep.so', 'libpearl_edge_proof.so'):
        path = libdir / name
        validate_elf(path, elf_machine)
        receipt['outputs'][name] = {'sha256': sha(path), 'bytes': path.stat().st_size, 'elf_machine': elf_machine}
    # Ensure generated dependencies retain the shipped lock, and recheck the
    # source after compilation before declaring a completed build.
    if (proof / 'Cargo.lock').read_bytes() != (HERE / 'proof/Cargo.lock').read_bytes():
        raise RuntimeError('Cargo modified the pinned lock file')
    check_hashes(HERE, pins['wrapper_source_sha256'])
    check_hashes(blake3, pins['blake3_files'])
    if run(['git', 'rev-parse', 'HEAD'], cwd=official) != pins['official_commit'] or run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=official):
        raise RuntimeError('Official source changed during compilation')
    receipt.update(compiled=True, library_directory=str(libdir))
    destination = output / 'build-receipt.json'
    destination.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if json.loads(destination.read_text()) != receipt:
        raise RuntimeError('Build receipt reread failed')
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
