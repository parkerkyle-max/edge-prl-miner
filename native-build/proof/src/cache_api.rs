// Additive ABI. Own immutable matrix copies in the official MerkleTree; never
// borrow foreign buffers beyond creation. Integer handles reject stale IDs.
use std::sync::Mutex;
struct CachedProof { a: MerkleTree, bt: MerkleTree }
struct ProofCacheSlot { next: usize, current: Option<(usize, CachedProof)> }
static PROOF_CACHE: OnceLock<Mutex<ProofCacheSlot>> = OnceLock::new();
fn proof_cache() -> &'static Mutex<ProofCacheSlot> {
    PROOF_CACHE.get_or_init(|| Mutex::new(ProofCacheSlot { next: 1, current: None }))
}
fn matrix_proof_from_tree(tree: &MerkleTree, rows: &[usize]) -> MatrixMerkleProof {
    let leaves = MerkleTree::compute_leaf_indices_from_rows(rows, (0, K));
    let proof = tree.get_multileaf_proof(&leaves);
    MatrixMerkleProof::new(proof.leaf_data, proof.leaf_indices, rows.to_vec(), proof.total_leaves, proof.root, proof.siblings)
}
#[no_mangle]
pub unsafe extern "C" fn prl_proof_cache_open(a: *const u8, a_len: usize,
    bt: *const u8, bt_len: usize, key: *const u8, root_a: *mut u8, root_b: *mut u8,
    error: *mut u8, error_cap: usize) -> isize {
    boundary(error, error_cap, || {
        ensure!(!a.is_null() && !bt.is_null() && !key.is_null() && !root_a.is_null() && !root_b.is_null(), "null cache pointer");
        ensure!(a_len == DIM*K && bt_len == DIM*K, "cache matrix size mismatch");
        let mut slot = proof_cache().lock().map_err(|_| anyhow!("proof cache poisoned"))?;
        ensure!(slot.current.is_none(), "only one proof cache allowed");
        ensure!(slot.next > 0 && slot.next < isize::MAX as usize, "cache ID exhausted");
        let aa = slice::from_raw_parts(a, a_len);
        let bb = slice::from_raw_parts(bt, bt_len);
        ensure!(aa.iter().chain(bb.iter()).all(|&v| (-64..=63).contains(&(v as i8))), "illegal original matrix input");
        let key32: [u8;32] = slice::from_raw_parts(key, 32).try_into()?;
        let context = single_pool().install(|| CachedProof { a: MerkleTree::new(aa, key32), bt: MerkleTree::new(bb, key32) });
        ensure!(context.a.key() == key32 && context.bt.key() == key32, "cached key mismatch");
        std::ptr::copy_nonoverlapping(context.a.root().as_ptr(), root_a, 32);
        std::ptr::copy_nonoverlapping(context.bt.root().as_ptr(), root_b, 32);
        let handle = slot.next; slot.next += 1; slot.current = Some((handle, context));
        Ok(handle as isize)
    })
}
#[no_mangle]
pub unsafe extern "C" fn prl_build_proof_cached(handle: usize, row: usize, col: usize,
    out: *mut u8, out_cap: usize, error: *mut u8, error_cap: usize) -> isize {
    boundary(error, error_cap, || {
        ensure!(!out.is_null() && handle > 0, "null output or invalid cache ID");
        ensure!(row < DIM-32 && row % 64 < 32 && col <= DIM-64 && col % 64 == 0, "invalid row pair / column band");
        ensure!(out_cap >= 512*1024 && out_cap <= MAX_PROOF, "proof output capacity outside bound");
        let slot = proof_cache().lock().map_err(|_| anyhow!("proof cache poisoned"))?;
        let (current, cached) = slot.current.as_ref().ok_or_else(|| anyhow!("proof cache closed"))?;
        ensure!(*current == handle, "stale proof cache ID");
        let a_rows = [row, row+32];
        let b_rows: Vec<usize> = (col..col+64).collect();
        let proof = single_pool().install(|| PlainProof { m:DIM,n:DIM,k:K,noise_rank:RANK,
            a:matrix_proof_from_tree(&cached.a,&a_rows), bt:matrix_proof_from_tree(&cached.bt,&b_rows),moe:None });
        let bytes = bincode::serialize(&proof)?;
        ensure!(bytes.len() <= out_cap, "proof output capacity insufficient");
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
        Ok(bytes.len() as isize)
    })
}
#[no_mangle]
pub unsafe extern "C" fn prl_proof_cache_close(handle: usize, error: *mut u8, error_cap: usize) -> isize {
    boundary(error, error_cap, || {
        let mut slot = proof_cache().lock().map_err(|_| anyhow!("proof cache poisoned"))?;
        ensure!(handle > 0 && slot.current.as_ref().map(|x| x.0) == Some(handle), "invalid or stale proof cache ID");
        slot.current = None;
        Ok(0)
    })
}
