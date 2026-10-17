<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance batch load through expert replica assignment

English | [简体中文](batch-replica-assignment_zh.md)

## Observations and Motivation

A history-balanced expert placement can still leave one GPU waiting for another in the current batch. Equal splitting of one expert's tokens ignores each destination's other work. LPLB addresses this faster timescale: redistribute current expert evaluations over existing replicas, without loading new weights or changing logical routing.

## Principles and Analysis

An explanatory token-count minimax model is:

```text
minimize z
subject to sum_r x[e,r] = n_e
           sum_e x[e,r] <= z
           x[e,r] >= 0, only where replica(e,r) exists
```

Here `n_e` is current logical expert demand, `x[e,r]` is assigned demand on destination rank `r`, and `z` bounds rank totals. This explains the goal, not every detail of the source solver. Fractional ratios must be realized as integer token-expert rows.

If A and B have 80 and 20 rows, and 40 of A's rows belong to an expert also present on B, moving 30 balances totals at 50 each. Unreplicated work cannot move. Top-k routes count expert evaluations, not requests, and equal row counts approximate runtime only when per-row costs are comparable.

## Design

Fix a redundancy topology before per-batch planning. The cited `Planner` requires equal redundant-slot counts and a structured mapping in which replicated logical experts have exactly two physical copies. History-based EPLB reorders originals and selects hot candidates for those slots.

The batch planner exchanges demand, solves assignment ratios over that replica graph, and redirects logical IDs to physical slots. Each slot must contain the same expert parameters and scales. Logical IDs and router weights remain unchanged; only the destination changes.

## Implementation

1. Run `count_workload` on the batch's logical expert IDs.
2. In `solve_probs`, synchronize demand through the configured process group unless the implementation's DeepEP path is initialized, then invoke the compiled solver.
3. Use `weighted_select_target` with solved ratios and per-SM count metadata to realize physical destinations for the finite rows.
4. Dispatch to those slots, execute the original expert functions, and combine with the original weights and token order.

The pinned `lplb/planner.py` and `lplb/eplb.py` own topology and assignment. They reference the older DeepEP `Buffer` and NVSHMEM synchronization; current DeepEP V2.5 uses `EPBuffer` and removed NVSHMEM. Integrating these sources requires adapting that communication boundary, not substituting dependencies blindly. [Windowed balancing](expert-window-rebalancing.md) owns slower placement changes.

## Impact and Results

The LPLB README reports approximately 100 microseconds for an intra-node solve, with higher inter-node cost. This is a solver-overhead estimate from a research implementation, not a decode speedup or a model/hardware benchmark. It can outweigh saved rank imbalance in tiny batches.

Assignment reacts to batch fluctuations without weight migration, but is limited by the two-copy graph and consumes time for counting, exchange, solving, and mapping. More replicas require more storage. Token-count balance does not account for nonlinear grouped-GEMM tile efficiency or topology-dependent dispatch cost, so lower maximum rows need not mean a shorter layer.

## References

[LPLB README and planner](https://github.com/deepseek-ai/LPLB/tree/0490f79452f7ef277e814449600b1b1dd4c663b3), commit `0490f794`, including `lplb/planner.py` and `lplb/eplb.py`; [current DeepEP](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509).
