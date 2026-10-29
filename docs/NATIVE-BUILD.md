# Build the native PRL proof and preparation libraries

`native-build/` contains readable source for `libpearl_prep.so` and
`libpearl_edge_proof.so`. It includes the additive proof-cache API as well as the
original stateless proof API. These libraries perform PRL preparation, proof
construction, and official verification. They do not perform NPU inference.

The build uses a fixed upstream commit, a Cargo lock file, source SHA256 checks,
and a fixed Rust compiler. This is a repeatable source-build procedure; identical
binary bytes across different hosts and C compilers are **not** promised. Each
successful build records its actual inputs, compiler versions, commands, output
hashes, and ELF architecture in a reread `build-receipt.json`.

## Inputs and licensing

The exact pins are in [`native-build/pins.json`](../native-build/pins.json).

* Official Pearl source: `pearl-research-labs/pearl`, commit
  `0a17bb345a838147a4c14fc618de1839cbc16c1e` (ISC).
* Preparation source: `benjamin920102/ascend_prl`, commit
  `12c156c24e7db40aaa903a4e79a258b5dd46a33b` (MIT). Our checked-in derivative
  limits Merkle hashing to one worker and checks the fixed dense cert3 dimensions.
* BLAKE3 C input: the unmodified `blake3` 1.8.7 crate; six input files are
  individually hashed. Its upstream Apache/LLVM/CC0 notices are retained.
* The proof wrapper/cache additions are distributed under the repository MIT
  license. Retain the upstream notices in `engine/licenses/` when redistributing
  builds. The Cargo lock also records transitive crates, whose licenses continue
  to apply; the repository MIT license does not relicense dependencies.

No vendor SDK, vendor headers, NPU model, compiler distribution, or binary is
included here. The separate A733/VIPLite v2 NPU bridge still requires the user's
installed matching vendor SDK and device model. Successfully building these two
libraries alone does not make a new NPU supported.

## Explicit Linux or WSL build

Use Linux/WSL with Python 3.9+, Git, GCC and OpenMP, and an **already installed**
Rust 1.99.1 toolchain (commit `58cf8f9c35b355199727627d07ae34a4e2846d67`).
The command checks the compiler version instead of silently installing a newer
one. The pinned upstream uses nightly features: this recipe explicitly sets
`RUSTC_BOOTSTRAP=1` with that exact stable compiler. This is a compatibility
constraint, not a claim that upstream supports stable Rust generally.

For ARM cross-compilation, also install `aarch64-linux-gnu-gcc`, its target libc
and OpenMP libraries, and Rust's `aarch64-unknown-linux-gnu` standard library.
Native aarch64 Linux can use GCC directly. Windows native and macOS builds are
not implemented.

Supply a clean official checkout and an unmodified BLAKE3 crate directory. For
example, these optional setup commands intentionally access the network:

```sh
git clone https://github.com/pearl-research-labs/pearl /path/to/pearl-source
git -C /path/to/pearl-source checkout --detach 0a17bb345a838147a4c14fc618de1839cbc16c1e
```

The BLAKE3 crate may already be under
`~/.cargo/registry/src/<registry>/blake3-1.8.7`. Set `--blake3-source` to that
crate directory, not its `c/` child. Missing Cargo dependencies must first be
fetched explicitly or fetched during a build with `--allow-network`.

From this repository, validate inputs without writing or compiling:

```sh
python3 native-build/build.py \
  --official-source /path/to/pearl-source \
  --blake3-source /path/to/blake3-1.8.7 \
  --target x86_64 --output "$PWD/local/native-host-01" --check-only
```

Build host libraries with the same command after removing `--check-only`. Build
ARM libraries with a fresh output directory:

```sh
python3 native-build/build.py \
  --official-source /path/to/pearl-source \
  --blake3-source /path/to/blake3-1.8.7 \
  --target aarch64 --output "$PWD/local/native-arm-01"
```

Builds are offline unless `--allow-network` is explicitly passed. Cargo uses
`--locked`; inputs and the lock are checked again after compilation. Use
`--rust-toolchain <installed-name>` if the pinned compiler is not called `stable`.
An optional `--cargo-target-dir /path/to/cache` reuses a local Cargo build cache.
Do not run simultaneous builds sharing the same cache. Existing output
directories and paths outside ignored `local/` are refused.

Results are in `<output>/lib/`. Before installing them, review the receipt and
run offline proof/preparation parity checks, followed by the normal native
self-test, device conformance, and pool acceptance gates. A host compile does
not establish NPU correctness, hashrate, or pool acceptance. Device SDK/model
gates remain separate.

Run the explicit native conformance test after building host libraries:

```sh
python3 native-build/selftest.py \
  --libdir "$PWD/local/native-host-01/lib" \
  --output "$PWD/local/native-selftest-01"
```

This uses about 0.5 GiB of RAM and real full-size cert3 matrices; it is deliberately
outside the lightweight CI suite. It makes no network or NPU calls. Eight
boundary row/column proofs must match the stateless ABI byte for byte, with
frozen reference roots/keys/commitments and available transcript/digest vectors.
It also checks malformed inputs, immutable matrix ownership, repeated close,
and stale handles across sequential jobs. The result is reread from a receipt
alongside the proof files. For an additional comparison with an independently
built earlier library, pass `--legacy-library /path/to/libpearl_edge_proof.so`;
by default both APIs come from the newly built library. Never use `python -O`.

The harness can also run on aarch64 Linux with matching ARM libraries, but it
consumes CPU and memory. Run device tests deliberately alongside any existing
workloads and measure contention separately.

## Cache ABI and correctness

[`include/prl_proof.h`](../native-build/include/prl_proof.h) documents the C ABI.
Cache creation copies the original matrices into immutable native-owned Merkle
trees. The caller must compare returned roots against the preparation roots.
Only one cache may be active per loaded library; handles are positive monotonic
integers, and closed or stale handles are rejected. Closing releases the native
trees. The stateless builder remains available as the byte-for-byte oracle.

Cached proof construction does not weaken verification: the official verifier,
actual candidate digest, compact target, job identity, and expiry checks remain
required. Offline easy-target test acceptance is not evidence of a payable
share. Never infer live mining speedup from proof-build timing alone.

## Recorded source-build evidence (2026-10-29 UTC)

The checked-in native sources were built in Linux/WSL for both architectures,
using the existing Cargo dependency cache, `--locked --offline`, GCC 11.4.0,
and pinned Rust 1.99.1. Source hashes and clean official source were checked
before and after compilation; both resulting ELF files and receipts were reread.
No SDK, model, miner service, or pool connection was involved in these builds.

The input manifest SHA256 was
`c5c687473d3262d761cfaf497dbc4ff23903ec09e4133b07eb4b21552fcd62bb`;
the build driver SHA256 was
`313e7c91aa9f4443d6d4f93ce924d7f66a329f96e0fa59b1e4da494f3f54af40`.

| Architecture | Library | Bytes | SHA256 |
| --- | --- | ---: | --- |
| x86_64 | `libpearl_prep.so` | 43424 | `e7b892a4a7164c838cd20f6cfe0337431d4d087154c7b9af373cac86e9d41239` |
| x86_64 | `libpearl_edge_proof.so` | 1057104 | `34c556ab3e212c4fc3253a1b5ccfbdb9d3ac9b621b7a59a34c122b667b360fa8` |
| aarch64 | `libpearl_prep.so` | 44264 | `818c257993a8c8b32eb9715aae92f6d76bd3506116f94d915baeff75f9e5f15d` |
| aarch64 | `libpearl_edge_proof.so` | 999896 | `aededc2ae1f8b1a6dbd493670b0a004496d3ffd816026edcc582e9f60e96527c` |

The fresh x86_64 binaries passed the published native self-test against the
earlier stateless proof library: eight byte-identical boundary proofs, 24
correctness/lifetime checks, and two additional sequential jobs. This included
frozen roots, keys, commitments and available transcript/digest vectors, official
verification, malformed inputs, caller-buffer mutation, and stale handles.
The public runtime wrapper also passed an integration check in both `legacy`
and `cached` modes using the fresh host binaries; cached mode released its
original Python matrix copies and closed its native handle.

ARM validation at this build stage checked ELF64 AArch64 and the exported ABI:
`prep_from_ab`, `prl_config_and_key`, `prl_build_proof`, `prl_verify_proof`, and
the three cache functions. This was a static ABI check, not ARM execution.
Seven lightweight build guard tests passed on Linux; Windows passed six with
the directory-symlink case skipped because that host lacked symlink privileges.

These results establish source build and host proof-cache conformance only.
They do not establish NPU correctness, ARM runtime behavior, pool acceptance,
or a mining speedup. Native proof-build microtimings exclude matrix preparation,
NPU scans, transport, cache creation, and interference with other CPU workloads.
