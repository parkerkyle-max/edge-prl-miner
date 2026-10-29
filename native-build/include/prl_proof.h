#ifndef PRL_EDGE_PROOF_H
#define PRL_EDGE_PROOF_H
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Fixed dense cert3 ABI: M=N16384, K4096, rank128; rows[r,r+32], cols[c..c+63].
 * Errors write a bounded NUL-terminated diagnostic and return -1; caught panic -2.
 * Buffers are caller-owned. One active caller per PreparedJob. No network or fee.
 */
ssize_t prl_config_and_key(const uint8_t *header, size_t header_len,
    uint8_t config_out[52], uint8_t key_out[32], uint8_t *error, size_t error_cap);
ssize_t prl_build_proof(const uint8_t *a, size_t a_len, const uint8_t *bt, size_t bt_len,
    const uint8_t key[32], size_t row, size_t col, uint8_t *out, size_t out_cap,
    uint8_t *error, size_t error_cap);
/* Additive cache ABI: owned immutable matrix snapshots, one active cache,
 * monotonic positive integer handles. A closed/stale handle is rejected.
 * Compare returned roots with preparation roots before using cached proofs.
 */
ssize_t prl_proof_cache_open(const uint8_t *a, size_t a_len,
    const uint8_t *bt, size_t bt_len, const uint8_t key[32],
    uint8_t root_a[32], uint8_t root_b[32], uint8_t *error, size_t error_cap);
ssize_t prl_build_proof_cached(size_t handle, size_t row, size_t col,
    uint8_t *out, size_t out_cap, uint8_t *error, size_t error_cap);
ssize_t prl_proof_cache_close(size_t handle, uint8_t *error, size_t error_cap);
/* Returns0 only after the pinned official full PlainProof verifier accepts the
 * explicit compact target. expected_jackpot is optional only for offline oracles;
 * live callers must supply the actual NPU candidate's32-byte digest.
 * oracle_ready=1 means transcript/hash were reconstructed even if target fails;
 * it does not mean the proof met target. easy targets must stay offline.
 */
ssize_t prl_verify_proof(const uint8_t *header, size_t header_len,
    const uint8_t *proof, size_t proof_len, uint32_t certificate, uint32_t nbits_override,
    const uint8_t *expected_jackpot, uint8_t jackpot_out[32], uint8_t transcript_out[64],
    uint32_t *oracle_ready, uint8_t *error, size_t error_cap);
#ifdef __cplusplus
}
#endif
#endif
