<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce request latency through topology-aware cache placement

English | [简体中文](cache-local-placement_zh.md)

## Observations and Motivation

Adding a replica can increase compute while reducing prefix reuse or congesting a shared cache link. Concentrating warm state improves affinity but can create a fan-in hotspot. Placement determines where capacity and state live; routing chooses which requests consume them.

## Principles and Analysis

For each feasible location, compare `T = queue + restore + uncached_prefill + decode`. A GPU-local hit, host-memory restore and remote restore have different costs. Reuse first requires compatible checkpoint, positional semantics, cache dtype and connector layout.

As an arithmetic example, restoring 3 GiB over an effective 20 GB/s path takes at least 161 ms before setup and contention. Recomputing the same prefix in 40 ms is cheaper; recomputing it in 500 ms can make restoration worthwhile. Both paths still process the uncached suffix. Longest-hit selection therefore need not minimize latency.

Replication spreads contention but consumes memory and warmup bandwidth. Cold replicas spread compute yet can repeatedly regenerate the same prefix. Cached model weights speed startup without providing warm request KV; these are separate placement benefits.

## Design

Organize placement around actual shared-link domains and workload locality, including GPU–NIC affinity, NVLink/NVSwitch, rack links, host NUMA and storage bandwidth. Preserve model quota floors when several models compete for GPUs. Dense BF16 attention, MLA and hybrid state have different footprints; weight FP8 does not make remote KV cheap, and a changed TP layout may require gathering or replication.

The controller owns worker placement/lifecycle; the cache storage and connector own state movement; the request layer owns routing. An index reports locations rather than allocating workers or reserving transfer bandwidth.

## Implementation

1. Enumerate locations satisfying device, parallelism, partner-stage and quota requirements.
2. Find compatible reusable prefixes and classify their tier and transfer path; calculate actual state bytes for the cache layout.
3. Estimate queueing, contended restoration and remaining compute for the expected request mix, including warmup and duplicate-state cost.
4. Select the placement balancing request latency and shared-domain pressure, provision through the controller, then publish routable capacity when ready.
5. On worker retirement, release local state through its owner; persistent shared entries follow their storage lifecycle.

llm-d's cost-aware memory store bounds index-memory cost, not worker allocation. Production Stack integrates LMCache lookup and load/cache routing, not automatic migration. Kueue provides quota floors, borrowing and preemption. Foretoken distinguishes GPU-local credit from restored extensions (`restoreWeight=0.5`), but these scores are preferences, not transfer-time estimates; Encoder/Decode use load alone.

## Impact and Results

Topology-aware placement avoids spending more time moving state than recomputation would take and reduces concentrated restore traffic. Its cost is warmup, duplicate weights/state and shared-store capacity. The cited AFD study models scale-up versus scale-out P/D partner placement for FP8 MoE on B200, illustrating link-dependent execution costs rather than a universal measured placement speedup.

## References

- [llm-d cost-aware-memory implementation](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/kvcache/kvblock/cost_aware_memory.go)
- [Production Stack routing implementation](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [llm-d Kueue guide](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/kueue-rebalancing/README.md)
- [Foretoken routing owner](../../../../data-plane/frontend/src/router/README.md)
- [AFD design-space study](https://arxiv.org/html/2605.28302v1)
- [KV-cache knowledge](../kv-cache/)
- [disaggregated serving](../disaggregated-serving/)
