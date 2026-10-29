# CPU/NPU combined value comparison

This tool estimates whether NPU PRL work compensates for lost CPU XMR work and
additional wall-power cost. It does not contact a pool, fetch prices, send funds,
or infer revenue from accepted-share counts. `CONDITIONAL_ESTIMATE_NOT_PAYOUT`
means exactly that: the output is a scenario based on the caller's inputs.

From the project directory:

```console
python -m edge_prl.economics comparison.json
```

The Python API is `edge_prl.economics.evaluate(data: dict) -> dict`. Invalid input
raises `InputError`; the CLI exits with status 2 and writes no result to stdout.
Only the Python standard library is required.

## Input example: synthetic numbers, not a market quote

Save this example as `comparison.json`. These prices and rates are intentionally
synthetic and do not predict an Orange Pi's current income.

```json
{
  "schema_version": 1,
  "baseline": {
    "cpu_hps": 300,
    "wall_watts": 5
  },
  "dual": {
    "cpu_hps": 280,
    "npu_effective_mac_s": 5000000000,
    "throughput_basis": "completed_nonstale_macs_per_wall_second",
    "wall_watts": 6
  },
  "market": {
    "prl": {
      "snapshot_id": "synthetic-example",
      "observed_at_utc": "2026-01-01T00:00:00Z",
      "reward_per_mac_day": 0.00000000000001,
      "cad_per_coin": 2,
      "after_pool_fee": true
    },
    "xmr": {
      "snapshot_id": "synthetic-example",
      "observed_at_utc": "2026-01-01T00:00:00Z",
      "reward_per_hash_day": 0.0000001,
      "cad_per_coin": 400,
      "after_pool_fee": true
    }
  },
  "developer_fee_fraction": 0.01,
  "electricity_cad_per_kwh": 0.12
}
```

Required market fields cannot be omitted or replaced with guessed defaults.
PRL and XMR must carry the same nonempty `snapshot_id` and the same explicit UTC
`observed_at_utc` time. `Z` and `+00:00` are equivalent. A historical timestamp is
allowed for reproducing a historical comparison; it does not become a live quote.
Retain source URLs, raw quotes, difficulty/reward assumptions and timestamps with
the caller's original market snapshot. This calculator checks consistency, not
the authenticity of that snapshot.

All supplied numeric inputs must be finite, nonnegative JSON numbers, not strings
or booleans. Unknown fields and duplicate JSON keys are rejected to catch unit or
schema mistakes. `developer_fee_fraction` defaults to 0.01 for the current release and
may range from 0 to 0.01. This is only a comparison parameter: the calculator
does not enable any developer fee in the miner.

## Units and deductions

`cpu_hps` is sustained CPU hashes per wall-clock second. The PRL input is the
rate of complete, nonstale logical MAC work over the whole measured wall-clock
window. One MAC is one multiply-accumulate, not two operations. A bare NPU kernel
benchmark, vendor TOPS figure, or short burst excluding preparation, compilation,
transfer, folding and proof work must not populate `npu_effective_mac_s`.

The PRL reward coefficient is **PRL/day per sustained 1 MAC/s** under the
explicitly established PRL accounting convention. It is not PRL per individual
MAC. The XMR coefficient is **XMR/day per sustained 1 H/s**. Do not multiply by
86400 again. Keep the mapping from logical MAC/s to the network's work convention
with the market evidence; this module cannot establish that mapping itself.

Both reward coefficients must already be net of pool fees, stated with
`after_pool_fee: true`. The tool never subtracts another pool fee. The developer
fee is applied only to NPU PRL production; it is not charged against the separate
CPU miner or its existing donation policy. The XMR input should reflect that
miner's effective sustained work after its own relevant overheads.

The calculation is:

```text
CPU-only value = baseline CPU H/s × XMR/day per H/s × CAD/XMR
dual CPU value = dual CPU H/s × XMR/day per H/s × CAD/XMR
NPU value = effective MAC/s × PRL/day per MAC/s × CAD/PRL
developer fee = NPU value × developer_fee_fraction
dual user value = dual CPU value + NPU value - developer fee
gross increment = dual user value - CPU-only value
extra electricity = (dual watts - baseline watts) × 24 / 1000 × CAD/kWh
net increment = gross increment - extra electricity
```

The example produces CPU-only value C$0.012/day, dual value after the example
fee C$0.011299/day, CPU value loss C$0.0008/day, and additional electricity cost
C$0.00288/day. Its conditional net increment is **−C$0.003581/day**. A working
NPU can therefore reduce combined value. The absolute baseline and dual net
values are reported separately from the increment.

`wall_watts` and `electricity_cad_per_kwh` may be absent or null. Missing either
power reading or the electricity price leaves `comparison.net_delta_cad_per_day`
and `estimate_positive_after_electricity` null and lists the unknown fields.
Zero is accepted only when explicitly supplied. A decrease in measured power
can yield a negative incremental electricity cost; output deltas can be negative.
The calculator does not add unmeasured compiler-PC energy or amortized hardware
cost. Include those in a broader system comparison before making net-income
claims.

## Measurement and release gate

A short successful run proves neither stable throughput nor positive user value.
Before advertising extra income:

1. Use the same physical board, CPU miner settings, pool conditions and cooling
   for CPU-only and dual runs. Record software/model hashes and the measurement
   windows. Do not compare different boards or unrelated historical windows as
   a controlled experiment.
2. Warm up, alternate baseline/dual windows, repeat the pair and preserve raw
   CPU/NPU progress and temperature logs. Measure full-window nonstale work, not
   only active-kernel time. Report variability instead of treating overlapping
   rolling hashrates as independent samples.
3. Run an initial 24-hour stability check after functional correctness, then a
   longer paired evaluation suitable for the observed task/share cadence. A
   fixed elapsed time alone is not acceptance: retain correlated real-pool
   accepted submissions, rejected/unknown replies, restart and stale-work counts.
4. Measure whole-board wall power in both modes with the same meter. Document
   its resolution; two readings both rounded to 5 W do not prove zero incremental
   power. Include any external compiler or host in a system-level comparison.
5. Use one explicit valuation snapshot for both modes. Price, network difficulty,
   pool policy, conversion costs, payout minimums and downtime can change the
   achievable result. An estimated positive delta must exceed measurement
   uncertainty before it becomes a product claim.

The helper intentionally has no field for `accepted_shares`: a few fortunate
shares must not be extrapolated into guaranteed daily income. A fee fraction
describes expected allocation of work; short-window realized developer receipts
need not equal exactly 1% of user receipts. Neither local computation nor pool
acceptance proves a withdrawal or fiat receipt. The economics input uses a
fraction such as `0.01`, while the miner's `dev_fee` field uses integer percent
`1`. Nominal time allocation also excludes neither restart overhead nor pool
luck. If the supplied NPU rate already excludes developer work, explicitly use
`developer_fee_fraction: 0` to avoid deducting the fee twice; retain that rate's
measurement basis with the input. See `DEV-FEE.md`.
