# Experimental A733 PRL engine

This package contains a foreground port of the previously tested FP32 256-by-256
`cached-neon-a` implementation. The original implementation produced one
pool-accepted proof on Suprnova. The extracted zero-fee alpha was separately
revalidated on one target on 2026-10-29: 64 reference hashes, six official offline
proofs, and 35 accepted submissions during a bounded five-minute live test.
The new default 1% fee scheduler has not yet undergone a fresh hardware trial.
See `VALIDATION.md` and `DEV-FEE.md` for the separate evidence scopes. Offline
unit tests use local fixtures and do not establish hardware or pool acceptance.
Long-duration stability, new-device support and positive total user income
remain unverified.

The faster experimental NN pipeline is not included. This FP32 backend uses a
fixed model with two dynamic inputs and does not need a Windows, WSL, SSH, or
x86 compiler service while mining. Initial model generation needs the vendor
toolchain. No `.nb`, `.so`, vendor header, SDK, wallet, account configuration,
SSH credential, old device state, or service installation is shipped here.

## External artifacts

Provide the following files locally on the explicitly selected 4 GB A733:

* FP32 model SHA-256
  `e0ad42a6e40b4e675d95bce537c9ca45bd1bb984d23e45b4bbf5d3fd16537e93`.
* `libprl_npu.so`, built for the `native/` cached/NEON/A-cache implementation.
* `libpearl_prep.so` and `libpearl_edge_proof.so`, built against the pinned
  preparation and official proof implementations.
* The matching installed VIPLite v2/NPU kernel driver and vendor libraries
  `libNBGlinker.so` and `libVIPhal.so` with their dependencies.
* Linux AArch64, Python 3.10 or newer, glibc, the relevant GCC/OpenMP runtime,
  and sufficient device permissions. Python engine code uses the standard
  library only.

The native backend build flags are:

```text
-shared -fPIC -std=c11 -O3 -fno-fast-math -ffp-contract=off
-Wall -Wextra -Werror -DNPU_SW_VERSION=2
-DPRL_BUFFER_MODE=1 -DPRL_USE_NEON=1 -DPRL_A_CACHE=1
-I<user-installed-VIPLite-header-directory>
prl_batch_backend.c -lNBGlinker -lVIPhal -lm -o libprl_npu.so
```

Do not use CPU-stub test binaries as the NPU backend. A full portable source
build of the preparation/proof dependencies is **not implemented in this first
package**. In particular the earlier Rust build used local source paths which
must be replaced with pinned reproducible dependencies before claiming source
build support. Inspect `ldd` on the target and require no unresolved libraries;
the read-only doctor checks ELF headers but does not prove dynamic linking.

The official proof code was pinned to Pearl commit
`0a17bb345a838147a4c14fc618de1839cbc16c1e`; preparation derives from the Ascend
fork commit `12c156c24e7db40aaa903a4e79a258b5dd46a33b`. The copied notices are
under `engine/licenses/`. Original Edge PRL project code has the root MIT license;
upstream components retain their own licenses. Neither grant covers separate
vendor artifacts or automatically covers every transitive Rust dependency.
Complete their redistribution and dependency review before distributing native
artifacts. See `LICENSE-STATUS.md` and `THIRD-PARTY-NOTICES.md` in the root.

## Configuration contract

`engine.configuration.validate_config(cfg, hardware=False)` checks structure,
formats, ranges, and target Linux paths without reading devices or loading
libraries. It returns a normalized copy and raises `ValueError` on rejection.
`preflight(cfg)` performs read-only hardware and artifact checks; it does not
write fan PWM, load native code, or mine. `hardware=True` additionally invokes
that preflight.

Required flat fields:

| Fields | Requirements |
| --- | --- |
| `hostname`, `device_confirmed` | Exact target hostname, explicit `true` after the user identifies the target. |
| `account`, `worker` | User-provided public mining account/address; never a private key or seed. |
| `dev_fee` | Integer percentage `0` or `1`; template default `1`. Fee-enabled operation requires Suprnova; Kryptex requires explicit `0`. |
| `npu_variant`, `npu_width`, `model_sha256` | `f32-256`, integer `256`, and the exact pinned hash above. |
| `backend_profile` | `cached-neon-a`; this is the only accepted profile. |
| `model`, `libdir` | Absolute Linux paths for the externally supplied artifacts. |
| `runtime_dir`, `log_dir`, `validation_gate` | Explicit writable target paths, separate from the input artifacts. |
| `cpu_affinity`, `cpu_threads` | Explicit unique CPU index list and integer `1`. Preparation is verified only with one thread. Select cores without changing the user's XMR configuration. |
| `fan_pwm_path`, `fan_enable_path` | Explicit sysfs attributes; existing values must be `255` and `1`. Software cannot prove physical fan rotation. |
| `pool_kind`, `pool_host`, `pool_port`, `allow_plaintext` | One of the modes below; all must be explicit. |
| `max_runtime_seconds` | Integer `1..2400`, applying to each foreground run including self-test. |

Kryptex uses `pool_kind=kryptex`, an approved Kryptex host, TLS port `8048`,
`allow_plaintext=false`, and a user-supplied `krx...` account. Suprnova uses
`pool_kind=suprnova`, an approved official host, port `3370`, a public `prl1...`
address, and **explicit `allow_plaintext=true`**. The latter is unencrypted
transport; no fallback silently changes a TLS connection into plaintext.
The fee uses the same explicitly selected Suprnova endpoint. Its public recipient,
600-second cycle and restart overhead are disclosed in `DEV-FEE.md`.

Preflight requires Linux AArch64, A733 in device-tree compatibility, the
qualified 4 GB memory range, the exact hostname, available selected CPU cores,
valid thermal sensors, full fan command, at least 768 MiB available RAM before
start, the exact model SHA, AArch64 ELF headers for all three libraries, and an
inactive or absent legacy `opi-prl-npu.service`. It only queries that service;
it never stops or restarts it. Any unknown legacy service state refuses start.

The A733 check accepts the exact SoC compatible `allwinner,sun60i-a733`, or
requires **both** vendor strings `xunlong,orangepi-4-pro` and `arm,sun60iw2p1`.
It never accepts an arbitrary `sun60...` prefix or either vendor string alone.
The vendor naming is verified against Orange Pi's own
[6.6 device tree](https://github.com/orangepi-xunlong/linux-orangepi/blob/orange-pi-6.6-sun60iw2/arch/arm64/boot/dts/allwinner/sun60i-a733-orangepi-4-pro.dts):
its root explicitly declares `board = "A733"` and that exact compatible pair.
Orange Pi's [board build configuration](https://github.com/orangepi-xunlong/orangepi-build/blob/next/external/config/boards/orangepi4pro.conf)
also associates Orange Pi 4 Pro with A733, the sun60iw2 family, and the A733 DTB.
Sources were checked on 2026-10-29. This is a SoC capability check; vendor DTB
labels do not independently prove the physical board's retail model, and the
exact NPU model/runtime and fresh hardware proof gates still apply.

## Running a bounded experiment

`backend_id` explicitly selects an implemented backend. Existing configurations
without the field retain the A733 backend; new templates name
`a733-viplite-v2-f32-256`. All other identifiers are refused before device reads
or SDK loading. `engine/backend/selection.py` defines the exact scan contract;
the compatibility catalog describes research status and cannot enable a driver.
Read-only `devices` discovery never replaces the existing hardware/proof gates.

From the project root on the target, after providing a configuration:

```sh
python3 -m engine.miner --help
python3 -m engine.runtime_guard --config /absolute/path/to/config.json --self-test
python3 -m engine.runtime_guard --config /absolute/path/to/config.json
```

`engine.miner --self-test` and `engine.miner --live` also delegate to the guard.
The internal `--guard-child` mode is not a public bypass: native work requires
its owning parent PID and installs a Linux parent-death kill signal.

The guard holds one device-wide nonblocking `flock`, refuses a second engine,
and runs at most one owned child process group at a time. The optional fee
schedule stops and recreates that child at user/developer role boundaries; it
does not install an indefinitely restarting service. At the configured limit
it kills the owned group even if native code or networking has stalled.
It also stops on 76 C, insufficient available memory (320 MiB
while active), invalid fan command, sensor-read failure, or 180 seconds with
no identity-bound progress heartbeat. Ctrl+C ends only this experiment.
No XMR process, service, fan control, clock, boot setting, or other application
is modified. This 40-minute experiment limit does not constitute a 24-hour
stability test.

Every changed configuration, model, engine Python file, native library,
reference fixture, device/kernel identity, or path invalidates the hardware
self-test gate. The self-test binds artifacts before and after validation,
compares the complete reference hashes, verifies genuine official proofs,
and rejects corrupted proofs. It has no network client. Never distribute a
private device's gate or copy it to another device.

## Optional proof cache

`"proof_mode": "cached"` uses the additive native cache ABI built by the
[pinned build recipe](NATIVE-BUILD.md). The default for configurations that omit
this field remains `legacy`. An explicitly selected cache fails if its ABI is
missing; it never silently falls back. Re-run the full device self-test after
changing the library or mode.

For each immutable job, the cache owns copies of the original matrices and
their Merkle trees. The wrapper checks the roots against preparation before
releasing its redundant Python originals. Candidate proofs and official
verification keep their existing target/job/expiry checks. Job replacement and
shutdown close the cache; no tree is reused across jobs. Initial cache creation,
peak memory, CPU contention and job lifetime must be included when evaluating
the benefit, not just the hot proof-construction call.

## Interpreting output

`events.jsonl` rotates with a bounded size. `miner.json` reports raw and stale
logical MAC counts and an effective logical GMAC/s estimate, alongside the
pool's submitted, accepted, rejected, and unknown counters. Local work and
offline proofs are not pool acceptance or revenue.

Only a matching pending **submit** request in the current connection session,
with `result=true` and no error, emits `share_accepted`. The event binds request
ID, job token and proof SHA. Authorization never increments accepted. A
disconnect before a reply is unknown, not accepted. A guard exit code of zero
at the time limit only means the bounded run ended; inspect its actual work
and share evidence separately.

Tests from the project root:

```sh
python3 -m unittest discover -s tests -p 'test_engine_*.py' -v
```

These tests do not connect to pools or touch NPU/fan devices. Fresh device
self-test and pool acceptance remain required before treating this extracted
version as a functioning release on any target.
