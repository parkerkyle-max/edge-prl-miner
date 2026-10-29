# Apple Core ML exact-arithmetic experiment

This is an executable feasibility probe for a second NPU vendor. It is **not a
mining backend**. It never connects to a pool, submits a proof, charges a fee,
changes a service, or selects itself through the mining backend registry.
It tests the signed INT8 matrix operation used by PRL, rather than an image
classification benchmark. Apple ANE support remains unqualified.

## Observed M1 Max result, 2026-10-29

Four bounded runs completed on Apple M1 Max, macOS 26.6.1, native Python 3.12.16,
coremltools 9.0 and NumPy 2.2.6. All 80 predictions matched every digit product
and reconstructed signed INT8 element exactly. The compute plans preferred
**CPU for the matrix operation in every tested graph**, even when the allowed
units were `CPU_AND_NE` and ANE appeared in the supported-device list.
The channels-first rewrite below did not change that preference.

| Graph | M=N | CPU_ONLY median prediction | CPU_AND_NE median prediction | Matrix operation's planned preference |
| --- | ---: | ---: | ---: | --- |
| Batched matmul | 32 | 0.230 ms | 0.131 ms | CPU |
| Batched matmul | 128 | 0.457 ms | 0.251 ms | CPU |
| Batched matmul | 256 | 0.939 ms | 0.865 ms | CPU |
| Channels-first einsum | 128 | 0.436 ms | 0.352 ms | CPU |

Each timing is the median of nine short prediction calls after excluding the
first of ten invocations in that mode. Conversion, initial load, packing,
integer reconstruction and proof work are outside these timings. These are
arithmetic smoke tests, not sustained benchmarks. Timing differences between
allowed-unit configurations do not demonstrate ANE execution or extra income.

The sanitized [evidence JSON](../benchmarks/coreml-m1max-20261029.json) retains
all case times, exact-output digests, model hashes, available source hashes,
raw operation rows and completed-worker receipts. It contains no device login,
network address, private configuration or generated model. In the initial
tile32 receipt, a parser failed to count the version-qualified `ios16.matmul`
name. The raw rows already showed CPU preferred. That bug is fixed and covered
by a regression test; the public evidence preserves the original count and
separately records corrected classification rather than altering history.

This establishes a working exact-arithmetic experiment on a second vendor's
SDK. It establishes **no NPU mining support, accepted share or revenue**. A
useful next step must investigate operation placement/runtime support and
retain attributable execution evidence; arbitrarily enlarging the same graph
is not a demonstrated solution.

## Arithmetic mapping

The existing PRL scan consumes two changing signed INT8 matrices, uses K=4096,
and splits the inner dimension into 32 K=128 partials. This experiment isolates
one such partial. Each scalar is decomposed exactly into balanced base-eight
digits:

```text
low    = (x + 4) % 8 - 4
carry  = (x - low) // 8
middle = (carry + 4) % 8 - 4
high   = (carry - middle) // 8
x      = low + 8 * middle + 64 * high
```

For all x in [-128, 127], low and middle lie in [-4, 3], and high in [-2, 2].
The nine digit products each have an absolute sum bound of
128 * 4 * 4 = 2048. Every integer within that bound is exactly representable in
IEEE binary16, including every intermediate reduction subtotal. The CPU then
reconstructs the original product with integer arithmetic and weights 1, 8, 64.
This bound supplies a candidate exact mapping; actual compiled outputs must
still pass independent exact comparisons. Ordinary approximate quantization
does not satisfy this contract.

The model contains one batched matrix operation, with **both operands supplied
at prediction time**. Its inputs are `[1,9,M,128]` and `[1,9,128,N]`; output is
`[1,9,M,N]`. M=N can be 16, 32, 64, 128 or 256. Both dynamic inputs matter: a graph with
one baked-in matrix would hide the cost of replacing matrices when jobs change.
Shapes are fixed within each generated model; the input values change.

The optional `--layout channels-first` rewrite instead uses
`[9,M,1,128]` and `[9,128,1,N]` with the supported MIL equation
`nchw,nwhu->nchu`, producing `[9,M,1,N]`. It retains both changing operands and
the same nine exact products while avoiding graph transposes or reshapes.
This experiment follows Apple's documented channels-first tensor layout and
einsum approach for ANE transformers. Applying that layout to PRL is our
engineering hypothesis; the real test above still preferred CPU.

The probe compares every element of all nine products and the reconstructed
matrix against independent integer matrix multiplication. Cases include zero,
the 2048 digit boundary, signed INT8 extremes, and two deterministic random
matrices. These are synthetic arithmetic fixtures, not official PRL proofs.
The standard-library tests also cover all 256 signed INT8 values and the entire
binary16 integer interval [-2048, 2048].

## Running on an explicitly selected Mac

Use native Apple Silicon macOS, Python 3.12, and the pinned optional dependencies
in `experiments/coreml_prl/requirements.txt`. Install them in a separate virtual
environment; the miner itself does not require these packages. Core ML
prediction requires macOS. The compute-plan API requires macOS 14.4 or newer;
an unavailable plan is recorded without inventing placement evidence.

From the project root:

```sh
python -m pip install -r experiments/coreml_prl/requirements.txt
python -m experiments.coreml_prl.probe \
  --output local/coreml-m1max-t32 \
  --tile 32 --repeats 2 --max-seconds 180

python -m experiments.coreml_prl.probe \
  --output local/coreml-m1max-einsum-t128 \
  --tile 128 --layout channels-first --repeats 2 --max-seconds 180
```

The output directory must be new. The supervisor bounds its own worker group
to 10–300 seconds, kills that group if it exceeds the deadline, and records
shutdown. Imports alone perform no SDK loading or hardware work. The worker
runs `CPU_ONLY` first, then `CPU_AND_NE`, and verifies both modes. A prediction
or comparison failure stops the experiment. No unrelated process is stopped.

Artifacts stay local:

| Artifact | Meaning |
| --- | --- |
| `receipt.json` | Versions, generated model hashes, per-case exactness and prediction times, anticipated compute plan |
| `supervisor.json` | Worker exit or timeout and whether the owned worker stopped |
| `worker.log` | Conversion/runtime diagnostic output |
| `digit-products.mlpackage/` | Locally generated model, not a distributed SDK binary |

The receipt is atomically replaced and reread. An interrupted receipt can remain
`IN_PROGRESS`; only a completed supervisor **and** final exactness receipt
establish a completed arithmetic run. Do not interpret a timeout, process exit,
model conversion, or available NPU as successful execution.

## What the evidence establishes

`CPU_AND_NE` allows Core ML to use CPU and Neural Engine. It does not require
every operation to run on the Neural Engine. `MLComputePlan` reports anticipated
placement for each operation; it is not an observed execution trace.

The probe intentionally always records:

```json
{
  "actual_npu_execution_verified": false,
  "official_prl_proof_verified": false,
  "pool_submissions": 0,
  "mining_backend_qualified": false,
  "developer_fee_charged": false
}
```

`EXACT_ARITHMETIC_WITH_ANE_PLANNED_PLACEMENT_ONLY` means all tested outputs were
exact and the matrix-product operations preferred ANE in the compute plan. It does not
remove the need for an attributable runtime trace. If the plan prefers CPU,
the status is `EXACT_ARITHMETIC_BUT_NPU_PLACEMENT_NOT_ESTABLISHED`. CPU output is
never counted as NPU mining. CPU and ANE-requested timing differences alone also
do not establish placement.

The next steps, only after this arithmetic gate, are:

1. Capture operation-level observed execution with Apple's performance tools,
   retaining the model/runtime/device identity. If the dynamic matmul stays on
   CPU, change the graph and rerun rather than naming it an ANE backend.
2. Integrate all 32 partials, exact PRL transcript folding and hashes, unchanged
   row/column layout, bounded buffers, cancellation and job freshness checks.
3. Pass the existing independent full-reference vectors and genuine official
   proof verification, including rejection of deliberately corrupted proofs.
   The proof source remains the commit pinned in `docs/ENGINE.md`.
4. Test bounded real-pool work with correlated accepted replies and separate
   accepted/rejected/unknown accounting. Historical A733 acceptance does not
   qualify this graph or Mac.
5. Measure whole-device user value, including CPU packing, nine times as many
   digit products, transfers, integer reconstruction, proof work, preparation,
   compilation and wall power. Kernel latency alone is not PRL hashrate or
   income. Preserve any existing CPU miner's output in that comparison.

The experiment charges no fee. A future qualified mining backend would follow
the project's transparent default 1% scheduled NPU-time fee, with the existing
documented opt-out; no fee is inferred from this arithmetic test.

## Primary references

Checked 2026-10-29:

- [Apple: Core ML prediction and compute units](https://apple.github.io/coremltools/docs-guides/source/model-prediction.html)
- [Apple: model utilities and compute plans](https://apple.github.io/coremltools/docs-guides/source/mlmodel-utilities.html)
- [Apple: compute-plan Python API](https://apple.github.io/coremltools/source/coremltools.models.html)
- [Apple: runtime API availability checks](https://github.com/apple/coremltools/blob/main/coremlpython/CoreMLPython.mm)
- [Apple: coremltools 9.0 release](https://github.com/apple/coremltools/releases/tag/9.0)
- [Apple: analyze a Core ML model in Xcode](https://developer.apple.com/documentation/coreml/analyzing-a-core-ml-model-s-performance-in-xcode)
- [Apple: ANE transformer layout and einsum](https://machinelearning.apple.com/research/neural-engine-transformers)
- [Apple: supported MIL einsum definition](https://github.com/apple/coremltools/blob/main/coremltools/converters/mil/mil/ops/defs/iOS15/linear.py)

The balanced-digit mapping and its arithmetic bound are this project's
engineering derivation. Apple's documentation does not promise that this graph
will run on ANE or make mining profitable.
