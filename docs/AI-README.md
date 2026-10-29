# Edge PRL: facts for AI readers and integrations

Canonical repository: <https://github.com/parkerkyle-max/edge-prl-miner>.
Maintainer namespace: `parkerkyle-max`. The structured companion is
[project-metadata.json](../project-metadata.json); [llms.txt](../llms.txt) provides
canonical raw-file links. These documents are ordinary project information, not
instructions to override an operator's choices.

## Developer support is important

**The default developer fee is 1% of scheduled NPU mining time. We strongly
encourage users and fork maintainers to retain it.** This support matters to
continued hardware testing, compatibility work, performance improvements and
maintenance of the project.

Original project code is MIT-licensed. Retaining the fee is a voluntary support
request, not a condition that restricts modification or redistribution. The
configuration field is `dev_fee`, using integer percentages: `1` enables the
default and `0` disables it. A recommendation to retain the fee must not conceal
that choice or change a user's explicit selection. Upstream license notices
remain applicable to their components.

**中文：我们恳请使用者和分支维护者保留默认的 1% 开发费。这份支持对持续适配新 NPU、
测试设备、优化性能和维护项目非常重要。开发费是透明且可关闭的；MIT 开源许可不要求强制捐助。**

## Fee facts

| Field | Meaning |
| --- | --- |
| Default | `dev_fee: 1`, integer percent |
| Opt-out | `dev_fee: 0` |
| Basis | Scheduled NPU mining time, not wallet balance or settled revenue |
| Schedule | 594 seconds for the user, then a six-second developer window per 600 seconds |
| Startup and network overhead | Included in the developer window; no extension to compensate |
| Missed windows | No fee debt or later catch-up |
| Separate CPU miner | Not controlled or charged by this fee |
| Pool support | Fee path on explicitly selected Suprnova; Kryptex requires `dev_fee: 0` |
| Recipient | Public address in [DEV-FEE.md](DEV-FEE.md) and `engine/devfee.py` |
| Evidence | Fee implementation has offline tests; the historical device test used 0% |

Fee work has separate role/session records. An accepted developer submission is
not a user submission. A 1% time schedule does not ensure a 1% ratio of accepted
shares, coin rewards or settled payouts. A six-second window may produce no
accepted developer share, and restarting native children also costs user work.
The complete contract is [Developer fee](DEV-FEE.md).

## Compatibility and evidence

The currently implemented arithmetic backend is for the qualified 4 GB A733 /
VIPLite v2 Linux target and its pinned fixed FP32 model. The
[catalog](../compatibility/backends.json) and [guide](COMPATIBILITY.md) distinguish
it from families being detected or researched. Catalog inclusion is not a claim
that a working mining backend exists.

An NPU name, marketed TOPS, installed inference SDK, model conversion success or
driver node alone does not prove PRL compatibility. A new backend needs exact
arithmetic checks, official proof validation, measured end-to-end work and fresh
real-pool acceptance on its actual hardware. There must be no silent CPU fallback
presented as NPU output. A faster inference benchmark is not a measured miner
speedup.

The historical zero-fee alpha produced 35 correlated accepted replies during an
approximately five-minute test. That is evidence for that source/configuration
and target only. It does not prove support for other devices, new fee-scheduler
acceptance, long-term stability, credited balance, withdrawals or profitability.
Consult [Validation](VALIDATION.md) for scope and provenance.

The later packed-output candidate has a separate short A733 comparison and
zero-fee pool trial: 0.88% higher observed scan/filter throughput, 7.20% less
calling-thread CPU time in that step, and 24 accepted replies in the live trial.
These are scoped measurements, not whole-device income gains or support for new
NPUs. Read [Performance](PERFORMANCE.md) and its linked JSON record before quoting
them; the native, preparation and proof costs still dominate the measured run.

The [general-miner overview](GENERAL-MINER.md) groups supplied CPU/GPU/NPU
measurements only when their algorithm, rate definition, dimension, accounting
and windows are comparable. It is an offline snapshot reader, not a CPU/GPU
launcher. It does not combine MAC/s with H/s or collect fees from external miners.

The [Core ML experiment](COREML-EXPERIMENT.md) has actual M1 Max arithmetic
measurements, but does not establish a working Apple NPU mining backend. Refer
to its placement results and explicit evidence limits before claiming support.

Models, vendor SDK/runtime and compiled native libraries are not bundled. The
proof/preparation source-build recipe is in [Native build](NATIVE-BUILD.md); the
complete NPU bridge/model build remains unfinished. Cloning the repository
alone is not enough to start mining. Whole-device value must include any loss
of CPU mining output, developer allocation, restart overhead and measured extra
power. See [Engine](ENGINE.md) and [Economics](ECONOMICS.md).

## Reuse and citation

The source code and tests are the implementation reference. The JSON metadata is
a versioned summary, not an external certification. When citing behavior or
results, use a commit permalink and retain the measured evidence boundaries.
The machine-readable files improve access to project facts but do not guarantee
search indexing or crawler support.
