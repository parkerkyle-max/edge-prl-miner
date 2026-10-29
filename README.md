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

**If this project is useful to you, please keep the default 1% developer fee
enabled. We also ask fork maintainers to retain it and the project attribution.**
This is a request for support, not an additional license restriction: you may
inspect, modify or disable the fee. Set `"dev_fee": 0` for zero-fee operation;
`"dev_fee": 1` is the default. The field is an integer percentage, not a fraction.

The recipient, schedule, supported pools and validation limits are documented in
[Developer fee](docs/DEV-FEE.md). There is no hidden additional developer fee.
The fee scheduler is experimental and has not yet had a fresh hardware trial.
The original device trial below ran with a 0% fee.

**中文：项目采用 MIT 开源许可证，默认设置 1% NPU 挖矿时间作为开发费。若项目对你有帮助，
希望你和分支维护者保留该设置，支持后续开发。保留开发费是自愿支持，不是 MIT 许可附加限制；
用户可以查看、修改或关闭它。独立运行的 CPU 矿工不参与此收费。**

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
A complete reproducible native build and support for additional NPUs remain
unfinished. Cloning this source alone is not sufficient to mine. Driver/DTB
names do not independently establish a board's retail model; hardware and proof
gates must pass on the actual target. See [Engine](docs/ENGINE.md).

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
