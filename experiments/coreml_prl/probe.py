"""Bounded macOS Core ML exact-matmul feasibility experiment, never mining.

The parent process imposes a deadline on its own worker process group. Imports
are inert; optional SDK dependencies are imported only inside the worker.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

from .arithmetic import plan_evidence


def save_json(path, value):
    encoded = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('xb') as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if path.read_bytes() != encoded:
        raise RuntimeError('Receipt reread failed')


def tree_hashes(directory):
    result = {}
    for file in sorted(directory.rglob('*')):
        if file.is_file():
            if file.is_symlink():
                raise RuntimeError('Unexpected symlink in generated model')
            result[file.relative_to(directory).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def numpy_digits(values, np):
    values = values.astype(np.int64)
    low = (values + 4) % 8 - 4
    carry = (values - low) // 8
    middle = (carry + 4) % 8 - 4
    high = (carry - middle) // 8
    return (low, middle, high)


def inputs_and_expected(a, b, np, layout='batched-matmul'):
    da, db = numpy_digits(a, np), numpy_digits(b, np)
    left = np.stack([da[i] for i in range(3) for _ in range(3)]).astype(np.float16)[None, ...]
    right = np.stack([db[j] for _ in range(3) for j in range(3)]).astype(np.float16)[None, ...]
    products = np.stack([da[i] @ db[j] for i in range(3) for j in range(3)])[None, ...]
    if layout == 'channels-first':
        left, right = left[0, :, :, None, :], right[0, :, :, None, :]
    elif layout != 'batched-matmul':
        raise ValueError('Unknown model layout')
    return {'a': left, 'b': right}, products, a.astype(np.int64) @ b.astype(np.int64)


def check_outputs(raw, expected_digits, expected_matrix, np, layout='batched-matmul'):
    values = np.asarray(raw)
    if layout == 'channels-first':
        if values.ndim != 4 or values.shape[2] != 1:
            raise RuntimeError('Invalid channels-first prediction shape')
        values = values[:, :, 0, :][None, ...]
    elif layout != 'batched-matmul':
        raise ValueError('Unknown model layout')
    if values.shape != expected_digits.shape or not np.isfinite(values).all():
        raise RuntimeError('Invalid prediction shape or nonfinite value')
    if not np.equal(values, np.rint(values)).all() or np.max(np.abs(values)) > 2048:
        raise RuntimeError('Digit products are not exact bounded integers')
    integers = values.astype(np.int64)
    if not np.array_equal(integers, expected_digits):
        raise RuntimeError('Core ML digit products differ from independent integer oracle')
    reconstructed = np.zeros(expected_matrix.shape, dtype=np.int64)
    for i, wi in enumerate((1, 8, 64)):
        for j, wj in enumerate((1, 8, 64)):
            reconstructed += integers[0, i * 3 + j] * wi * wj
    if not np.array_equal(reconstructed, expected_matrix):
        raise RuntimeError('Signed INT8 matrix reconstruction differs from oracle')
    return hashlib.sha256(reconstructed.astype('<i8').tobytes()).hexdigest()


def compute_plan(model, compute_units, ct):
    rows = []
    try:
        plan = ct.models.compute_plan.MLComputePlan.load_from_path(
            path=model.get_compiled_model_path(), compute_units=compute_units)
        program = plan.model_structure.program
        if program is None:
            raise RuntimeError('Expected ML Program structure')
        def visit(block):
            for operation in block.operations:
                usage = plan.get_compute_device_usage_for_mlprogram_operation(operation)
                rows.append({'operator': operation.operator_name,
                             'preferred': type(usage.preferred_compute_device).__name__ if usage else None,
                             'supported': [type(x).__name__ for x in usage.supported_compute_devices] if usage else []})
                for child in operation.blocks:
                    visit(child)
        for function in program.functions.values():
            visit(function.block)
        return {'status': 'AVAILABLE', 'operations': rows, **plan_evidence(rows)}
    except Exception as exc:
        return {'status': 'UNAVAILABLE', 'error_type': type(exc).__name__, 'operations': rows,
                **plan_evidence([])}


def worker(args):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This experiment requires native Apple Silicon macOS')
    if os.getppid() != int(os.environ.get('EDGE_PRL_COREML_PARENT', '0')):
        raise RuntimeError('Worker requires its owning bounded parent')
    import numpy as np
    import coremltools as ct
    from coremltools.converters.mil import Builder as mb
    from coremltools.converters.mil.mil import types
    directory = args.output
    receipt = {'schema': 'edge-prl-coreml-arithmetic-probe-v1',
               'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'status': 'IN_PROGRESS', 'macos': platform.mac_ver()[0], 'architecture': platform.machine(),
               'python': platform.python_version(), 'coremltools': ct.__version__, 'numpy': np.__version__,
               'tile': args.tile, 'k': 128, 'digit_products': 9, 'layout': args.layout,
               'input_layout': ('[9,M,1,128] x [9,128,1,N]' if args.layout == 'channels-first'
                                else '[1,9,M,128] x [1,9,128,N]'),
               'both_matrix_inputs_dynamic': True, 'actual_npu_execution_verified': False,
               'official_prl_proof_verified': False, 'pool_submissions': 0, 'mining_backend_qualified': False,
               'developer_fee_charged': False, 'results': []}
    receipt['experiment_source_sha256'] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('__init__.py', 'arithmetic.py', 'probe.py')}
    save_json(directory / 'receipt.json', receipt)
    try:
        shapes = ((9, args.tile, 1, 128), (9, 128, 1, args.tile)) if args.layout == 'channels-first' else (
            (1, 9, args.tile, 128), (1, 9, 128, args.tile))
        @mb.program(input_specs=[mb.TensorSpec(shape=shapes[0], dtype=types.fp16),
                                mb.TensorSpec(shape=shapes[1], dtype=types.fp16)],
                    opset_version=ct.target.macOS13)
        def program(a, b):
            if args.layout == 'channels-first':
                return mb.einsum(values=(a, b), equation='nchw,nwhu->nchu', name='digit_products')
            return mb.matmul(x=a, y=b, name='digit_products')
        started = time.perf_counter()
        model = ct.convert(program, convert_to='mlprogram', minimum_deployment_target=ct.target.macOS13,
                           compute_precision=ct.precision.FLOAT16, compute_units=ct.ComputeUnit.CPU_ONLY,
                           skip_model_load=True)
        package = directory / 'digit-products.mlpackage'
        model.save(str(package))
        receipt['conversion_seconds'] = time.perf_counter() - started
        receipt['model_files_sha256'] = tree_hashes(package)
        rng = np.random.default_rng(733128)
        cases = []
        for name in ('zero', 'digit-boundary', 'signed-boundary', 'random-1', 'random-2'):
            if name == 'zero':
                a = np.zeros((args.tile, 128), dtype=np.int64)
                b = np.zeros((128, args.tile), dtype=np.int64)
            elif name == 'digit-boundary':
                a = np.full((args.tile, 128), 124, dtype=np.int64)
                b = np.full((128, args.tile), 124, dtype=np.int64)
            elif name == 'signed-boundary':
                a = np.resize(np.array([-128, 127, -127, 126], dtype=np.int64), (args.tile, 128))
                b = np.resize(np.array([127, -128, 125, -126], dtype=np.int64), (128, args.tile))
            else:
                a = rng.integers(-128, 128, size=(args.tile, 128), dtype=np.int64)
                b = rng.integers(-128, 128, size=(128, args.tile), dtype=np.int64)
            inputs, expected_digits, expected_matrix = inputs_and_expected(a, b, np, args.layout)
            cases.append((name, inputs, expected_digits, expected_matrix))
        for mode in ('CPU_ONLY', 'CPU_AND_NE'):
            units = getattr(ct.ComputeUnit, mode)
            started = time.perf_counter()
            loaded = ct.models.MLModel(str(package), compute_units=units)
            mode_result = {'requested_compute_units': mode, 'model_load_seconds': time.perf_counter() - started,
                           'compute_plan': compute_plan(loaded, units, ct), 'cases': []}
            for name, inputs, expected_digits, expected_matrix in cases:
                timings = []
                digest = None
                for repetition in range(args.repeats):
                    started = time.perf_counter()
                    output = loaded.predict(inputs)
                    timings.append(time.perf_counter() - started)
                    digest = check_outputs(output['digit_products'], expected_digits, expected_matrix, np, args.layout)
                mode_result['cases'].append({'name': name, 'exact': True, 'predictions': args.repeats,
                    'prediction_seconds': timings, 'reconstructed_sha256': digest})
            receipt['results'].append(mode_result)
            save_json(directory / 'receipt.json', receipt)
            del loaded
        ne = receipt['results'][-1]['compute_plan']
        receipt['status'] = ('EXACT_ARITHMETIC_WITH_ANE_PLANNED_PLACEMENT_ONLY'
                             if ne['all_matrix_products_prefer_neural_engine'] else
                             'EXACT_ARITHMETIC_BUT_NPU_PLACEMENT_NOT_ESTABLISHED')
        receipt['completed_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(directory / 'receipt.json', receipt)
    except Exception as exc:
        receipt.update(status='FAILED', error_type=type(exc).__name__, error=str(exc)[:1200])
        save_json(directory / 'receipt.json', receipt)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bounded exact Core ML arithmetic probe; never starts mining.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tile', type=int, choices=(16, 32, 64, 128, 256), default=32)
    parser.add_argument('--layout', choices=('batched-matmul', 'channels-first'), default='batched-matmul')
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=2)
    parser.add_argument('--max-seconds', type=int, default=180)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.output = args.output.resolve()
    if not 10 <= args.max_seconds <= 300:
        parser.error('--max-seconds must be between 10 and 300')
    if args.worker:
        worker(args)
        return 0
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        parser.error('Prediction requires native Apple Silicon macOS; offline oracle tests run elsewhere')
    args.output.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, '-m', 'experiments.coreml_prl.probe', '--worker', '--output', str(args.output),
               '--tile', str(args.tile), '--layout', args.layout,
               '--repeats', str(args.repeats), '--max-seconds', str(args.max_seconds)]
    environment = os.environ.copy()
    environment['EDGE_PRL_COREML_PARENT'] = str(os.getpid())
    child = None
    try:
        with (args.output / 'worker.log').open('xb') as log:
            child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=environment,
                                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
            try:
                code = child.wait(timeout=args.max_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
                save_json(args.output / 'supervisor.json', {'status': 'TIMEOUT', 'worker_stopped': True,
                          'max_seconds': args.max_seconds, 'mining_started': False})
                return 2
        save_json(args.output / 'supervisor.json', {'status': 'COMPLETED' if code == 0 else 'FAILED',
                  'worker_exit_code': code, 'worker_stopped': True, 'max_seconds': args.max_seconds,
                  'mining_started': False})
        if code == 0:
            receipt = json.loads((args.output / 'receipt.json').read_text())
            print(json.dumps({key: receipt[key] for key in ('status', 'actual_npu_execution_verified',
                              'official_prl_proof_verified', 'pool_submissions', 'mining_backend_qualified')}))
        return code
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
