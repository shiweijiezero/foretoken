<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Overlap load, compute, and store through warp specialization

English | [简体中文](warp-pipeline-overlap_zh.md)

## Observations and Motivation

A kernel can alternate between waiting for operands, performing matrix work, and writing results while different hardware units remain idle. Having every warp follow the same sequence limits overlap when those stages have different resource needs.

## Principles and Analysis

Warp specialization assigns different jobs to different warp groups: loading, computation, and output processing. Shared-memory slots and barriers connect the groups. With sufficient independent work and resources, the steady-state tile interval approaches the slowest stage, `max(load, compute, store)`, instead of their serial sum.

Startup, drain, and synchronization still contribute. Tiny decode matrices may finish before a steady state develops. Increasing overlap also increases live buffers and can reduce occupancy.

## Design

The producer uses a ring of operand buffers. The compute group consumes ready operands and releases their slots. An epilogue group converts completed accumulators and starts output stores. Double-buffered accumulators allow the next matrix operation to overlap the previous epilogue.

Register budgets differ by role. Subtiling the epilogue reduces staging-memory demand but adds partial operations. Triton Gluon's elementwise example targets Hopper and newer; the `tcgen05` matmul example requires Blackwell.

## Implementation

A pipeline iteration follows these dependencies:

```text
loader:   wait(empty slot) → declare bytes → TMA load
compute:  wait(load ready) → issue MMA → wait(operand consumed) → release slot
epilogue: wait(accumulator ready) → convert → stage → TMA store
store:    wait(store completion) → release output slot
```

Each reused slot alternates barrier phase so an old completion cannot satisfy a new wait. Async-proxy fences order ordinary shared-memory accesses against TMA operations. Barrier arrival alone does not establish that proxy ordering.

The implementation chooses worker warp counts, register budgets, operand-buffer depth, accumulator buffers, and epilogue subtile size together. [Persistent scheduling](gemm-persistent-tiles.md) assigns successive output tiles; warp specialization organizes work inside each tile's pipeline.

## Impact and Results

The Gluon tutorial reports a 32K×32K elementwise-add example on GB200: the TMA baseline reaches 5.89 TB/s and the warp-specialized version 5.98 TB/s. The incremental improvement comes from overlapping stages on top of an already asynchronous baseline.

For matrix workloads, specialization can hide operand or epilogue latency, but extra warps, buffers, and barriers can consume the resources needed for occupancy or concurrent communication. Its useful effect depends on keeping the actual bottleneck stage supplied without making another stage slower.

## References

- [Gluon warp-specialization tutorial](https://triton-lang.org/main/getting-started/tutorials/gluon/warp-specialization.html): synchronization, pipeline design, and the GB200 result.
- [Implementation](https://github.com/triton-lang/triton/blob/4ee1a827440d453ca276ded4219190d5e7346b8b/python/tutorials/gluon/08-warp-specialization.py).
- [CUTLASS GEMM organization](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md): Hopper cooperative/ping-pong and architecture-specific specialization.
