"""One prepared cert-v3 job and pinned official PlainProof verification.

No network, device or dev-fee logic lives here. Target selection is caller-owned;
the live caller must pass the immutable job's actual compact target.
"""
from __future__ import annotations
import ctypes as C
import hashlib
import os
from pathlib import Path
import time

M = N = 16384
K = 4096
RANK = 128
CONFIG52 = bytes.fromhex("00100000800000001f0100000000003f00000000") + bytes(32)
PROOF_CAP = 2 * 1024 * 1024


class PreparedJob:
    def __init__(self, libdir, header76: bytes, seed32: bytes | None = None,
                 m: int = M, n: int = N, cert_version: int = 3):
        if (m, n, cert_version) != (M, N, 3):
            raise ValueError("only M=N16384, K4096, rank128 and certificate3 are supported")
        if not isinstance(header76, bytes) or len(header76) != 76:
            raise ValueError("header must be exactly 76 bytes")
        if seed32 is None:
            seed32 = os.urandom(32)
        if not isinstance(seed32, bytes) or len(seed32) != 32:
            raise ValueError("seed must be exactly 32 bytes")
        for name in ("OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "RAYON_NUM_THREADS"):
            os.environ[name] = "1"
        self.closed = False
        self.m, self.n, self.k, self.rank, self.cert_version = m, n, K, RANK, cert_version
        self.header76 = header76
        self.seed_commitment = hashlib.sha256(seed32).hexdigest()
        self._header = C.create_string_buffer(header76, 76)
        self._prep = C.CDLL(str(Path(libdir) / "libpearl_prep.so"))
        self._proof = C.CDLL(str(Path(libdir) / "libpearl_edge_proof.so"))
        pointer, size = C.c_void_p, C.c_size_t
        self._proof.prl_config_and_key.argtypes = [pointer, size, pointer, pointer, pointer, size]
        self._proof.prl_config_and_key.restype = C.c_ssize_t
        self._proof.prl_build_proof.argtypes = [pointer, size, pointer, size, pointer, size, size, pointer, size, pointer, size]
        self._proof.prl_build_proof.restype = C.c_ssize_t
        self._proof.prl_verify_proof.argtypes = [pointer, size, pointer, size, C.c_uint32, C.c_uint32,
                                               pointer, pointer, pointer, pointer, pointer, size]
        self._proof.prl_verify_proof.restype = C.c_ssize_t
        self._prep.prep_from_ab.argtypes = [pointer, pointer, pointer, C.c_int64, C.c_int64, C.c_int64, C.c_int,
                                          pointer, pointer, pointer, pointer, pointer, pointer, pointer, pointer, C.c_int, C.c_int]
        self._prep.prep_from_ab.restype = C.c_int
        self._key = (C.c_uint8 * 32)()
        configuration = (C.c_uint8 * 52)()
        error = C.create_string_buffer(2048)
        result = self._proof.prl_config_and_key(self._header, 76, configuration, self._key, error, len(error))
        if result != 0:
            raise RuntimeError(error.value.decode(errors="replace"))
        self.config52, self.job_key = bytes(configuration), bytes(self._key)
        if self.config52 != CONFIG52:
            raise RuntimeError("official configuration serialization differs from expected config52")
        started = time.monotonic()
        # Legal sparse originals: all-zero matrices with unique bounded seed stamps.
        # The stamp is inside both opened first strips in the (0,0) correctness test.
        self.original_a = (C.c_int8 * (M*K))()
        self.original_bt = (C.c_int8 * (N*K))()
        seed_a = hashlib.sha256(b"PRL-edge-original-A-v1" + seed32).digest()
        seed_b = hashlib.sha256(b"PRL-edge-original-Bt-v1" + seed32).digest()
        for i in range(32):
            self.original_a[i] = (seed_a[i] & 127) - 64
            self.original_bt[i] = (seed_b[i] & 127) - 64
        self.a_noised = (C.c_int8 * (M*K))()
        self.b_noised = (C.c_int8 * (K*N))()
        eal, ebr = (C.c_int8 * (M*RANK))(), (C.c_int8 * (N*RANK))()
        roots = [(C.c_uint8 * 32)() for _ in range(4)]
        result = self._prep.prep_from_ab(self.original_a, self.original_bt, self._key, M, N, K, RANK,
                                        self.a_noised, self.b_noised, eal, ebr, *roots, 1, cert_version)
        if result != 0:
            self.close()
            raise RuntimeError(f"prep_from_ab failed: {result}")
        self.root_a, self.root_b, self.commit_a, self.commit_b = (bytes(value) for value in roots)
        self.preparation_seconds = time.monotonic() - started
        self.matrix_bytes_resident = 2 * (M+N) * K
        # eal and ebr are released here; callers need only the four persistent matrices.

    def _open(self):
        if self.closed:
            raise RuntimeError("PreparedJob is closed")

    def build_proof(self, row: int, col_base: int) -> bytes:
        self._open()
        if not (isinstance(row, int) and isinstance(col_base, int) and 0 <= row < M-32
                and row % 64 < 32 and 0 <= col_base <= N-64 and col_base % 64 == 0):
            raise ValueError("invalid [row,row+32] /64-column band")
        output, error = C.create_string_buffer(PROOF_CAP), C.create_string_buffer(2048)
        length = self._proof.prl_build_proof(self.original_a, M*K, self.original_bt, N*K,
                                           self._key, row, col_base, output, len(output), error, len(error))
        if length <= 0:
            raise RuntimeError(error.value.decode(errors="replace"))
        return output.raw[:length]

    def verify(self, proof_bytes: bytes, nbits_override: int, expected_jackpot: bytes | None = None) -> dict:
        self._open()
        if not isinstance(proof_bytes, bytes) or not 0 < len(proof_bytes) <= PROOF_CAP:
            raise ValueError("proof size/type invalid")
        if not isinstance(nbits_override, int) or not 0 < nbits_override <= 0xffffffff:
            raise ValueError("explicit uint32 compact target required")
        if expected_jackpot is not None and (not isinstance(expected_jackpot, bytes) or len(expected_jackpot) != 32):
            raise ValueError("expected jackpot must be32 bytes")
        proof = C.create_string_buffer(proof_bytes, len(proof_bytes))
        expected = C.create_string_buffer(expected_jackpot, 32) if expected_jackpot is not None else None
        jackpot, transcript = (C.c_uint8 * 32)(), (C.c_uint8 * 64)()
        oracle_ready, error = C.c_uint32(0), C.create_string_buffer(2048)
        result = self._proof.prl_verify_proof(self._header, 76, proof, len(proof_bytes), self.cert_version, nbits_override,
                                            expected, jackpot, transcript, C.byref(oracle_ready), error, len(error))
        return {"valid": result == 0, "error": error.value.decode(errors="replace"), "return_code": result,
                "oracle_available": bool(oracle_ready.value),
                "jackpot_hex": bytes(jackpot).hex() if oracle_ready.value else None,
                "transcript_hex": bytes(transcript).hex() if oracle_ready.value else None,
                "nbits_override": nbits_override, "cert_version": self.cert_version,
                "official_commit": "0a17bb345a838147a4c14fc618de1839cbc16c1e"}

    def close(self):
        # The coordinator must wait for its NPU scan to finish before calling close.
        self.closed = True
        for name in ("original_a", "original_bt", "a_noised", "b_noised"):
            setattr(self, name, None)

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *_):
        self.close()
