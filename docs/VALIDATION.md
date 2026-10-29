# Bounded alpha validation — 2026-10-29

This is a short functional milestone on one existing 4 GB A733/VIPLite v2 Linux
target. It is not a general hardware certification or a profitability claim.
The private operator retained full receipts outside this source project.

The source archive deployed for this test had SHA256:
`5efbe8ef5684449b664b41afd72826bc2581bcab515405d5002791ce7f99646d`.
Documentation was updated afterward; all twelve runtime Python/reference files
were independently compared with the device's gate and remained byte-identical.

| Check | Observed result |
| --- | --- |
| Read-only target/model/native checks | Passed; native library hashes matched the prior qualified local artifacts |
| Offline exact reference hashes | 64 checked |
| Official offline proofs | Six verified; corrupted proofs rejected |
| Bounded live run | 300.17 seconds under the guard |
| Correlated accepted submit responses | 35 distinct session/request pairs |
| Recorded rejected submit responses | 0 |
| Independent pool check | Suprnova public API showed the new worker and matching final-share second |
| Actual NPU use | Live process held `/dev/vipcore` |
| Highest sampled temperature | 62.434 C |
| Last local effective logical work rate | About 0.707 GMAC/s; a snapshot, not a settled pool or fiat rate |
| CPU miner | Same process/start identity, six threads; continued submitting shares |
| CPU hashrate snapshot | About 297.3 H/s before, around 280–282 H/s during dual work |
| End condition | Runtime limit, no owned experimental miner processes remaining |
| Fee and startup | 0% developer fee; no service/autostart installed |

The CPU snapshots are short rolling observations, not a randomized paired test.
Their reduction must be included when evaluating the whole device. No claim of
net additional user value, improved watts-per-income, 24-hour stability, credited
PRL balance or withdrawal is established. Public `pendingShares` can represent
weighted work and is not interpreted as a count of proof submissions.

The guard's final `STOPPED` receipt and absence of owned processes establish
shutdown. The last `miner.json` can precede shutdown and is only a snapshot;
unknown/pending submissions are never converted into accepted shares.

The experiment did not modify CPU-miner configuration or service state. Its
process identity was verified; no CPU configuration file path was obtained from
that process command line, so this test does not claim an independent before/
after hash comparison of every CPU configuration file.

The next gates are a reproducible native build, sustained stability testing,
paired CPU-only/dual value measurements with wall power, and optimization that
preserves more CPU output.

## Later 1% fee implementation

The subsequent source revision adds an optional developer-fee scheduler, with a
default of 1% scheduled NPU time. The trial above was run with a **0% fee before
that change**. Its source hash and unchanged-file comparison apply only to that
historical trial, not to the later engine revision. No fresh hardware trial of
the fee scheduler, accepted developer share or developer payout is established
by this document. Offline scheduler/protocol tests are a separate level of
evidence. See `DEV-FEE.md` for the timing and accounting contract.
