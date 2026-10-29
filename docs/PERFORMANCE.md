# Performance: reduce overhead, measure the complete miner

The first portable optimization keeps the native arithmetic, model, proof
verification and pool target unchanged. It reduces Python work between the
existing native scan and proof construction. It does not add another NPU backend
or demonstrate profitability on a new device.

## Packed scan output

Each supported native batch produces 512 hashes, 32 bytes each. Previously the
adapter sliced a ctypes array 512 times. Each slice created a Python list of 32
integers, which was then converted into a hash string. The live loop converted
every hash into a 256-bit integer before selecting potential proofs.

The new path:

1. Reuses the owned native output buffer and work counter, and retains the
   32-byte commitment buffer while the commitment is unchanged.
2. Copies the complete output into one immutable 16 KiB byte string. Returned
   output cannot be changed by the next scan.
3. Rejects hashes whose most significant byte exceeds the target's most
   significant byte. Remaining candidates receive the complete, unsigned,
   little-endian 256-bit comparison with the same inclusive `<=` boundary.
4. Builds a list of actual hits; only candidates surviving the byte check need
   separate 32-byte strings. Every hit still passes the existing job freshness,
   phase deadline and official proof verification checks before submission.

The supported model returns little-endian hashes; comparing the first byte would
be incorrect. The tests include equal/adjacent targets, all-zero/all-FF values,
every candidate position, randomized full-width targets, failures and stale
jobs. An invalid, incomplete or poisoned native result never publishes an old
buffer. A nonblocking per-backend lock rejects concurrent scans or close calls,
because ctypes releases the GIL during native execution.

`BatchBackend.scan_packed(prepared, row, col)` returns `(packed_bytes, profile)`.
`NPU.scan_packed(...)` returns `(packed_bytes, completed_macs)` and retains the
native profile as `last_profile`. `packed_candidates(packed_bytes, target)`
returns `(candidate_index, hash_bytes)` pairs. The original `scan` interfaces
still return full hash lists for reference gates and callers needing every hash.

## Initial host-only measurement

A reproducible synthetic conversion/filter experiment on Windows AMD64 with
Python 3.12.4 used seven repeats of 1,000 scans, reporting the median per scan.
The data used `random.Random(512256)`, 16,384 bytes, and explicit zero/equal-target
candidates. No native arithmetic, NPU, proof construction or pool was involved.

| Target case | Selected hashes | Previous Python path | Packed Python path |
| --- | ---: | ---: | ---: |
| `2^240 - 1` | 2 | 423.83 microseconds | 23.46 microseconds |
| `2^255 - 1` | 260 | 441.67 microseconds | 111.24 microseconds |
| `2^256 - 1` | 512 | 439.47 microseconds | 195.07 microseconds |

The host path was about 2.25–18.06 times faster in those fixtures, but saved only
about **0.24–0.40 milliseconds per scan** on that machine. This is **not** an
NPU/miner speed multiplier. If a complete native scan takes hundreds of
milliseconds, the total throughput change may be too small to distinguish from
normal variation. Native work, memory traffic, job preparation, proofs, CPU
contention and role-switch overhead still dominate many configurations.

To reproduce the comparison, construct the ctypes output once from the fixed
fixture. Compare these two operations with `timeit.repeat`, verify equal results
first, and do not include synthetic input generation in either timed region:

```python
# Previous extraction and selection (output is c_uint8 * 16384).
hashes = [bytes(output[i * 32:(i + 1) * 32]) for i in range(512)]
hits = [(i, h) for i, h in enumerate(hashes)
        if int.from_bytes(h, "little") <= target]

# New extraction and selection.
from engine.miner import packed_candidates
hits = packed_candidates(bytes(output), target)
```

## Controlled A733 comparison, 2026-10-29

On one qualified 4 GB A733 board, the published `8c7bdc3` Python scan/filter
path and the candidate packed path ran in ABBAABBA order: eight scans per block,
32 scans per implementation. Both used the same fixed prepared input, pinned
model and native libraries. Full hash buffers and selected hit buffers matched
for every coordinate. Contexts were recreated between blocks; the first scan
is included in the all-scan averages below. The CPU miner remained running.

| Measured scan plus host filter | Previous path | Packed path |
| --- | ---: | ---: |
| Mean wall time | 290.309 ms | 287.782 ms |
| Mean calling-thread CPU time | 43.987 ms | 40.817 ms |
| Complete logical work per scan second | 0.9247 GMAC/s | 0.9328 GMAC/s |
| Range of eight-scan block means | 288.035–292.490 ms | 285.634–288.597 ms |

The observed scan throughput increase was **0.88%**, with **7.20% less CPU time
in that timed scan/filter interval**. This is one short experiment with variation,
not a guaranteed device-wide or income improvement. It excludes preparation,
proof construction, network delays and fee-role switching. A second bounded
180-second live trial produced 24 correlated accepted replies, zero recorded
rejections and an independently visible pool worker. Full offline validation
checked 64 reference hashes and six official proofs first. Both trials used
explicit `dev_fee: 0`, so neither validates fee collection.

The live snapshot recorded approximately 132.84 s in native scan/adapter work,
24.94 s building proofs, 13.08 s preparing six jobs, and 0.114 s filtering hashes.
Driver-wall time (116.05 s) is nested inside native time; verification is nested
inside submission. These counters suggest focusing subsequent work on native
execution/dispatch, preparation and proofs rather than further Python filtering.
They are diagnostic observations, not a new performance guarantee.

XMR's rolling rate was about 297.8 H/s before the experiment and 281.0 H/s at its
end. Its process identity was unchanged and it continued accepting shares. This
is not a paired XMR-income test. Maximum sampled temperature was 66.906 C; the
experiment stopped at its runtime limit with no owned miner process remaining.
No new wall-power reading or positive total-income result was established.

The tested source archive SHA256 is
`a8a4aff2504e6f5634bc5716a0f2812cc6bd9fa02b71374f53980bb798e1097b`.
After testing, the only engine-source edit was a clarification of the
`packed_candidates` docstring; executable statements were unchanged. Published
documentation was updated afterward. Fresh gates still bind exact deployed bytes.
Machine-readable measurements are in
[the sanitized benchmark record](../benchmarks/a733-packed-20261029.json).

## Runtime phase measurements

The live status file includes cumulative `phase_timings.seconds` and `.calls`
for the current child process and mining role:

| Field | Measured interval |
| --- | --- |
| `npu_initialize` | Model/native adapter initialization |
| `job_prepare` | Real job preparation, including its memory allocation |
| `native_scan_and_adapter` | Entire native scan call, profile retrieval and packed-output copy |
| `candidate_filter` | Python exact candidate selection |
| `proof_build` | Construction of candidate proofs |
| `proof_verify` | Official verification called during submission |
| `submit_including_verify` | Submission call, including verification and transport-side work |

`native_cumulative_profile` preserves the native scan count and reported wall,
thread-CPU and driver-wall nanoseconds from the existing profile ABI. Driver-wall
time is a subset of native wall time; it is not automatically silicon-only
execution time. `proof_verify` is nested inside `submit_including_verify`.
**Do not add overlapping fields to obtain total run time.** Timers count attempts,
including exceptions where the interval can be recorded. They omit unrelated
loop work, network waiting and process-start costs outside their stated scope.
`process_cpu_seconds` reports this child's CPU consumption, not the separate XMR
process or the board's electrical power.

These observations reset when the guard changes user/developer roles. A saved
snapshot can lag the final work or precede a forced stop. They are profiling
evidence, not accepted-share accounting or a measure of actual revenue.

## Device acceptance and future backends

Any code change invalidates the existing hardware gate. Re-run the complete
independent reference-hash and official-proof self-test on the exact device,
then a separately bounded real-pool trial. For an A/B speed claim, alternate the
old and new implementation with the same fixed preparation/model/library,
verify every hash is equal, and report complete wall time and variability.
Random live-pool job timing is not a controlled comparison.

Measure the CPU miner and total board power too. Retain the default 1% developer
fee in ordinary use to support this work; compare the same explicitly recorded
fee configuration on both sides of any experiment. A nominal fee, switching
cost or increased CPU contention can change the user's combined income even
when this Python path is faster. See [Economics](ECONOMICS.md) and
[Developer fee](DEV-FEE.md).

Other NPU implementations must preserve exact PRL arithmetic and proof
verification. Approximate inference throughput, INT8 TOPS, CPU fallback or a
successful model load does not establish usable mining speed. Only measured
complete work and real pool acceptance can advance a backend's support status.
