<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance Expert and Network Load: Topology-Aware Replica Placement

English | [简体中文](expert-topology-placement_zh.md)

## Observations and Motivation

An expert placement can balance token counts yet send too much traffic across a slow boundary. Conversely, preserving locality can leave one rank overloaded. The right objective includes both expert execution and the links that carry tokens, outputs, and migrated weights.

## Principles and Analysis

Replication changes the physical destination graph without changing the logical router. A copy on a nearby GPU can absorb demand without introducing another network-domain crossing, but only if the dispatch assignment actually selects it. A copy on a remote node can balance arithmetic while increasing both dispatch and return traffic.

For an illustrative layer with hidden width `H=4096`, BF16 activations, and 1000 token-expert rows crossing a node boundary, the raw row payload is `1000*4096*2=8,192,000` bytes in one direction. This excludes metadata, scales, headers, alignment, and deduplication; if several chosen experts share a destination, a transport may avoid duplicating the input row. Returning outputs is a separate traffic leg. Input deduplication and the separate return leg make traffic differ from a simple top-k multiplier.

## Design

Use a hierarchical physical placement without changing logical routing. Pack model-defined expert groups onto nodes, create redundant experts inside those nodes and distribute per-copy work over local GPUs. A nearby replica improves locality only when dispatch selects it. Weight memory for redundancy competes with KV capacity.

LPLB's `r2o` maps replica relationships through cube, hypercube or torus graphs. Cube supports an eight-GPU subgroup; torus can join a local neighbor and a neighboring node. GPU/NIC reachability and shared traffic define the real communication domains, not node count alone.

## Implementation

The cited vLLM EPLB default policy implements the hierarchy:

1. Sum demand per model group, sort groups by descending demand and place each in the lightest non-full node pack. Packs have equal item-count capacity.
2. Start with one copy per local expert. For each redundant slot, add a copy to the expert maximizing `demand / replica_count`, then increment its count.
3. Sort physical copies by per-copy load and greedily pack them into the lightest non-full local GPU pack; publish logical-to-physical mappings.
4. If group/node divisibility does not support the hierarchy, use global packing; this fallback does not preserve the same locality guarantee.

When newly assigned slots need weights, DeepEP main's `lb_prefetch_weights` uses caller-provided redundancy mappings and reserved storage to transfer weights plus quantization scales over NVLink. Every rank in the domain participates, including ranks with no slots. After completion, the caller reroutes tokens and runs expert computation. Preserve identical expert weights and all scale tensors; changing trained router bias or logical selection is a model change, not physical placement.

## Impact and Results

Hierarchical placement can keep expert dispatch/return traffic within faster domains while redundant copies spread hot-expert computation. Global packing may improve arithmetic balance but increase remote traffic. The trade-off therefore includes the slowest rank and the busiest link, not token counts alone.

Redundancy consumes weight memory and migration bandwidth; frequent changes can move more weight bytes than they save in token traffic. DeepEP main supplies the NVLink prefetch primitive, not a complete serving replication policy. PR #500 belongs to `antgroup-opt`, so its features are not mainline capabilities.

## References

- [EPLB default policy at 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/eplb/policy/default.py)
- [LPLB 0490f794](https://github.com/deepseek-ai/LPLB/tree/0490f79452f7ef277e814449600b1b1dd4c663b3)
- [DeepEP main 93eb6eb2](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [DeepEP #500](https://github.com/deepseek-ai/DeepEP/pull/500)
- [Online replica assignment](../moe-execution/batch-replica-assignment.md)
- [Windowed expert balancing](../moe-execution/expert-window-rebalancing.md)
