<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce KV Transfer Overhead: Layouts, Registration, and Connectors

English | [简体中文](kv-transfer-layouts_zh.md)

## Observations and Motivation

A KV transfer can be limited by descriptor construction, layout conversion or completion handling long before saturating the NIC. Compatible byte counts also do not guarantee compatible cache contents: both endpoints must interpret layers, blocks, heads, scales and positions consistently.

## Principles and Analysis

A serialized latency estimate is `T = T_register + n T_descriptor + T_submit + D/B + T_completion + T_relayout`, where `n` is descriptor count, `D` is payload bytes and `B` is effective bytes/s. Registration, per-descriptor construction, submission, completion handling and layout conversion contribute the other terms; overlap changes their exposed cost. Sixty-four layers of separately addressed tiny ranges can require many descriptors even for modest payload D. Coalescing contiguous ranges or using a cross-layer physical layout can reduce n; gathering into a staging tensor adds copy work and memory. The best result depends on message size, locality and whether the attention backend accepts that layout.

## Design

Choose a physical layout accepted by both attention and transfer. Contiguous or cross-layer ranges reduce descriptor count; staging scattered pages into a contiguous tensor adds copying and memory. Register stable regions once and retain their owner until every dependent operation completes.

Compatibility includes architecture, dtype, attention geometry/backend and transfer mode. The cited NIXL matrix supports some heterogeneous TP combinations. Mamba requires equal TP; heterogeneous block sizes require a non-hybrid model without HMA layout and smaller producer blocks. MLA replicated KV is not ordinary head sharding. `LBHNC` and `LBNHC` differ in head-splitting behavior; cross-layer blocks require `BLHNC`. Static or packed-scale support does not establish runtime dynamic-scale support.

## Implementation

A Mooncake Transfer Engine operation follows these steps:

1. Register local ranges, open the remote segment and translate selected cache blocks into source/destination ranges.
2. Coalesce adjacent compatible ranges, allocate a batch handle and submit its descriptors.
3. Await task/batch completion before publishing destination readiness; retain both allocations while the transfer is outstanding.
4. Release the batch and memory protections only after completion, including cancellation paths.

For vLLM `MultiConnector`, query every connector. Any pending lookup keeps the composite pending; otherwise choose the first connector reporting matched tokens for loading. Save through all connectors and aggregate asynchronous completion. This neither unions partial hits nor dynamically chooses the fastest source. LMCache MP retrieval additionally uses token scatter and stream-ordered lock release; Dynamo's physical transfer layer owns executor/notification coordination.

## Impact and Results

Reused registration removes repeated setup, and range coalescing reduces CPU descriptor construction and submission overhead. These savings matter most for many small ranges; large payloads can remain bandwidth-bound. Cross-layer layouts avoid some packing only when the attention backend accepts them.

Connector composition increases storage coverage but can also add all-save traffic and pending-lookup waits. Safe completion ordering prevents partial data consumption and buffer reuse while a transfer still owns memory. Matching payload size alone provides neither layout compatibility nor this lifetime guarantee.

## References

- [Transfer Engine interface](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/mooncake-transfer-engine/include/transfer_engine.h)
- [NIXL compatibility matrix](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)
- [worker source](https://github.com/vllm-project/vllm/tree/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/kv_transfer/kv_connector/v1/nixl)
- [MultiConnector implementation](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/kv_transfer/kv_connector/v1/multi_connector.py)
- [MP retrieval](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [physical transfer layer](https://github.com/ai-dynamo/dynamo/tree/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kvbm-physical/src/transfer)
