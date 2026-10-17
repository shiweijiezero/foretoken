<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reuse GEMM setup through persistent tile scheduling

English | [简体中文](gemm-persistent-tiles_zh.md)

## Observations and Motivation

Efficient tensor-core instructions can still sit inside a slow GEMM because blocks repeatedly initialize state, revisit weights with poor locality, or finish in uneven waves. Decode and small expert batches expose these costs because they provide little output-tile parallelism.

## Principles and Analysis

A conventional tiled GEMM launches one block per output tile, with `ceil(M/BM) × ceil(N/BN)` tiles. Persistent execution launches a bounded worker set; each worker processes several tiles and reuses pipeline machinery.

Persistence does not eliminate the last partial wave. With 100 equal-cost tiles and 80 workers, only 20 workers have a second tile. Scheduling changes which tiles share a worker and how setup, loads, arithmetic, and epilogues overlap. Grouped ordering can improve cache reuse but also create imbalance when tile costs differ.

## Design

A persistent worker retains operand-ring and matrix-operation state across tile assignments. TMA loads fill shared-memory slots while the consumer performs matrix operations. Output conversion and stores can overlap the next tile.

Triton Gluon provides ordinary and grouped tile schedulers. Hopper uses register-backed warp-group MMA accumulators; Blackwell's `tcgen05` path uses tensor memory. Tile size, buffer count, and live ranges determine register/shared-memory demand and occupancy.

## Implementation

The worker structure is:

```text
initialize operand buffers, barriers, and matrix-operation state
for tile in scheduler.assigned_tiles(worker):
    for reduction slice in K:
        wait for a free operand slot
        issue asynchronous operand loads
        wait for the slot's ready phase
        issue matrix operations
        wait for operand consumption to complete before releasing the slot
    convert the accumulator and store the output tile
finish outstanding stores before releasing shared resources
```

TMA barriers track expected bytes and alternate phases as slots are reused. Buffer lifetimes permit operand and epilogue storage to overlap only after prior users finish. For small M/N, split-K can create more workers, but it additionally writes partial outputs and reduces them; that is a separate work-partition choice.

## Impact and Results

Persistent scheduling amortizes per-block setup and can improve weight locality or overlap epilogues with subsequent tiles. Its benefit comes from those removed costs, not additional tensor-core arithmetic capacity.

Static assignment can leave a long tail under unequal tile costs. Deeper buffering increases live storage, potentially lowering occupancy. A kernel occupying every SM can also slow communication intended to overlap it. Small expert batches require the real routing-dependent tile distribution described in [MoE execution](../moe-execution/).

## References

- [Triton Gluon persistence tutorial](https://triton-lang.org/main/getting-started/tutorials/gluon/persistence.html): scheduling and buffer lifetimes.
- [Tutorial implementation](https://github.com/triton-lang/triton/blob/4ee1a827440d453ca276ded4219190d5e7346b8b/python/tutorials/gluon/07-persistence.py): Hopper and Blackwell paths.
- [CUTLASS efficient GEMM](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md): persistent cooperative/ping-pong designs, epilogues, and split-K.
