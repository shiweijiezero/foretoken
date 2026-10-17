<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Hide KV Loading Latency Through Tiered Prefetch

English | [简体中文](kv-tiered-prefetch_zh.md)

## Observations and Motivation

A cache hit is useful only when its exposed loading cost is smaller than the computation it replaces. Host RAM, SSD and remote storage extend capacity, but a high hit rate can coexist with worse first-token latency when staging, queueing or transfer completion becomes the critical path.

## Principles and Analysis

For ordinary dense attention, approximate KV bytes per rank as `D = 2 L N Hkv d b`, using layers `L`, tokens `N`, locally stored KV heads `Hkv`, head dimension `d`, and bytes per element `b`. Head replication, MLA and hybrid models require their actual cache shapes instead. For example, for a 32-layer model, 8 KV heads, dimension 128, BF16 and 8,192 tokens, the unsharded KV is 1 GiB. At an illustrative effective 20 GiB/s, the payload alone takes 50 ms; lookup, page faults, packing and admission are additional.

A first-order comparison is

`T_hit = T_lookup + T_queue + T_stage + max(0, T_copy - T_overlap) + T_remaining`.

The alternative cost is prefill of the same tokens. Concurrent loading and decode determine effective bandwidth; a link specification does not include their contention. `T_overlap` counts useful concurrent computation, not merely asynchronous submission.

A layerwise pipeline retrieves layer 0, computes its remaining work while retrieving layer 1, then waits only when the next layer is not ready. In steady state its stage time approaches `max(T_layer_copy, T_layer_compute)`, with startup and drain costs. A whole-prefix load before execution has different overlap opportunities. CPU/SSD staging can introduce a third stage, and a saturated host-memory path can serialize apparently independent transfers.

## Design

Separate placement policy from transfer execution. The controller finds reusable objects, protects their source storage, reserves destination capacity and submits loads. The engine consumes only ready data, and completion releases transfer-owned resources. This makes cancellation and eviction compatible with asynchronous I/O.

Layerwise loading can overlap the next layer's transfer with current-layer computation. Whole-prefix onboarding has different dependencies. Prefetch distance trades waiting against early destination occupancy; lower tiers may also need SSD-to-host staging before remote or H2D transfer.

## Implementation

LMCache's `prefetch_controller.py` implements this lifecycle:

1. Lock existing L1 hits and issue L2 lookup-and-lock tasks for misses.
2. Resolve returned locations and let the policy plan loads; the cited implementation resolves placement to one L1 manager.
3. Reserve L1 destinations, then submit source reads into those reservations.
4. Admit completed objects to L1, publish their availability and release the corresponding source/destination locks.
5. On cancellation, retire unused reservations only when outstanding transfers no longer own them.

Dynamo's `offload/pipeline.rs` separates policy, batching, preconditions and transfer across HBM, pinned DRAM, SSD and object storage. A layerwise engine additionally waits on each layer's readiness before attention; Mooncake asynchronous submission alone does not create that schedule.

## Impact and Results

Tiering extends reusable-cache capacity beyond HBM and can replace expensive prefill with cheaper loading. Prefetch reduces exposed waits only where useful computation overlaps actual transfers. Its costs include save/offload traffic, pinned host memory, destination reservations and eviction pressure.

Repeated long contexts benefit most when transfer resources are available. Short prompts, low reuse or shared PCIe/host-memory contention can make lower-tier hits slower than recomputation. A high hit rate alone therefore does not imply lower TTFT.

## References

- [controller implementation](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/lmcache/v1/distributed/storage_controllers/prefetch_controller.py)
- [design](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/distributed/storage_controllers/prefetch_controller.md)
- [onboarding](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/docs/onboarding.md)
- [offload pipeline](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-engine/src/offload/pipeline.rs)
