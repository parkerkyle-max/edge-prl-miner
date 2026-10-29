//! Bounded C ABI around the pinned official Pearl PlainProof and verifier.
//! The Merkle proof construction follows the MIT ascend_prl proof-ffi implementation.
use anyhow::{anyhow, ensure, Result};
use bincode::Options;
use pearl_blake3::MerkleTree;
use std::{panic::{catch_unwind, AssertUnwindSafe}, slice, sync::OnceLock};
use zk_pow::{
    api::{proof::{IncompleteBlockHeader, MMAType, MiningConfiguration, PeriodicPattern, SeedDerivation},
          proof_utils::{CompiledPublicParams, compute_jackpot_hash}, verify::verify_plain_proof},
    circuit::{chip::compute_jackpot, pearl_noise::compute_noise},
    ffi::plain_proof::{check_cert_version_eligible, MatrixMerkleProof, PlainProof},
};

const DIM: usize = 16384;
const K: usize = 4096;
const RANK: usize = 128;
const MAX_PROOF: usize = 2 * 1024 * 1024;
static RAYON_POOL: OnceLock<rayon::ThreadPool> = OnceLock::new();

fn single_pool() -> &'static rayon::ThreadPool {
    RAYON_POOL.get_or_init(|| rayon::ThreadPoolBuilder::new().num_threads(1).build().expect("one proof worker"))
}

unsafe fn write_error(out: *mut u8, capacity: usize, message: &str) {
    if out.is_null() || capacity == 0 { return; }
    let length = message.len().min(capacity - 1);
    std::ptr::copy_nonoverlapping(message.as_ptr(), out, length);
    *out.add(length) = 0;
}

unsafe fn boundary<F: FnOnce() -> Result<isize>>(error: *mut u8, capacity: usize, work: F) -> isize {
    write_error(error, capacity, "");
    match catch_unwind(AssertUnwindSafe(work)) {
        Ok(Ok(value)) => value,
        Ok(Err(e)) => { write_error(error, capacity, &format!("{e:#}")); -1 },
        Err(_) => { write_error(error, capacity, "panic caught at proof FFI boundary"); -2 },
    }
}

fn config() -> Result<MiningConfiguration> {
    Ok(MiningConfiguration {
        common_dim: K as u32, rank: RANK as u16, mma_type: MMAType::Int7xInt7ToInt32,
        rows_pattern: PeriodicPattern::from_list(&[0, 32])?,
        cols_pattern: PeriodicPattern::from_list(&(0..64).collect::<Vec<u32>>())?, moe: None,
    })
}

fn matrix_proof(data: &[u8], key: [u8; 32], rows: &[usize]) -> MatrixMerkleProof {
    let tree = MerkleTree::new(data, key);
    let leaves = MerkleTree::compute_leaf_indices_from_rows(rows, (0, K));
    let proof = tree.get_multileaf_proof(&leaves);
    MatrixMerkleProof::new(proof.leaf_data, proof.leaf_indices, rows.to_vec(), proof.total_leaves, proof.root, proof.siblings)
}

#[no_mangle]
pub unsafe extern "C" fn prl_config_and_key(header: *const u8, header_len: usize,
    config_out: *mut u8, key_out: *mut u8, error: *mut u8, error_cap: usize) -> isize {
    boundary(error, error_cap, || {
        ensure!(!header.is_null() && !config_out.is_null() && !key_out.is_null(), "null pointer");
        ensure!(header_len == 76, "header must have 76 bytes");
        let bytes = slice::from_raw_parts(header, header_len);
        let parsed = IncompleteBlockHeader::from_bytes(bytes)?;
        ensure!(parsed.to_bytes() == bytes, "header round-trip mismatch");
        let configuration = config()?.to_bytes();
        let mut hasher = blake3::Hasher::new(); hasher.update(bytes); hasher.update(&configuration);
        std::ptr::copy_nonoverlapping(configuration.as_ptr(), config_out, 52);
        std::ptr::copy_nonoverlapping(hasher.finalize().as_bytes().as_ptr(), key_out, 32);
        Ok(0)
    })
}

#[no_mangle]
pub unsafe extern "C" fn prl_build_proof(a: *const u8, a_len: usize, bt: *const u8, bt_len: usize,
    key: *const u8, row: usize, col: usize, out: *mut u8, out_cap: usize,
    error: *mut u8, error_cap: usize) -> isize {
    boundary(error, error_cap, || {
        ensure!(!a.is_null() && !bt.is_null() && !key.is_null() && !out.is_null(), "null pointer");
        ensure!(a_len == DIM*K && bt_len == DIM*K, "only 16384x4096 original matrices supported");
        ensure!(row < DIM-32 && row % 64 < 32 && col <= DIM-64 && col % 64 == 0, "invalid row pair / column band");
        ensure!(out_cap >= 512*1024 && out_cap <= MAX_PROOF, "proof output capacity outside bound");
        let original_a = slice::from_raw_parts(a, a_len);
        let original_bt = slice::from_raw_parts(bt, bt_len);
        let key32: [u8;32] = slice::from_raw_parts(key, 32).try_into()?;
        let a_rows = [row, row+32];
        let b_rows: Vec<usize> = (col..col+64).collect();
        for &selected in &a_rows {
            ensure!(original_a[selected*K..(selected+1)*K].iter().all(|&v| (-64..=63).contains(&(v as i8))), "illegal opened original A input");
        }
        for &selected in &b_rows {
            ensure!(original_bt[selected*K..(selected+1)*K].iter().all(|&v| (-64..=63).contains(&(v as i8))), "illegal opened original Bt input");
        }
        let proof = single_pool().install(|| PlainProof {
            m: DIM, n: DIM, k: K, noise_rank: RANK,
            a: matrix_proof(original_a, key32, &a_rows),
            bt: matrix_proof(original_bt, key32, &b_rows), moe: None,
        });
        let bytes = bincode::serialize(&proof)?;
        ensure!(bytes.len() <= out_cap, "proof output capacity insufficient");
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
        Ok(bytes.len() as isize)
    })
}

#[no_mangle]
pub unsafe extern "C" fn prl_verify_proof(header: *const u8, header_len: usize,
    proof: *const u8, proof_len: usize, certificate: u32, nbits_override: u32,
    expected_jackpot: *const u8, jackpot_out: *mut u8, transcript_out: *mut u8,
    oracle_ready: *mut u32, error: *mut u8, error_cap: usize) -> isize {
    if !oracle_ready.is_null() { *oracle_ready = 0; }
    boundary(error, error_cap, || {
        ensure!(!header.is_null() && !proof.is_null() && !jackpot_out.is_null() && !transcript_out.is_null() && !oracle_ready.is_null(), "null pointer");
        ensure!(header_len == 76 && proof_len > 0 && proof_len <= MAX_PROOF, "header/proof length invalid");
        ensure!(certificate == 3, "only current certificate version 3 is supported");
        ensure!(nbits_override != 0, "explicit nonzero verification target required");
        let header = IncompleteBlockHeader::from_bytes(slice::from_raw_parts(header, header_len))?;
        let plain: PlainProof = bincode::DefaultOptions::new().with_fixint_encoding().reject_trailing_bytes()
            .with_limit(MAX_PROOF as u64).deserialize(slice::from_raw_parts(proof, proof_len))?;
        ensure!(plain.m == DIM && plain.n == DIM && plain.k == K && plain.noise_rank == RANK && plain.moe.is_none(), "unsupported proof shape");
        ensure!(plain.a.row_indices.len() == 2 && plain.bt.row_indices.len() == 64, "unsupported proof tile");
        check_cert_version_eligible(certificate, &plain)?;
        let (digest, message, verified) = single_pool().install(|| -> Result<([u8; 32], [u32; 16], Result<()>)> {
            let (private, public) = plain.parse_proof(header, SeedDerivation::Salted)?;
            public.sanity_check()?;
            ensure!(public.mining_config.to_bytes() == config()?.to_bytes(), "proof mining configuration differs from supported config52");
            ensure!(private.s_a.iter().chain(private.s_b.iter()).flatten().all(|&v| (-64..=63).contains(&v)), "original matrix values outside [-64,63]");
            let compiled = CompiledPublicParams::from(&public);
            let noise = compute_noise(&compiled);
            let message = compute_jackpot(&compiled, &private.s_a, &private.s_b, &noise);
            let digest = compute_jackpot_hash(&message, compiled.a_noise_seed());
            let verified = verify_plain_proof(&header, &plain, Some(nbits_override), SeedDerivation::Salted)
                .map_err(|e| anyhow!("official verify_plain_proof: {e:#}"));
            Ok((digest, message, verified))
        })?;
        for (index, value) in message.iter().enumerate() {
            std::ptr::copy_nonoverlapping(value.to_le_bytes().as_ptr(), transcript_out.add(index*4), 4);
        }
        std::ptr::copy_nonoverlapping(digest.as_ptr(), jackpot_out, 32);
        *oracle_ready = 1;
        if !expected_jackpot.is_null() {
            ensure!(slice::from_raw_parts(expected_jackpot, 32) == digest, "NPU jackpot differs from official oracle");
        }
        verified?;
        Ok(0)
    })
}

include!("cache_api.rs");
