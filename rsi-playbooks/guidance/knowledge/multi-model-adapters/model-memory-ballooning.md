<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Share GPU capacity through multi-model memory ballooning

English | [简体中文](model-memory-ballooning_zh.md)

## Observations and Motivation

Distinct full models cannot generally share one base GEMM or one set of KV states. Fixed GPU partitions waste memory when their traffic peaks at different times; repeatedly swapping all weights instead incurs activation cost. Prism combines spatial and temporal sharing with memory ballooning, while its open kvcached driver provides elastic physical KV allocation.

## Principles and Analysis

Reserve a stable virtual address range for each engine's KV tensors, but map physical pages only when token blocks need storage. Empty pages can be reclaimed without changing logical addresses used by the engine or graphs. Models share device capacity, not KV contents or layout semantics.

Physical feasibility is `Σ_j(W_j+K_j+A_j)+R≤C`, covering resident weights, physical KV, activations/workspaces, runtime reserve, and device capacity. Virtual reservation is not an additional physical allocation. Complementary demand permits one model to expand as another shrinks; simultaneous peaks still require sufficient physical capacity.

## Design

The balloon driver coordinates weight and KV demand. Low-load resident models release unused KV; idle-model eviction releases weights. Reusable engine pools preserve initialization and distributed contexts across activations. Demand-complementary placement and local slack-aware arbitration decide which models run without uncontrolled contention.

Prism's fast loading path partitions tensors across node GPUs, transfers in parallel, and aggregates through NVLink using bounded streaming buffers. This accelerates model activation separately from elastic KV. The research prototype, final paper, and independently versioned kvcached driver do not have identical graph/prefix-cache configurations.

## Implementation

Physical memory follows model activity:

```text
reserve stable virtual KV ranges for each engine
on token-block allocation: determine layout-compatible page geometry
obtain physical pages and map them into that engine's range
run the model using its own weights, KV, and positions
on block release: reclaim pages only when no live block uses them
when another model needs capacity: shrink unused KV reservations
if resident idle weights must leave: quiesce and evict that model
reactivate through an engine pool and the parallel loading path
retain virtual addresses; never substitute another model's KV contents
```

No new training is needed; every model uses its own checkpoint and tokenizer. The cited kvcached 0.1.6 README lists tested vLLM through 0.30.0 and SGLang through 0.5.20, hybrid attention, and AMD/ROCm support. AMD defaults to non-contiguous KV. Oversized blocks trigger automatic page enlargement; dtype, TP, and hybrid-state geometry determine actual mapping requirements.

## Impact and Results

Elastic allocation removes idle physical KV reservations, increasing usable capacity under complementary traffic without moving logical tensor addresses. Engine reuse and parallel loading reduce reactivation work but do not remove weight bytes or all startup latency. In the final Prism evaluation, 58 models spanning 1B–70B parameters use up to 32 H100-80GB GPUs across four nodes. At TTFT SLO scale 5 and TPOT scale 2, Prism attains 99% of both targets with 16 GPUs; MuxServe++ needs 20 GPUs for 99% TTFT only at scale 30 or higher. These scales multiply each model's dedicated-GPU P95 latency, rather than denoting milliseconds. The result combines ballooning, placement, and arbitration, not allocator speed alone.

Page mapping/reclamation, fragmented block occupancy, weight transfers, and activation interference remain costs. Concurrent peaks cannot be solved by virtual addressing alone. ServerlessLLM offers loading-optimized checkpoints, pinned buffers, direct I/O, and token-replay migration as a complementary approach: replay spends compute reconstructing state, whereas direct KV migration spends transfer bandwidth. Neither faster loading nor elastic memory establishes shared computation between different models.

## References

- [Prism, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/yu-shan), [final PDF](https://www.usenix.org/system/files/osdi26-yu-shan.pdf): final methods and evaluation, including memory ballooning, engine reuse, and loading; published July 2026.
- [kvcached at `94d117c`](https://github.com/ovg-project/kvcached/tree/94d117c1e2d6e6c190223e1de35d38e1882c7acb): README, commit; existing plugin support and version/layout limits retained.
- [Prism research prototype](https://github.com/Multi-LLM/prism-research): execution examples and stated prototype scope; not substituted for the current driver or final paper.
- [ServerlessLLM original method, arXiv:2401.14351v1](https://arxiv.org/html/2401.14351v1) and [current repository](https://github.com/ServerlessLLM/ServerlessLLM): loading and migration mechanisms.
