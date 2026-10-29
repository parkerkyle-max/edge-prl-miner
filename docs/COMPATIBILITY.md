# NPU compatibility and qualification

**This project does not yet mine on every NPU.** The implemented backend remains
the existing qualified 4 GB A733 / VIPLite v2 route. Its historical pool test was
performed with an earlier zero-fee version. Detection of another vendor is a
request for adapter work, not evidence that it can execute PRL correctly.

The machine-readable source is [the backend catalog](../compatibility/backends.json).
It is intentionally non-exhaustive. Unknown devices are refused by the mining
backend selector; they are never routed into the A733 driver as a fallback.

## Read-only discovery

```sh
python -m edge_prl backends
python -m edge_prl devices
```

`backends` reads the shipped catalog. `devices` collects a small allowlist of
local metadata and prints JSON. It loads no vendor SDK, opens no device node,
runs no subprocess, makes no network request and changes no clocks, services,
drivers or mining settings. It does not enumerate wallet files or SSH settings.
Its output excludes hostnames, serials, network addresses, accounts and private
paths. Still inspect a report before sharing it.

On Linux the probe reads bounded device-tree and memory metadata, checks a few
known node names, and recognizes selected driver basenames in fixed accelerator
and DRM slots. A node or driver is a hint, not a verified working runtime.
Generic GPU or `/dev/accel` presence never identifies a specific NPU vendor.
Windows/macOS native discovery and USB-only Coral discovery are not implemented;
the report says `unprobed_platform` or `not_detected`, not that an NPU is absent.

| Discovery status | Meaning |
| --- | --- |
| `needs_device_validation` | The basic A733 hints match; native libraries, exact model, cooling, proofs and pool work still need validation |
| `detected_blocked` | Some A733 hints exist, but this qualified backend's platform, memory or node checks do not match |
| `detected_unimplemented` | A vendor-family hint exists; there is no mining adapter for it |
| `not_detected` | This limited probe found no matching hint; hardware may still exist |
| `unprobed_platform` | Native hardware discovery is not implemented on this operating system |

Every discovery result has `can_mine_now: false`. Even
`eligible_for_device_validation: true` is only permission to try the existing
read-only preflight and offline hardware gate. It is not a mining authorization
or a replacement for explicit operator configuration. A 12 GB A733 is not
qualified by a report from a 4 GB A733.

## Current adapter map

| Hardware/runtime family | Project status | Main work still required |
| --- | --- | --- |
| A733 / VIPLite v2, qualified 4 GB target | Implemented experimental backend | Fresh exact-artifact/device proof gate; new fee and optimization variants need their own evidence |
| RK3588 / RKNN | Research only | Exact matmul layout/accumulator adapter and two changing inputs |
| RK3562/RK3566/RK3568 / RKNN | Research only | Per-chip validation; do not reuse RK3588 assumptions |
| Ascend / CANN | Research only | Per-SoC kernel/runtime adapter and exact proof tests |
| Intel NPU / OpenVINO | Research only | Exact output despite device precision, verified placement and compilation costs |
| AMD Ryzen AI / XDNA | Research only | Exact output and per-generation runtime/compiler integration |
| Qualcomm HTP / QNN | Research only | Exact arithmetic and target-specific context; CDSP RPC alone is not proof of an NPU |
| Apple ANE / Core ML | Research only | Actual ANE placement, exact graph output and bounded input/compile costs |
| Hailo / HailoRT | Research only | Target-specific compiled model, exact output and transfer/input economics |
| Coral Edge TPU | Research only | Exact computation within quantized TFLite restrictions and fallback accounting |

## Why an AI inference benchmark is not enough

PRL needs exact arithmetic and matching full proof results. Small numeric errors
that are tolerable in image classification are not tolerable in a proof. An SDK
accepting FP32 input does not establish FP32 hardware arithmetic, and a model
successfully executing does not establish that the NPU performed its work.

- Intel documents FP32/FP16 model types but FP16 hardware computation on the
  NPU. Its model cache avoids some recompilation; neither feature establishes
  exact PRL execution. [Intel NPU documentation](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html)
- AMD's Ryzen AI tooling supports several quantized/BF16 workflows. A port must
  prove its output is exact rather than rely on ordinary quantization accuracy.
  [AMD Quark for Ryzen AI](https://quark.docs.amd.com/latest/supported_accelerators/ryzenai/index.html)
- Qualcomm documents architecture-specific contexts and different quantized or
  floating-point paths. A context compiled for one HTP generation is not assumed
  compatible with another. [QNN HTP backend](https://docs.qualcomm.com/bundle/publicresource/topics/80-63442-10/htp_backend.html)
- Core ML chooses compute-unit placement per operation; a device preference is
  not proof of ANE-only execution. Measure the actual placement.
  [Apple performance analysis](https://developer.apple.com/documentation/coreml/analyzing-a-core-ml-model-s-performance-in-xcode)
- Coral requires compiled, fully 8-bit-quantized TFLite models. Exact PRL mapping
  and CPU fallback must be demonstrated separately.
  [Coral model requirements](https://coral.ai/docs/edgetpu/models-intro/)
- Rockchip supplies chip-specific RKNN tooling, and Hailo uses its own runtime
  and model compiler ecosystem. Their artifacts are not interchangeable with
  the A733 model. [Rockchip RKNN](https://github.com/airockchip/rknn-toolkit2),
  [HailoRT](https://github.com/hailo-ai/hailort)

These are engineering requirements inferred from the SDK constraints, not a
claim that a port is impossible. Sources were checked on 2026-10-29; validate
the exact hardware/compiler/runtime versions again when implementing a port.

## Promoting a new backend

1. Record hardware/OS/compiler/runtime and redistributable build inputs. Define
   shapes, layout, signedness, rounding, overflow and output buffer ownership.
2. Support both changing job inputs, or measure every required recompile. Prove
   exact results against independent reference vectors and official proofs.
   Re-run those checks after any code, model, compiler or driver change.
3. Demonstrate actual NPU use. Keep CPU fallback separate from NPU throughput.
   Test stale jobs, cancellation, invalid outputs, timeouts and teardown without
   affecting a separate CPU miner.
4. Run a bounded real-pool experiment and preserve correlated accepted/rejected/
   unknown results. Keep user and developer-fee accounting separate. A previous
   device's result never qualifies a new device or code revision.
5. Measure full-window CPU-only versus CPU+NPU value, including compilation,
   preparation, transfers, proof work, developer fee and wall power. Vendor TOPS
   and kernel-only throughput are not whole-miner efficiency or income.

Only then should the code's supported backend set and catalog be expanded. The
current registry is useful preparation for community ports; it does not ship
those ports or substitute documentation for their hardware validation.
