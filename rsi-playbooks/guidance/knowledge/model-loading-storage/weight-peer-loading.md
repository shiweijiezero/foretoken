<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Weight-Loading Cost: Peer and Shared-Store Reuse

English | [简体中文](weight-peer-loading_zh.md)

## Observations and Motivation

A warm peer can supply installed weights, while a shared store can supply checkpoint files or prepared artifacts. These sources remove different startup work. A byte-compatible transfer must also preserve model identity, rank layout and runtime interpretation; reaching a peer is not enough.

## Principles and Analysis

A file-cache hit still requires checkpoint parsing and parameter installation. A runtime-tensor peer can avoid repeated transformations if its tensor names, shapes, dtype, sharding and backend representation match the target. An allocation artifact instead supplies raw bytes and needs external knowledge to reconstruct tensor views. None of these automatically restores request KV or runtime execution state.

In an illustrative example, a rank needs 10 GiB of runtime weights. A peer delivering 40 GiB/s has a 250 ms payload floor, while a file source delivering 5 GiB/s has a 2 s floor. Metadata discovery, registration, source synchronization, installation and warmup are additional. Simultaneous targets divide source bandwidth and may compete with source inference. Peer loading is not a free acceleration merely because model files are absent from the path.

## Design

Separate source discovery from the data path. A coordinator selects compatible live tensor state or a prepared file source; peer payload moves directly through NIXL rather than the central server. Target storage and finalization remain loader-owned, including recovery from partial writes.

Compatibility covers model revision, tensor manifest, dtype, rank geometry, runtime representation and worker incarnation. A peer supplies weights, not request KV or execution state. A raw allocation artifact needs external tensor reconstruction metadata, whereas a file cache still needs parsing and installation.

## Implementation

ModelExpress's cited vLLM loader executes an eligibility-ordered strategy chain:

1. Discover a matching peer and obtain its tensor manifest; allocate/register target storage and transfer compatible tensors through NIXL.
2. Await completion and finalize the engine-specific weight state. Keep the source allocation valid while reads are outstanding.
3. If that strategy is unavailable or fails under the loader's handled conditions, try eligible server cache, InstantTensor, ModelStreamer, GDS and finally engine-native loading in order. A partial target must not retain incompatible data from two attempts.
4. Where shared storage is unavailable, file-cache streaming reconstructs a local snapshot for the file consumer.

vLLM's native wrapper delegates this behavior to ModelExpress. Mooncake's cited weight management implements publication/revision metadata, leases and HA recovery; its managed reader and follow-up client/RPC surfaces are not a complete loading path. Dynamo GMS restores aligned allocation bytes, not named/shaped/dtyped tensor metadata.

## Impact and Results

Compatible installed-tensor reuse avoids repeated checkpoint parsing and representation conversion; a warm file source mainly avoids remote storage reads. Peer loading adds discovery, registration, synchronization and transfer, and multiple targets share the source's bandwidth. Source inference can contend with that fan-out.

A fast payload does not remove finalization or warmup. Fallback makes the loading chain usable when a faster source is unavailable, but successful native loading is a different execution path from successful peer reuse. The loader's source identity and completion handling keep reused weights coherent.

## References

- [ModelExpress's architecture](https://github.com/ai-dynamo/modelexpress/blob/db0cbce97184d9135d35c7290711f3053b220b6a/docs/ARCHITECTURE.md)
- [vLLM loader](https://github.com/ai-dynamo/modelexpress/blob/db0cbce97184d9135d35c7290711f3053b220b6a/modelexpress_client/python/modelexpress/engines/vllm/loader.py)
- [native ModelExpress wrapper](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/modelexpress_loader.py)
- [weight-management document](https://github.com/kvcache-ai/Mooncake/blob/342c8aeece6b4eefb76c6b8b31ccfac8dde07aa3/docs/source/design/mooncake-reshard/weight-management.md)
- [weight artifact implementation](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/gpu_memory_service/v1/snapshot/weight_artifact.py)
