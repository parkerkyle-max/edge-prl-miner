# Edge PRL

**An experimental open-source PRL miner for idle edge-device NPUs.**

The goal is to add verifiable NPU mining output while preserving as much of an
existing CPU miner's output as possible. The initial backend targets a qualified
4 GB A733 / VIPLite v2 Linux device. This is an early research release, not a
universal Orange Pi miner or a promise of profit.

Project owner: [parkerkyle-max](https://github.com/parkerkyle-max).

## Open source and the 1% developer fee

Original Edge PRL code is released under the [MIT License](LICENSE). Upstream
components retain their own licenses and credits.

The default developer fee is **1% of scheduled NPU mining time**. It supports
development, device testing and maintenance. It does not take 1% from your wallet,
does not control your separate CPU miner, and is not a guarantee of exactly 1% of
short-term coin rewards.

**Please keep the default 1% developer fee enabled if you use this project.
This support is important to sustaining device testing, new NPU adapters,
performance work and maintenance. We also ask fork maintainers to retain the
fee setting and project attribution.**
This is a request for support, not an additional license restriction: you may
inspect, modify or disable the fee. Set `"dev_fee": 0` for zero-fee operation;
`"dev_fee": 1` is the default. The field is an integer percentage, not a fraction.

The recipient, schedule, supported pools and validation limits are documented in
[Developer fee](docs/DEV-FEE.md). There is no hidden additional developer fee.
The fee scheduler is experimental and has not yet had a fresh hardware trial.
The original device trial below ran with a 0% fee.

**中文：项目采用 MIT 开源许可证，默认设置 1% NPU 挖矿时间作为开发费。我们恳请使用者和
分支维护者保留这个设置；这份支持对持续测试设备、适配新 NPU、优化性能和维护项目非常重要。
保留开发费是自愿支持，不是 MIT 许可附加限制；用户可以查看、修改或关闭它。
独立运行的 CPU 矿工不参与此收费。**

## For AI readers, crawlers and integrations

- [llms.txt](llms.txt): a concise text index with canonical raw-file links.
- [Project metadata](project-metadata.json): versioned JSON describing the fee,
  license, evidence limits and compatibility catalog.
- [AI-readable project facts](docs/AI-README.md): a short factual reference for
  assistants, search tools and downstream documentation.
- [Compatibility catalog](compatibility/backends.json) and
  [compatibility guide](docs/COMPATIBILITY.md): implemented backends versus
  detection/research candidates, with the qualification requirements.

These files expose the same information as this README. They do not guarantee
that a crawler will index the project or that a listed NPU can mine. Hardware
detection, a usable inference runtime, bit-exact PRL work and accepted pool shares
are separate checks. The fee is visible and optional; support for its retention
does not override a user's explicit configuration choice.

## What has been verified

The earlier **zero-fee alpha** completed a bounded test on one qualified board:

- 64 independent reference hashes and six official proofs passed validation.
- About five minutes of real-pool work produced 35 correlated accepted replies
  and zero recorded rejected replies. Suprnova's public API independently showed
  the worker and matching last-share time.
- The highest sampled temperature was 62.4 C. The foreground experiment ended
  without leaving an owned mining process behind.
- The separate six-thread CPU miner continued running. Its short rolling rate
  fell from about 297 H/s to around 280–282 H/s during dual work.

These observations do **not** establish long-term stability, a positive change
in total income, credited balance or a withdrawal. They do not validate the new
fee scheduler. Read [Validation](docs/VALIDATION.md) for the measured scope.

Connection, authorization, local proof validation, a pool's accepted reply,
settlement and withdrawal are different states. Local logical MAC/s is not a
settled pool hashrate or a fiat-income estimate.

## Hardware and local artifacts

The later [proof-cache trial](docs/PERFORMANCE.md#cached-proof-construction-on-a733-2026-10-29)
recorded 22 accepted replies and zero rejected replies in 180 seconds with a 0%
fee. Eight controlled proof comparisons were byte-identical. Median hot proof
construction fell from 1.043 s to 0.006 s, with 1.404 s of additional cache
creation per job. This is a proof-step improvement, not a 175x mining or income
claim. Whole-device gains and the fee window still need separate measurements.

The initial backend uses a fixed FP32 256×256 model with both A and B as inputs.
At runtime it needs no Windows/WSL compiler host or per-job model compilation.
The separately researched dynamic-model NN route is not included in this backend.

Requirements on the target:

- Qualified 4 GB A733 Linux AArch64 device and Python 3.10+.
- VIPLite v2 driver and locally installed `libNBGlinker` and `libVIPhal`.
- Fixed `model.nb` with SHA256
  `e0ad42a6e40b4e675d95bce537c9ca45bd1bb984d23e45b4bbf5d3fd16537e93`.
- Locally supplied `libprl_npu.so`, `libpearl_prep.so`,
  `libpearl_edge_proof.so` and their system dependencies.
- Explicit CPU affinity, working cooling with the fan already commanded to full
  speed, and readable temperature sensors.

**The model, vendor SDK/runtime and compiled native libraries are not bundled.**
A [pinned source build for the proof and preparation libraries](docs/NATIVE-BUILD.md)
is provided. The NPU bridge/model build and support for additional NPUs remain
unfinished. Cloning this source alone is not sufficient to mine. Driver/DTB
names do not independently establish a board's retail model; hardware and proof
gates must pass on the actual target. See [Engine](docs/ENGINE.md).

## CPU, GPU and NPU together

The [general-miner overview](docs/GENERAL-MINER.md) can report supplied CPU,
GPU and NPU observations together. Run `python -m edge_prl overview
examples/devices.synthetic.json` for a clearly synthetic example. It does not
start, configure or charge fees to external CPU/GPU miners. Comparable rates
are grouped; different algorithms, MAC/s versus H/s, and local versus pool
observations are not added into a misleading universal hashrate. The useful
product claim is an independently measured extra NPU contribution and change
in total output, after accounting for CPU contention and all miner fees.

The [Core ML arithmetic experiment](docs/COREML-EXPERIMENT.md) explores another
NPU family. It is a research harness, not an Apple mining backend. Exact output,
planned placement, verified device execution and accepted PRL shares remain
separate evidence requirements.

Windows can run help, static configuration checks, economics calculations and
offline protocol tests. Passing those tests does not validate an NPU. A readable
fan PWM value proves a command, not physical fan rotation.

## Getting started

Run from the repository root; no Python package installation is required:

```sh
python -m edge_prl --help
python -m edge_prl init --output local/config.json
```

The generated template intentionally cannot start without editing. Supply the
target hostname, your public payout address/account, worker, local artifact paths,
CPU affinity and fan sysfs paths. Set `device_confirmed` to `true` only after
identifying the intended device. Keep private device configuration in ignored
`local/`. Wallet private keys, seed phrases and recovery backups never belong in
a miner configuration.

Suprnova's supported low-difficulty test endpoint on TCP port 3370 requires
explicit `allow_plaintext=true`. The Kryptex path uses a `krx` account and TLS port
8048; high pool difficulty may make low-power devices wait a long time for a
share. This release supports the 1% fee on Suprnova only: Kryptex requires explicit
`"dev_fee": 0`. It never changes a Kryptex TLS session into plaintext to collect a
fee. Check the pool's current rules before operating it.

```sh
python -m edge_prl doctor --config local/config.json
python -m edge_prl doctor --config local/config.json --device
python -m edge_prl self-test --config local/config.json
python -m edge_prl mine --config local/config.json
python -m edge_prl status --config local/config.json
```

`self-test` runs the complete offline numerical/proof gate without connecting to
a pool. Its receipt binds the device, configuration, code, model and libraries;
changing them requires a fresh self-test. `mine` uses a bounded foreground guard,
currently limited to 2400 seconds. Ctrl+C or the runtime limit stops the process
group owned by that experiment. There is no service installer or boot autostart.
Status files are saved observations and may be stale after an exit.

The engine does not modify an independently managed CPU miner's wallet, threads,
configuration or service. CPU contention can still reduce its output.

## Compare the whole device's value

The packed-output update reduces temporary Python allocations and adds phase
profiling. A short fixed-input A733 comparison observed **0.88% higher scan
throughput and 7.20% lower calling-thread CPU time for that scan/filter step**.
Its separate zero-fee pool trial returned 24 accepted replies. These are bounded
measurements, not a guarantee of increased total income or support for other
NPUs. See [Performance measurements and limits](docs/PERFORMANCE.md).

```sh
python -m edge_prl evaluate examples/economics.synthetic.json
```

The example contains explicitly synthetic data, not current market prices or a
profit forecast. A real comparison needs repeated CPU-only and CPU+NPU windows on
the same device, one shared price/reward snapshot, CPU output lost to contention,
the developer fee and additional measured wall-power cost. An unmeasured power
delta is not silently treated as zero. See [Economics](docs/ECONOMICS.md).

## Development

```sh
python -m unittest discover -s tests -v
python -m edge_prl bundle --output dist/edge-prl-source.tar.gz
```

Tests use local fixtures and do not mine. The source bundler allowlists files,
checks for common private material, and rereads its archive and file hashes.
Review [Third-party notices](THIRD-PARTY-NOTICES.md) and
[License scope](LICENSE-STATUS.md) before distributing native artifacts.

Do not run real mining in GitHub Actions or Codespaces. Live device trials run
directly on the operator's own equipment, outside those services. See
[Repository maintenance](docs/GITHUB.md) and [Roadmap](docs/ROADMAP.md).
