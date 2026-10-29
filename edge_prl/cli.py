"""Explicit local commands. Importing the package never starts mining."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import __version__

ROOT = Path(__file__).resolve().parents[1]


def read_config(path: Path) -> dict:
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Configuration is unexpectedly large')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a JSON object')
    from engine.configuration import validate_config
    return validate_config(value, hardware=False)


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Experimental PRL NPU miner; default developer fee is 1% (configurable to 0%).')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Write an unconfigured template; never mine or overwrite a file.')
    init.add_argument('--output', type=Path, default=Path('local/config.json'))
    doctor = commands.add_parser('doctor', help='Validate config; optional read-only device checks.')
    doctor.add_argument('--config', type=Path, required=True)
    doctor.add_argument('--device', action='store_true')
    for name, help_text in [('self-test', 'Offline NPU arithmetic and official-proof gate; no pool connection.'),
                            ('mine', 'Run the bounded foreground supervisor after the device gate passes.'),
                            ('status', 'Read saved observations, without claiming they prove a live process.')]:
        item = commands.add_parser(name, help=help_text)
        item.add_argument('--config', type=Path, required=True)
    economic = commands.add_parser('evaluate', help='Estimate paired CPU-only / dual value from an explicit snapshot.')
    economic.add_argument('input', type=Path)
    bundle = commands.add_parser('bundle', help='Create a source-only review archive; no upload or publishing.')
    bundle.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            template = ROOT / 'examples/config.template.json'
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as handle:
                handle.write(template.read_text(encoding='utf-8'))
            emit({'created':str(args.output.resolve()), 'configured':False, 'mining_started':False})
            return 0
        if args.command == 'evaluate':
            from .economics import evaluate, load_input
            emit(evaluate(load_input(args.input)))
            return 0
        if args.command == 'bundle':
            from .bundle import create_bundle
            emit(create_bundle(ROOT, args.output))
            return 0
        config = read_config(args.config)
        if args.command == 'doctor':
            if args.device:
                from engine.configuration import preflight
                result = preflight(config)
                emit({'config_valid':True, 'device_checked':True, 'checks':result,
                      'mining_started':False, 'pool_acceptance_established':False})
            else:
                emit({'config_valid':True, 'device_checked':False, 'mining_started':False,
                      'developer_fee_percent':config['dev_fee'], 'next_step':'doctor --device on the target A733 board'})
            return 0
        if args.command == 'status':
            result = {'evidence_scope':'Saved snapshots only; they may be stale.', 'snapshots':{}}
            for name in ('miner.json', 'supervisor.json'):
                path = Path(config['runtime_dir']) / name
                if path.exists():
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError('Status snapshot is unexpectedly large')
                    result['snapshots'][name] = json.loads(path.read_text(encoding='utf-8'))
                else:
                    result['snapshots'][name] = None
            emit(result)
            return 0
        module = 'engine.miner' if args.command == 'self-test' else 'engine.runtime_guard'
        command = [sys.executable, '-m', module, '--config', str(args.config.resolve())]
        if args.command == 'self-test':
            command.append('--self-test')
        # The engine owns its child process group and enforces an independent
        # deadline. Never attach this command to another miner's process.
        return subprocess.call(command, cwd=ROOT)
    except (OSError, ValueError, RuntimeError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
