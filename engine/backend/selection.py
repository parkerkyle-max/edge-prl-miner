"""Explicit backend qualification boundary; no SDK import or device access.

A discovery hint is not a driver, an exact arithmetic result, or permission to
mine. Ports must implement the scan contract and qualify their own model,
runtime and hardware before joining the executable backend allowlist.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_BACKEND_ID = 'a733-viplite-v2-f32-256'
SUPPORTED_BACKEND_IDS = frozenset((DEFAULT_BACKEND_ID,))


@dataclass(frozen=True)
class BackendContract:
    backend_id: str = DEFAULT_BACKEND_ID
    contract_version: int = 1
    implementation_status: str = 'implemented_experimental'
    host_os: str = 'Linux'
    host_architecture: str = 'aarch64'
    scan_width: int = 256
    dynamic_matrix_inputs: int = 2
    partial_inner_dimension: int = 128
    partial_count: int = 32
    model_precision: str = 'unquantized_fp32_exact_integer_partials'
    output_hashes_per_scan: int = 512
    output_hash_bytes: int = 32
    complete_macs_per_scan: int = 268435456
    cpu_matmul_fallback_allowed: bool = False
    fresh_device_proof_gate_required: bool = True

    def to_dict(self):
        return asdict(self)


def require_backend(backend_id=DEFAULT_BACKEND_ID) -> BackendContract:
    if not isinstance(backend_id, str) or backend_id not in SUPPORTED_BACKEND_IDS:
        raise ValueError('No qualified mining implementation for backend_id; '
                         'run `python -m edge_prl backends` for research status. '
                         'Discovery never enables an unsupported backend or CPU fallback.')
    return BackendContract()
