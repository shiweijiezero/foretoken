<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Match small-batch MoE to megakernel work partitions

English | [简体中文](small-batch-megakernels_zh.md)

## Observations and Motivation

Single-request decode can select several experts yet give each only one row. A large-batch grouped GEMM schedule then spends much of its time loading weights and coordinating workers. Splitting dispatch, compute, and combine adds launch boundaries; a megakernel removes those boundaries but must partition shared GPU resources among communication and computation.

## Principles and Analysis

In a persistent MegaMoE kernel, warps stage tokens, issue expert matrix products, execute epilogues, and return outputs. They compete for SMs, registers, shared memory, and bandwidth. More communication workers can shorten arrival delay while reducing compute resources; a wider output-feature tile can hold resources longer when `m_e` is tiny.

Buffer capacity `C` is not live token count `T`: a workspace sized for `C=2048` can repeatedly process `T=8`. Capacity determines storage and some compilation choices; live routes determine useful work. A profile tuned only at full capacity can therefore be inappropriate for most low-load steps.

## Design

FlashInfer provides split and fused backends with architecture- and dtype-specific profiles. The cited SM100 NVFP4 profiles vary output tile width and token-return roles: epilogue warps, reused dispatch warps, or standalone warps. These choices distribute finite execution resources, rather than add parallelism for free.

Prepare workspaces and choose configurations offline. Current architecture documents live-token-aware tuning records and pre-created workspace profiles, while older tuning sections describe capacity-only selection. Profile selection follows the active code path and its supported keys. Pooled profiles share storage; they are not independent concurrency lanes.

## Implementation

1. At initialization, prepare expert weights/scales and supported capacity profiles; use offline tuning records for the architecture, format, shapes, and supported live-token buckets.
2. Create capacity workspaces collectively in the same order on all EP ranks and warm them before graph capture. Keep storage alive until associated graphs retire, then destroy through the owning layer.
3. For each step, select within the capacity profile using the EP-wide maximum padded token count: prefer an exact recorded bucket, then the smallest larger bucket, then the largest smaller bucket. Prepare or quantize valid activations as required.
4. Launch split dispatch/compute/combine or MegaMoE with that profile. In the fused path, stage valid rows, execute FC1/activation/FC2, and return weighted results using the selected warp roles.
5. Preserve stream ordering for workspace reuse and complete output copies before consumers read them.

Collective compile-and-time tuning belongs outside the request path. SM90, SM100-family, and other backends have separate support. BF16, NVFP4, and MXFP8 profiles use different numerical representations. See [gather fusion](moe-gather-fusion.md) and [communication overlap](../parallelism-communication/moe-dispatch-overlap.md) for related costs.

## Impact and Results

MegaMoE can remove launch and intermediate staging boundaries, but cannot create token reuse for one-row experts; low-load execution may remain weight-bandwidth-bound. Upstream tuning notes include small-batch losses from combine quantization or in-kernel reduction despite larger-batch wins. Thus more fusion or a smaller payload is not uniformly better.

Prestaged microbenchmarks exclude per-forward activation preparation. Their kernel region does not include quantization, input staging, or output copies, all of which remain in service latency. The `C=2048,T=8` example distinguishes storage from useful work and is not a performance measurement.

## References

FlashInfer [MoE-EP architecture](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/docs/design_docs/moe_ep_architecture.md) and [SM100 tuning notes](https://github.com/flashinfer-ai/flashinfer/blob/a8e747c3c1f7dfe5d51fab3883824b15da6829b7/flashinfer/moe_ep/kernel_src/sm100/cutedsl_megamoe/TUNING.md), commit `a8e747c3`.
