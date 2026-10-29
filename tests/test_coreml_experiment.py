"""Independent exact arithmetic and evidence boundaries; no Apple SDK required."""
import random
import struct
import unittest

from experiments.coreml_prl.arithmetic import (
    split_scalar, digit_products_reference, matmul_reference,
    reconstruct_scalar, plan_evidence, MAX_HALF_INTEGER_PARTIAL)


class CoreMLArithmeticTests(unittest.TestCase):
    def test_balanced_digits_cover_all_signed_int8_values(self):
        for value in range(-128, 128):
            a, b, c = split_scalar(value)
            self.assertEqual(a + 8 * b + 64 * c, value)
            self.assertTrue(-4 <= a <= 3 and -4 <= b <= 3 and -2 <= c <= 2)
        for bad in (True, 1.0, '1', -129, 128):
            with self.assertRaises(ValueError):
                split_scalar(bad)

    def test_entire_integer_partial_bound_is_exact_binary16(self):
        self.assertEqual(MAX_HALF_INTEGER_PARTIAL, 2048)
        for value in range(-2048, 2049):
            self.assertEqual(struct.unpack('<e', struct.pack('<e', value))[0], value)

    def test_nine_digit_products_reconstruct_full_int8_matmul(self):
        rng = random.Random(733128)
        cases = [([[124] * 128] * 2, [[124] * 3] * 128),
                 ([[-128] * 128] * 2, [[-128] * 3] * 128),
                 ([[127] * 128] * 2, [[-128] * 3] * 128)]
        cases += [([[rng.randrange(-128, 128) for _ in range(128)] for _ in range(3)],
                   [[rng.randrange(-128, 128) for _ in range(4)] for _ in range(128)]) for _ in range(4)]
        for a, b in cases:
            expected = matmul_reference(a, b)
            products = digit_products_reference(a, b)
            for i, row in enumerate(expected):
                for j, value in enumerate(row):
                    partials = [products[p][i][j] for p in range(9)]
                    self.assertEqual(reconstruct_scalar(partials), value)
                    self.assertTrue(all(abs(x) <= 2048 for x in partials))

    def test_bad_shapes_and_lossy_partials_fail(self):
        for a, b in (([], []), ([[1, 2], [3]], [[1], [2]]), ([[1]], [[1], [2]]),
                     ([[1] * 129], [[1]] * 129)):
            with self.assertRaises(ValueError):
                matmul_reference(a, b)
        for bad in (float('nan'), float('inf'), 0.5, 2049, True):
            with self.assertRaises(ValueError):
                reconstruct_scalar([bad] + [0] * 8)

    def test_requested_units_or_plan_never_prove_actual_npu_execution(self):
        for rows in ([], [{'operator': 'matmul', 'preferred': 'MLCPUComputeDevice'}],
                     [{'operator': 'matmul', 'preferred': 'MLNeuralEngineComputeDevice'}],
                     [{'operator': 'matmul', 'preferred': 'MLNeuralEngineComputeDevice'},
                      {'operator': 'matmul', 'preferred': 'MLCPUComputeDevice'}]):
            result = plan_evidence(rows)
            self.assertFalse(result['actual_npu_execution_verified'])
            self.assertFalse(result['mining_backend_qualified'])
        self.assertTrue(plan_evidence([{'operator': 'matmul', 'preferred': 'MLNeuralEngineComputeDevice'}])['all_matmuls_prefer_neural_engine'])

    def test_real_coreml_qualified_operator_names_are_counted(self):
        cpu = plan_evidence([{'operator': 'ios16.matmul', 'preferred': 'MLCPUComputeDevice',
                              'supported': ['MLCPUComputeDevice', 'MLNeuralEngineComputeDevice']}])
        self.assertEqual(cpu['matmul_operations'], 1)
        self.assertFalse(cpu['all_matmuls_prefer_neural_engine'])
        ne = plan_evidence([{'operator': 'ios16.matmul', 'preferred': 'MLNeuralEngineComputeDevice'}])
        self.assertEqual(ne['matmul_operations'], 1)
        self.assertTrue(ne['all_matmuls_prefer_neural_engine'])
        self.assertFalse(ne['actual_npu_execution_verified'])
        self.assertFalse(plan_evidence([{'operator': 'not_matmul', 'preferred': 'MLNeuralEngineComputeDevice'}])['all_matmuls_prefer_neural_engine'])

    def test_channels_first_einsum_is_a_matrix_product_not_observed_npu_evidence(self):
        evidence = plan_evidence([{'operator': 'ios16.einsum', 'preferred': 'MLNeuralEngineComputeDevice'}])
        self.assertEqual(evidence['matmul_operations'], 0)
        self.assertEqual(evidence['einsum_operations'], 1)
        self.assertTrue(evidence['all_matrix_products_prefer_neural_engine'])
        self.assertFalse(evidence['actual_npu_execution_verified'])
        mixed = plan_evidence([{'operator': 'ios16.einsum', 'preferred': 'MLNeuralEngineComputeDevice'},
                               {'operator': 'ios16.matmul', 'preferred': 'MLCPUComputeDevice'}])
        self.assertFalse(mixed['all_matrix_products_prefer_neural_engine'])

    def test_optional_numpy_layout_rewrite_preserves_non_square_signed_products(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('Optional NumPy arithmetic test; no Apple SDK required')
        from experiments.coreml_prl.probe import inputs_and_expected, check_outputs
        a = np.array([[-128, 127, 124, -3], [33, -14, 0, 2]], dtype=np.int64)
        b = np.array([[127, -128, 1], [124, -9, 3], [-4, 5, 6], [3, -4, 5]], dtype=np.int64)
        reference = np.array(matmul_reference(a.tolist(), b.tolist()), dtype=np.int64)
        for layout in ('batched-matmul', 'channels-first'):
            inputs, digits, expected = inputs_and_expected(a, b, np, layout)
            output = (np.einsum('nchw,nwhu->nchu', inputs['a'], inputs['b']) if layout == 'channels-first'
                      else np.matmul(inputs['a'], inputs['b']))
            self.assertTrue(np.array_equal(expected, reference))
            self.assertEqual(len(check_outputs(output, digits, reference, np, layout)), 64)
            output.flat[0] += 1
            with self.assertRaises(RuntimeError):
                check_outputs(output, digits, reference, np, layout)


if __name__ == '__main__':
    unittest.main()
