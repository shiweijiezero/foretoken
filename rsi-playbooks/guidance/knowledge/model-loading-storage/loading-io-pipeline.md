<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance Loading Speed and Memory: Mmap, Pinned Staging, Parallel I/O

English | [简体中文](loading-io-pipeline_zh.md)

## Observations and Motivation

Lazy mapping, eager reads and pinned staging solve different problems. Mmap avoids an initial full-file copy but can trigger many small page faults. Eager loading changes the access pattern and memory footprint. Pinned host buffers enable asynchronous device transfers but neither make the filesystem faster nor guarantee overlap.

## Principles and Analysis

A lazy safetensors reader maps file-backed pages and touches tensor ranges as weights are requested. On a warm local filesystem, those pages may already be in RAM. On network storage, irregular tensor access can turn into expensive faults and small reads. The tensor-to-device copy still has to happen; mmap is not a direct storage-to-GPU transfer.

An eager reader loads a whole shard before yielding tensors. This makes file access more sequential, while materializing a larger CPU state dictionary. Parallel shard reads can increase outstanding I/O and also multiply resident host memory. Eight threads do not imply eight times bandwidth, especially when ranks compete for one disk or network mount.

Pinned staging introduces reusable host buffers: read a chunk, place it in pinned memory, enqueue H2D, and return the buffer to the pool only after its copy completes. The pinned-memory capacity is approximately `number_of_buffers × buffer_size`. A pipeline transferring at B bytes/s with stage latency t needs roughly `B × t` bytes in flight to avoid starvation; this is a sizing estimate, not a universal buffer default. Too much pinned memory reduces reclaimable RAM and can harm co-located workloads.

## Design

Build a bounded storage-to-host-to-device pipeline. Use sequential or parallel reads where storage benefits from outstanding I/O, and a reusable pinned pool where H2D needs asynchronous staging. The pool's owner tracks copy completion before reuse; yielded tensors need independent ownership if the reader recycles its internal storage.

Page-cache prefetch and pinned staging occupy different tiers. Automatic prefetch can warm a network-backed file in reclaimable RAM; pinned buffers serve device copies. GDS removes part of host staging only with a compatible filesystem, driver and loader. Shared KV offload and registered memory compete for the same host resources.

## Implementation

The cited `weight_utils.py` and `DefaultModelLoader` implement alternative readers:

1. Lazy mode opens the safetensors mapping and yields requested tensor ranges; eager mode materializes a shard's CPU tensors before yielding them.
2. Prefetch reads file pages into the page cache before tensor iteration. Automatic selection recognizes specific network filesystems and considers available RAM.
3. The separate multithreaded iterator reads shards concurrently. `DefaultModelLoader` rejects combining it with non-default safetensors strategies.
4. Model-specific installation consumes each tensor, applies the required slice/layout and copies into device parameters.

For a pinned-pool pipeline, take a free buffer, read a chunk, enqueue H2D and attach a completion event; return the buffer only after the event completes. RunAI forwards concurrency/memory limits to its external streamer. InstantTensor yields owned copies to survive loader-storage reuse; fastsafetensors has GDS and no-GDS paths.

## Impact and Results

Lazy loading avoids immediate whole-shard materialization, while eager reads can replace irregular network faults with sequential access. Parallel I/O increases outstanding requests but also resident host memory; it cannot exceed shared storage bandwidth. Pinned staging can overlap transfers with other work, at the cost of non-reclaimable host RAM.

These choices address different bottlenecks: storage threads do not remove pageable-to-pinned preparation, and pinned buffers do not accelerate a saturated filesystem. Completion-ordered reuse bounds memory without corrupting in-flight copies.

## References

- [vLLM weight utilities](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/weight_utils.py)
- [DefaultModelLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/default_loader.py)
- [RunAI loader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/runai_streamer_loader.py)
- [ServerlessLLM's method](https://arxiv.org/html/2401.14351v1)
