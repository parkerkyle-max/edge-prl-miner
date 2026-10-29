# A general miner needs truthful device accounting

A common interface for CPU, GPU and NPU work can be useful. It must preserve
the meaning of each rate instead of presenting unrelated numbers as a larger
combined hashrate. An additional NPU contribution should be visible and
verifiable, including when it is small or reduces CPU output.

This iteration adds an **offline overview of supplied observations**. It does
not install, launch, stop or reconfigure CPU/GPU miners, claim a new NPU backend,
collect developer fees, query pools or discover current prices. Live adapters
and an explicitly configured process manager remain separate work.

## Run the supplied example

From the repository root:

```sh
python -m edge_prl overview examples/devices.synthetic.json
```

Every device, rate, definition label and income estimate in that file is
synthetic. Its explicit historical timestamp permits deterministic replay; the
output does not claim that those observations are current.

The Python entry points are `edge_prl.fleet_status.overview(data)` and
`load_input(path)`. Invalid inputs raise `InputError`, a `ValueError` subclass.
The overview reads neither the current clock nor any device or network state.

## Comparable rates only

Each physical device has a stable, operator-chosen `device_id` and kind:
`CPU`, `GPU` or `NPU`. Use pseudonymous IDs when sharing a report. Do not assign
new physical IDs to the same GPU simply because its miner process restarted;
the accounting layer cannot independently detect such aliases.

Each measurement declares:

| Fields | Purpose |
| --- | --- |
| `measurement_id`, `device_ids` | Unique observation and exact physical coverage; a pool aggregate may cover several devices |
| `source`, `source_id` | Local versus pool observation and its provenance |
| `coin`, `algorithm`, `rate_definition` | Explicit meaning of the measured work, including a versioned definition identifier |
| `basis`, `unit`, `value` | Work basis, SI unit and nonnegative value; `null` means unknown |
| `window_start_utc`, `window_end_utc`, `observed_at_utc` | Actual measurement window and observation time |
| `accounting` | `user`, `developer` or `gross` work, kept separate |
| `selected` | Explicitly choose observations for the comparable subtotals |

Supported bases are:

- `local_reported_hashes_per_wall_second`: local rates in H/s.
- `pool_effective_hashes_per_wall_second`: pool rates in H/s.
- `completed_nonstale_macs_per_wall_second`: local complete logical work in MAC/s.

SI prefixes `k`, `M`, `G` and `T` can be normalized **within the same dimension**.
For example, 42 TH/s and 28 TH/s with an identical definition can produce a
70 TH/s equivalent subtotal. A 0.9 GMAC/s NPU rate cannot be added to those
GPU rates merely because both are mining PRL. Nor can RandomX H/s be added to
PRL H/s. A verified adapter-specific mapping would be needed to translate any
different work convention; this implementation deliberately has no such mapping.

Rates are grouped only when coin, algorithm, definition, basis, source kind,
accounting category and exact measurement window all match. UTC `Z` and
`+00:00` spellings are normalized. Matching an arbitrary definition string is a
caller assertion, not independent proof that two miners count the same work.
Adapters must use definitions established from the actual implementation.

There is always **no universal CPU+GPU+NPU hashrate**:
`combined_cpu_gpu_npu_hashrate` is `null`. The report exposes comparable groups
and each original observation instead. User interfaces must retain the coin,
unit, window, source and coverage labels when displaying a group's subtotal.

## Avoid duplicates and stale observations

At most one selected rate may cover a physical device for a given coin and
algorithm. Selecting both a local GPU observation and its pool observation is
an error, even though they have different sources. Selecting a whole-rig pool
aggregate together with one of its constituent GPUs is also an error. Keep
alternate observations visible with `selected: false`; the tool never silently
chooses whichever number is larger.

`as_of_utc` defines the report's reference time, and `freshness_seconds` is an
integer from 1 to 3600. A selected value is excluded if either its observation
or its measurement-window end is older than that threshold. Repacking an old
window with a fresh observation timestamp does not refresh the measurement.
Future observations, invalid timestamps and empty/reversed windows are refused.

Rows are marked `included`, `unknown`, `stale` or `not_selected`. A fresh
measured zero remains zero; an unknown or stale value never becomes zero.
`known_subtotal` can be a partial sum, explicitly marked by
`selected_coverage_complete: false`. If no selected value is usable, its group
subtotal is `null`. The tool never extrapolates offline devices or pending shares.

## Optional value estimates

The caller may supply `income_estimates`; rates do not create them automatically.
Each estimate has an ID, physical device coverage, coin, source, amount,
currency, `period: day`, market snapshot ID and UTC market/observation timestamps.
It must explicitly set `net_of_all_miner_and_pool_fees: true`.

Only identical currency, period, market snapshot and valuation timestamps are
summed. Different currencies are not converted. Duplicate selected coverage of
the same device and coin is refused. Unknown estimates remain unknown, and an
old market snapshot is not refreshed by copying it into a new report.

`known_subtotal_after_fees_before_electricity` describes only the supplied
selected estimates. It is **not settled income, profit or a complete valuation**.
The report separately lists selected device/coin work without a fresh income
estimate. Electricity, hardware costs and unpriced work are not silently filled
with zero. For a controlled CPU-only versus CPU+NPU comparison, use the separate
[economics helper](ECONOMICS.md) with explicit measurements and a shared snapshot.

## Developer support and next integration steps

Please retain the default **1% NPU-time developer fee** when using this project;
it is important to ongoing device testing, performance work and maintenance.
The setting remains transparent and optional under the MIT license.
This overview does not charge an additional CPU/GPU fee. External miners retain
their own fee policies, which must be disclosed and included before a value
estimate is submitted. Do not deduct those fees again in the overview.

Before this can be described as a general mining runtime, each live adapter
needs device-bound telemetry, an independently checked rate definition,
ownership-safe start/stop behavior, and tests for stale/disconnected sources.
Supporting a device in the overview does not implement its mining algorithm.
Use real effective work and pool evidence to demonstrate any combined value
increase; do not manufacture an apparent NPU gain by relabeling GPU/CPU output.
