"""Proof-cache ABI ownership tests with tiny synthetic arrays, not PRL proofs.

These tests do not load a shared library, contact a pool or establish hardware
or consensus correctness. Native proof equivalence requires the separate gate.
"""
from contextlib import ExitStack
import ctypes as C
import os
import unittest
from unittest.mock import patch
import weakref

from engine.proof import liveproof


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class NativeFixture:
    def __init__(self):
        self.prepared = 0
        self.opened = 0
        self.closed = []
        self.cached_builds = []
        self.legacy_builds = []
        self.prep_result = self.close_result = 0
        self.open_result = 17
        self.close_exception = None
        self.mismatch = False
        self.build_result = None
        self.original_refs = []
        self.roots = [bytes([i]) * 32 for i in range(1, 5)]
        self.prl_config_and_key = Function(self.config)
        self.prl_build_proof = Function(self.build_legacy)
        self.prl_verify_proof = Function(lambda *args: 0)
        self.prep_from_ab = Function(self.prepare)
        self.prl_proof_cache_open = Function(self.open)
        self.prl_build_proof_cached = Function(self.build_cached)
        self.prl_proof_cache_close = Function(self.close)

    def config(self, header, header_len, config, key, error, error_len):
        C.memmove(config, liveproof.CONFIG52, 52)
        C.memmove(key, bytes([9]) * 32, 32)
        return 0

    def prepare(self, *args):
        self.prepared += 1
        self.original_refs = [weakref.ref(args[0]), weakref.ref(args[1])]
        for pointer, root in zip(args[11:15], self.roots):
            C.memmove(pointer, root, 32)
        return self.prep_result

    def open(self, a, a_len, bt, bt_len, key, root_a, root_b, error, error_len):
        self.opened += 1
        # Save bytes, never Python arrays, mirroring the ownership contract.
        self.native_originals = (bytes(a), bytes(bt))
        C.memmove(root_a, bytes(32) if self.mismatch else self.roots[0], 32)
        C.memmove(root_b, self.roots[1], 32)
        if self.open_result <= 0:
            error.value = b"synthetic open error"
        return self.open_result

    def build_legacy(self, a, a_len, bt, bt_len, key, row, col, output, capacity, error, error_len):
        self.legacy_builds.append((row, col))
        return self.build(output, error)

    def build_cached(self, handle, row, col, output, capacity, error, error_len):
        self.cached_builds.append((handle, row, col))
        return self.build(output, error)

    def build(self, output, error):
        output.value = b"synthetic-proof"
        error.value = b"synthetic build error"
        return len(b"synthetic-proof") if self.build_result is None else self.build_result

    def close(self, handle, error, error_len):
        self.closed.append(handle)
        if self.close_exception:
            raise self.close_exception
        if self.close_result:
            error.value = b"synthetic close error"
        else:
            self.native_originals = None
        return self.close_result


class ProofCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.native = NativeFixture()
        self.stack.enter_context(patch.object(liveproof.C, "CDLL", return_value=self.native))
        # Preserve fixed-shape production validation; substitute tiny dimensions
        # only inside this isolated Python ownership/dispatch test fixture.
        self.stack.enter_context(patch.multiple(liveproof, M=128, N=128, K=64, RANK=2))
        self.stack.enter_context(patch.dict(os.environ))

    def make(self, **kwargs):
        job = liveproof.PreparedJob("unused", bytes(76), bytes(32), m=128, n=128, **kwargs)
        self.addCleanup(job.close)
        return job

    def test_legacy_default_works_with_old_library_and_keeps_originals(self):
        for name in ("prl_proof_cache_open", "prl_build_proof_cached", "prl_proof_cache_close"):
            delattr(self.native, name)
        job = self.make()
        self.assertEqual(job.proof_mode, "legacy")
        self.assertEqual(job.build_proof(0, 0), b"synthetic-proof")
        self.assertEqual(self.native.legacy_builds, [(0, 0)])
        self.assertEqual(self.native.opened, 0)
        self.assertIsNotNone(job.original_a)
        self.assertEqual(job.python_matrix_bytes_resident, 32768)
        self.assertEqual(job.native_matrix_bytes_resident, 0)
        self.assertEqual(job.matrix_bytes_resident, 32768)
        self.assertEqual(job.proof_cache_create_seconds, 0)
        job.close()
        self.assertEqual(job.matrix_bytes_resident, 0)

    def test_cached_mode_owns_copies_releases_python_originals_and_dispatches(self):
        job = self.make(proof_mode="cached")
        self.assertEqual(self.native.prepared, 1)
        self.assertEqual(self.native.opened, 1)
        self.assertIsNone(job.original_a)
        self.assertIsNone(job.original_bt)
        self.assertTrue(all(reference() is None for reference in self.native.original_refs))
        self.assertEqual(len(self.native.native_originals[0]), 8192)
        self.assertEqual(job.python_matrix_bytes_resident, 16384)
        self.assertEqual(job.native_matrix_bytes_resident, 16384)
        self.assertEqual(job.matrix_bytes_resident, 32768)
        self.assertEqual(job.build_proof(1, 64), b"synthetic-proof")
        self.assertEqual(self.native.cached_builds, [(17, 1, 64)])
        self.assertEqual(self.native.legacy_builds, [])
        self.assertGreaterEqual(job.proof_cache_create_seconds, 0)
        self.assertGreaterEqual(job.preparation_seconds, job.matrix_preparation_seconds + job.proof_cache_create_seconds)

    def test_testing_retention_counts_duplicate_original_storage(self):
        job = self.make(proof_mode="cached", retain_originals_for_testing=True)
        self.assertTrue(all(reference() is not None for reference in self.native.original_refs))
        self.assertEqual(job.python_matrix_bytes_resident, 32768)
        self.assertEqual(job.native_matrix_bytes_resident, 16384)
        self.assertEqual(job.matrix_bytes_resident, 49152)
        job.build_proof(0, 0)
        self.assertEqual(self.native.legacy_builds, [])

    def test_missing_cached_abi_fails_before_matrix_preparation_no_fallback(self):
        for symbol in ("prl_proof_cache_open", "prl_build_proof_cached", "prl_proof_cache_close"):
            with self.subTest(symbol=symbol):
                callback = getattr(self.native, symbol)
                delattr(self.native, symbol)
                with self.assertRaisesRegex(RuntimeError, symbol):
                    self.make(proof_mode="cached")
                setattr(self.native, symbol, callback)
        self.assertEqual(self.native.prepared, 0)
        self.assertEqual(self.native.opened, 0)
        self.assertEqual(self.native.legacy_builds, [])

    def test_preparation_failure_never_opens_cache_and_releases_matrices(self):
        self.native.prep_result = 1
        job = liveproof.PreparedJob.__new__(liveproof.PreparedJob)
        with self.assertRaisesRegex(RuntimeError, "prep_from_ab failed"):
            job.__init__("unused", bytes(76), bytes(32), m=128, n=128, proof_mode="cached")
        self.assertTrue(job.closed)
        self.assertIsNone(job.original_a)
        self.assertIsNone(job.b_noised)
        self.assertEqual(self.native.opened, 0)
        self.assertEqual(self.native.closed, [])

    def test_cache_open_failure_does_not_close_invalid_handle(self):
        for result in (0, -1):
            self.native.open_result = result
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "synthetic open error"):
                self.make(proof_mode="cached")
        self.assertEqual(self.native.closed, [])
        self.assertEqual(self.native.legacy_builds, [])

    def test_root_mismatch_closes_owned_handle_once_and_never_builds(self):
        self.native.mismatch = True
        job = liveproof.PreparedJob.__new__(liveproof.PreparedJob)
        with self.assertRaisesRegex(RuntimeError, "roots differ"):
            job.__init__("unused", bytes(76), bytes(32), m=128, n=128, proof_mode="cached")
        job.close()
        self.assertEqual(self.native.closed, [17])
        self.assertEqual(job._cache_handle, 0)
        self.assertEqual(job.matrix_bytes_resident, 0)
        self.assertEqual(self.native.cached_builds, [])

    def test_close_is_idempotent_and_closed_job_rejects_use(self):
        job = self.make(proof_mode="cached")
        job.close()
        job.close()
        self.assertEqual(self.native.closed, [17])
        self.assertEqual(job.matrix_bytes_resident, 0)
        self.assertEqual(job.native_matrix_bytes_resident, 0)
        self.assertIsNone(job.a_noised)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            job.build_proof(0, 0)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            job.verify(b"test", 1)

    def test_native_close_error_is_reported_without_double_release_or_zero_memory_claim(self):
        self.native.close_result = -1
        job = self.make(proof_mode="cached")
        with self.assertRaisesRegex(RuntimeError, "synthetic close error"):
            job.close()
        job.close()
        self.assertEqual(self.native.closed, [17])
        self.assertTrue(job.closed)
        self.assertEqual(job.python_matrix_bytes_resident, 0)
        self.assertEqual(job.native_matrix_bytes_resident, 16384)
        self.assertEqual(job.matrix_bytes_resident, 16384)

    def test_native_close_exception_is_not_retried(self):
        self.native.close_exception = RuntimeError("synthetic binding error")
        job = self.make(proof_mode="cached")
        with self.assertRaisesRegex(RuntimeError, "binding error"):
            job.close()
        job.close()
        self.assertEqual(self.native.closed, [17])
        self.assertIsNone(job.original_a)

    def test_constructor_reports_primary_and_cleanup_failure(self):
        self.native.mismatch = True
        self.native.close_result = -1
        with self.assertRaisesRegex(RuntimeError, "roots differ.*cleanup also failed.*synthetic close error"):
            self.make(proof_mode="cached")
        self.assertEqual(self.native.closed, [17])

    def test_context_manager_closes_cache_on_exception(self):
        with self.assertRaisesRegex(ValueError, "caller error"):
            with self.make(proof_mode="cached"):
                raise ValueError("caller error")
        self.assertEqual(self.native.closed, [17])

    def test_invalid_modes_and_retention_flags_rejected_before_native_calls(self):
        for mode in (None, True, [], "auto", "Cached"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "proof_mode"):
                self.make(proof_mode=mode)
        for flag in (None, 0, 1, "false"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "boolean"):
                self.make(retain_originals_for_testing=flag)
        self.assertEqual(self.native.prepared, 0)

    def test_bounds_and_native_failures_never_return_truncated_proof(self):
        job = self.make(proof_mode="cached")
        for row, col in ((True, 0), (0, False), (32, 0), (-1, 0), (96, 0), (0, 1), (0, 128)):
            with self.subTest(row=row, col=col), self.assertRaises(ValueError):
                job.build_proof(row, col)
        self.assertEqual(self.native.cached_builds, [])
        for result in (0, -1, liveproof.PROOF_CAP + 1):
            self.native.build_result = result
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                job.build_proof(0, 0)
        self.assertEqual(self.native.legacy_builds, [])
        with self.assertRaises(ValueError):
            job.verify(b"test", True)


if __name__ == "__main__":
    unittest.main()
