"""Source-only review packaging with an allowlist and personal-data checks."""
from __future__ import annotations
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

ROOT_FILES = {'README.md', 'LICENSE', 'LICENSE-STATUS.md', 'THIRD-PARTY-NOTICES.md', '.gitignore', '.gitattributes',
              'llms.txt', 'project-metadata.json', 'AGENTS.md'}
LICENSE_FILES = {
    'engine/licenses/ASCEND-FORK-LICENSE.txt',
    'engine/licenses/PEARL-OFFICIAL-LICENSE.txt',
    'engine/licenses/BLAKE3-LICENSE_A2',
    'engine/licenses/BLAKE3-LICENSE_A2LLVM',
    'engine/licenses/BLAKE3-LICENSE_CC0',
}
PUBLIC_DEV_FEE_ADDRESS = 'prl1pwjessemugthkad9hkttcx2rvenux2q0w0xcah2j0a7zfrycjhz5qhn7apw'
PUBLIC_ADDRESS_FILES = {'engine/devfee.py', 'docs/DEV-FEE.md', 'edge_prl/bundle.py'}
BLOCKED_SUFFIXES = {'.so', '.dll', '.exe', '.nb', '.db', '.dpapi', '.prlbackup', '.pem', '.key', '.pyc'}


def allowed(relative: Path) -> bool:
    name = relative.as_posix()
    if name in ROOT_FILES or name in LICENSE_FILES or name in {'.github/workflows/tests.yml',
            '.github/ISSUE_TEMPLATE/npu-support.yml', 'compatibility/backends.json',
            'benchmarks/a733-packed-20261029.json'}:
        return True
    if relative.suffix.lower() in BLOCKED_SUFFIXES or '__pycache__' in relative.parts:
        return False
    parts = relative.parts
    if len(parts) < 2:
        return False
    if parts[0] in {'edge_prl', 'tests'}:
        return relative.suffix == '.py'
    if parts[0] == 'engine':
        return (relative.suffix in {'.py', '.c', '.h'} or
                name == 'engine/proof/host-reference.json' or
                (parts[1] == 'licenses' and relative.suffix in {'.txt', '.md'}))
    if parts[0] == 'docs':
        return relative.suffix == '.md'
    return name in {'examples/config.template.json', 'examples/economics.synthetic.json'}


def assert_public_text(name: str, data: bytes):
    text = data.decode('utf-8-sig')
    # The owner explicitly publishes this receiving address for the developer
    # fee. This is not a blanket exception for wallet or operator addresses.
    if name in PUBLIC_ADDRESS_FILES:
        text = re.sub(re.escape(PUBLIC_DEV_FEE_ADDRESS) + r'(?![a-zA-Z0-9])',
                      'PUBLIC_DEVELOPER_FEE_RECIPIENT', text)
    patterns = (
        r'krx[A-Za-z0-9]{7,}', r'opi-zero3w-0\d', r'192\.168\.\d+\.\d+',
        r'[A-Za-z]:[/\\]+Users[/\\]+[^/\\\s]+', r'PearlNpu(?:Test)Wallet',
        r'prl1[023456789acdefghjklmnpqrstuvwxyz]{40,}',
        r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    )
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            raise ValueError('Personal data or key material detected in ' + name)


def create_bundle(root: Path, destination: Path) -> dict:
    root = root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError('Refusing to overwrite the archive')
    inventory = []
    content = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not path.is_file() or not allowed(relative):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Source symlinks are not packaged')
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Unexpectedly large source file: ' + relative.as_posix())
        data = path.read_bytes()
        assert_public_text(relative.as_posix(), data)
        inventory.append({'path':relative.as_posix(), 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
        content.append((relative.as_posix(), data))
    if not inventory or not any(row['path'] == 'engine/miner.py' for row in inventory):
        raise ValueError('Source bundle is incomplete: engine/miner.py is required')
    manifest = {'schema':'edge-prl-source-review-v1', 'created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'scope':'Local review source archive; not a public release or hardware acceptance.',
                'includes_vendor_runtime_or_models':False, 'files':inventory}
    manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, 'x:gz') as archive:
        for name, data in content + [('SOURCE-MANIFEST.json', manifest_bytes)]:
            info = tarfile.TarInfo('edge-prl-miner/' + name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    # Independently reread archive bytes and compare the full inventory.
    with tarfile.open(destination, 'r:gz') as archive:
        expected = {'edge-prl-miner/' + name for name, _ in content} | {'edge-prl-miner/SOURCE-MANIFEST.json'}
        if set(archive.getnames()) != expected:
            raise RuntimeError('Bundle inventory reread failed')
        for row in inventory:
            data = archive.extractfile('edge-prl-miner/' + row['path']).read()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise RuntimeError('Bundle hash reread failed')
    return {'archive':str(destination), 'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'files':len(inventory), 'reread_verified':True, 'published':False}
