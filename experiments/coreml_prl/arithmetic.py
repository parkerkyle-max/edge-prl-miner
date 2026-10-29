"""Exact signed-INT8 decomposition for a bounded binary16 arithmetic probe.

This module uses the standard library and performs no model/device/network work.
It is an independent small-shape oracle, never a CPU fallback for mining.
"""
from __future__ import annotations

import math

WEIGHTS = (1, 8, 64)
PARTIAL_K = 128
MAX_DIGIT_MAGNITUDE = 4
MAX_HALF_INTEGER_PARTIAL = PARTIAL_K * MAX_DIGIT_MAGNITUDE ** 2


def split_scalar(value: int) -> tuple[int, int, int]:
    if type(value) is not int or not -128 <= value <= 127:
        raise ValueError('Expected a signed INT8 integer')
    low = (value + 4) % 8 - 4
    carry = (value - low) // 8
    middle = (carry + 4) % 8 - 4
    high = (carry - middle) // 8
    return low, middle, high


def validate_shape(matrix, name):
    if not isinstance(matrix, list) or not matrix or not isinstance(matrix[0], list) or not matrix[0]:
        raise ValueError(name + ' must be a nonempty row-major matrix')
    width = len(matrix[0])
    if any(not isinstance(row, list) or len(row) != width for row in matrix):
        raise ValueError(name + ' must be rectangular')
    for row in matrix:
        for value in row:
            split_scalar(value)
    return len(matrix), width


def matmul_reference(a, b):
    m, k = validate_shape(a, 'A')
    kb, n = validate_shape(b, 'B')
    if k != kb or k > PARTIAL_K:
        raise ValueError('Shapes must agree and K must not exceed 128')
    return [[sum(a[i][p] * b[p][j] for p in range(k)) for j in range(n)] for i in range(m)]


def digit_products_reference(a, b):
    m, k = validate_shape(a, 'A')
    kb, n = validate_shape(b, 'B')
    if k != kb or k > PARTIAL_K:
        raise ValueError('Shapes must agree and K must not exceed 128')
    aa = [[split_scalar(value) for value in row] for row in a]
    bb = [[split_scalar(value) for value in row] for row in b]
    return [[[sum(aa[i][p][x] * bb[p][j][y] for p in range(k)) for j in range(n)]
             for i in range(m)] for x in range(3) for y in range(3)]


def reconstruct_scalar(partials):
    if not isinstance(partials, (list, tuple)) or len(partials) != 9:
        raise ValueError('Exactly nine digit products are required')
    values = []
    for value in partials:
        if type(value) not in (int, float) or not math.isfinite(value) or value != int(value) or abs(value) > MAX_HALF_INTEGER_PARTIAL:
            raise ValueError('Digit product must be a finite exact integer within +/-2048')
        values.append(int(value))
    return sum(values[x * 3 + y] * WEIGHTS[x] * WEIGHTS[y] for x in range(3) for y in range(3))


def plan_evidence(operation_rows):
    """A compile plan describes anticipated placement, never observed execution."""
    # Core ML exposes version-qualified names such as "ios16.matmul" on device.
    # Retain the original name in receipts, but recognize its operation suffix.
    products = [row for row in operation_rows
                if isinstance(row.get('operator'), str)
                and row['operator'].rsplit('.', 1)[-1] in ('matmul', 'einsum')]
    matmuls = [row for row in products if row['operator'].rsplit('.', 1)[-1] == 'matmul']
    preferred = bool(products) and all(row.get('preferred') == 'MLNeuralEngineComputeDevice' for row in products)
    return {'matmul_operations': len(matmuls), 'einsum_operations': len(products) - len(matmuls),
            'matrix_product_operations': len(products), 'all_matrix_products_prefer_neural_engine': preferred,
            'all_matmuls_prefer_neural_engine': bool(matmuls) and preferred,
            'evidence_scope': 'anticipated_compute_plan_only', 'actual_npu_execution_verified': False,
            'mining_backend_qualified': False}
